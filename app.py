"""Vinex Fasteners - Stock Management (prototype).

Stock is an append-only ledger of movements; quantities on hand are always
computed from the ledger, so every number can be traced back to who did what.
"""
import csv
import functools
import gzip
import io
import os
import sqlite3
import threading
import uuid
from datetime import date, timedelta

from flask import Flask, Response, g, jsonify, request, send_from_directory

import analytics
import auth
import insights
import prodata

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("VINEX_STOCK_DB", os.path.join(BASE, "stock.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    spec TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL DEFAULT 'pcs',
    min_stock REAL NOT NULL DEFAULT 0,
    unit_cost REAL,
    prodata_code TEXT,
    barcode TEXT,
    material_id INTEGER REFERENCES products(id),
    material_per_1000 REAL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS locations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS movements(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    kind TEXT NOT NULL CHECK(kind IN ('IN','OUT','ADJUST','TRANSFER_IN','TRANSFER_OUT')),
    qty REAL NOT NULL,
    lot TEXT NOT NULL DEFAULT '',
    reference TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    operator TEXT NOT NULL DEFAULT '',
    transfer_id TEXT,
    reversal_of INTEGER,
    prodata_synced INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_mov_product ON movements(product_id, location_id);
CREATE INDEX IF NOT EXISTS ix_mov_created ON movements(created_at);
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','staff','viewer')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS counts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    lot TEXT NOT NULL DEFAULT '',
    system_qty REAL NOT NULL,
    counted REAL NOT NULL,
    movement_id INTEGER,
    operator TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_counts_created ON counts(created_at);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")


# ---------------------------------------------------------------- database
def connect():
    db = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)  # we manage transactions
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=NORMAL")    # safe with WAL, much faster writes
    db.execute("PRAGMA cache_size=-20000")     # about 20 MB page cache per connection
    db.execute("PRAGMA temp_store=MEMORY")
    return db


# ---------------------------------------------------------------- result cache
# Analytics read the whole ledger, so their results are kept until something is saved.
# Every successful write bumps a counter in the database, which also covers several worker processes.
_cache = {}
_cache_lock = threading.Lock()


def data_version(db):
    return db.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()


def cached(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        db = get_db()
        v = data_version(db)
        key = (fn.__name__, request.full_path, v["value"] if v else "0", date.today().isoformat())
        with _cache_lock:
            hit = _cache.get(key)
        if hit is not None:
            return Response(hit, mimetype="application/json")
        resp = fn(*a, **kw)
        if resp.status_code == 200:
            with _cache_lock:
                if len(_cache) > 64:
                    _cache.clear()
                _cache[key] = resp.get_data()
        return resp
    return wrapper


@app.after_request
def bump_data_version(resp):
    if request.method in ("POST", "PUT", "DELETE") and request.path.startswith("/api/") and resp.status_code < 400 \
            and request.path not in ("/api/login", "/api/logout", "/api/me/password"):
        db = connect()
        try:
            db.execute("""INSERT INTO settings(key, value) VALUES ('data_version', '1')
                          ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1""")
        finally:
            db.close()
    return resp


@app.after_request
def compress_and_cache(resp):
    path = request.path
    if path.startswith(("/static/vendor/", "/static/icons/")):
        resp.headers["Cache-Control"] = "public, max-age=2592000"   # 30 days: fonts, scanner, icons
    text_types = ("application/json", "text/html", "text/csv", "text/javascript", "application/javascript", "text/css")
    if resp.direct_passthrough and resp.mimetype in text_types:
        resp.direct_passthrough = False          # static text files (page, scanner script) are compressed too
    if (resp.status_code == 200 and not resp.direct_passthrough and "gzip" in request.headers.get("Accept-Encoding", "")
            and "Content-Encoding" not in resp.headers
            and resp.mimetype in text_types):
        data = resp.get_data()
        if len(data) > 1024:
            resp.set_data(gzip.compress(data, 5))
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Content-Length"] = len(resp.get_data())
            resp.vary.add("Accept-Encoding")
    return resp


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = connect()
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    # upgrade databases created by an earlier version
    for table, column, ddl in (("products", "barcode", "ALTER TABLE products ADD COLUMN barcode TEXT"),
                               ("movements", "reversal_of", "ALTER TABLE movements ADD COLUMN reversal_of INTEGER"),
                               ("products", "material_id", "ALTER TABLE products ADD COLUMN material_id INTEGER REFERENCES products(id)"),
                               ("products", "material_per_1000", "ALTER TABLE products ADD COLUMN material_per_1000 REAL")):
        if column not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(ddl)
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_products_barcode ON products(barcode) WHERE barcode IS NOT NULL")
    # indexes for the queries that grow with the number of movements
    for ddl in ("CREATE INDEX IF NOT EXISTS ix_mov_reversal ON movements(reversal_of) WHERE reversal_of IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS ix_mov_kind_created ON movements(kind, created_at)",
                "CREATE INDEX IF NOT EXISTS ix_mov_product_created ON movements(product_id, created_at)",
                "CREATE INDEX IF NOT EXISTS ix_mov_product_lot ON movements(product_id, location_id, lot)"):
        db.execute(ddl)
    # stocktakes recorded before the counts table existed: rebuild them from the adjustment notes
    if not db.execute("SELECT 1 FROM settings WHERE key='counts_backfilled'").fetchone():
        import re
        for m in db.execute("SELECT * FROM movements WHERE kind='ADJUST' AND reversal_of IS NULL AND note LIKE '%Stocktake: counted%'").fetchall():
            hit = re.search(r"Stocktake: counted (-?[\d.e+]+), system had (-?[\d.e+]+)", m["note"])
            if hit:
                db.execute("INSERT INTO counts(product_id, location_id, lot, system_qty, counted, movement_id, operator, created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (m["product_id"], m["location_id"], m["lot"] or "", float(hit[2]), float(hit[1]), m["id"], m["operator"], m["created_at"]))
        db.execute("INSERT INTO settings(key, value) VALUES ('counts_backfilled', '1')")
    db.execute("PRAGMA optimize=0x10002")       # refresh query planner statistics
    if db.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 0:
        db.execute("INSERT INTO locations(name) VALUES ('Main warehouse')")
    db.close()


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status
        self.extra = {}  # optional machine-readable details sent with the error


@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify(error=e.message, **e.extra), e.status


def num(value, field, minimum=None, required=True):
    if value is None or value == "":
        if required:
            raise ApiError(f"{field} is required")
        return None
    try:
        v = round(float(value), 3)
    except (TypeError, ValueError):
        raise ApiError(f"{field} must be a number")
    if minimum is not None and v < minimum:
        raise ApiError(f"{field} must be at least {minimum}")
    return v


def text(payload, key, required=False, default=""):
    v = str(payload.get(key) if payload.get(key) is not None else default).strip()
    if required and not v:
        raise ApiError(f"{key} is required")
    return v


# ---------------------------------------------------------------- stock logic
def balance(db, product_id, location_id, lot=""):
    q = "SELECT COALESCE(SUM(qty),0) FROM movements WHERE product_id=? AND location_id=?"
    args = [product_id, location_id]
    if lot:
        q += " AND lot=?"
        args.append(lot)
    return round(db.execute(q, args).fetchone()[0], 3)


def insert_movement(db, product_id, location_id, kind, qty, lot, reference, note, operator, transfer_id=None):
    cur = db.execute(
        """INSERT INTO movements(product_id, location_id, kind, qty, lot, reference, note, operator, transfer_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (product_id, location_id, kind, qty, lot, reference, note, operator, transfer_id),
    )
    return cur.lastrowid


def require_product(db, product_id):
    p = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        raise ApiError("Product not found", 404)
    if not p["active"]:
        raise ApiError("Product is inactive")
    return p


def require_location(db, location_id, label="location"):
    loc = db.execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone()
    if not loc:
        raise ApiError(f"{label} not found", 404)
    if not loc["active"]:
        raise ApiError(f"{label} is inactive")
    return loc


def stock_summary(db):
    products = db.execute("SELECT * FROM products WHERE active=1 ORDER BY sku").fetchall()
    rows = db.execute(
        """SELECT m.product_id, m.location_id, l.name AS location, ROUND(SUM(m.qty),3) AS qty
           FROM movements m JOIN locations l ON l.id=m.location_id
           GROUP BY m.product_id, m.location_id HAVING ABS(SUM(m.qty)) > 0.0005
           ORDER BY l.name"""
    ).fetchall()
    by_product = {}
    for r in rows:
        by_product.setdefault(r["product_id"], []).append(
            {"location_id": r["location_id"], "location": r["location"], "qty": r["qty"]}
        )
    out = []
    for p in products:
        locs = by_product.get(p["id"], [])
        total = round(sum(x["qty"] for x in locs), 3)
        status = "out" if total <= 0 else ("low" if p["min_stock"] > 0 and total <= p["min_stock"] else "ok")
        out.append({**dict(p), "total": total, "locations": locs, "status": status})
    return out


def activity_series(db, days=14):
    """Received / issued movement counts per day, for the dashboard chart."""
    start = date.today() - timedelta(days=days - 1)
    rows = db.execute(
        """SELECT date(created_at) AS d, kind, COUNT(*) AS n FROM movements
           WHERE kind IN ('IN','OUT') AND date(created_at) >= ? GROUP BY d, kind""",
        (start.isoformat(),),
    ).fetchall()
    counts = {(r["d"], r["kind"]): r["n"] for r in rows}
    series = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        series.append({"date": d, "in": counts.get((d, "IN"), 0), "out": counts.get((d, "OUT"), 0)})
    return series


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/sw.js")
def service_worker():
    # served from the root so it can control the whole app
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript", max_age=0)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json", max_age=0)


# ---------------------------------------------------------------- products
@app.get("/api/products")
def list_products():
    db = get_db()
    include_inactive = request.args.get("all") == "1"
    q = "SELECT * FROM products" + ("" if include_inactive else " WHERE active=1") + " ORDER BY sku"
    return jsonify([dict(r) for r in db.execute(q)])


UNITS = {"kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg", "kilogramë": "kg",
         "pcs": "pcs", "pc": "pcs", "piece": "pcs", "pieces": "pcs", "unit": "pcs", "units": "pcs", "copë": "pcs", "cope": "pcs"}


def unit_of(payload, current=None):
    """Two units: kg (main, decimals allowed) and pcs (counted in whole numbers 1, 2, 3).
    An existing product that already uses another unit keeps it."""
    raw = text(payload, "unit").lower()
    if not raw:
        return current or "kg"
    if raw in UNITS:
        return UNITS[raw]
    if current and raw == current.lower():
        return current
    raise ApiError("Unit must be kg or pcs (pieces)")


def check_whole(db, pid, qty, label="Quantity"):
    """Products counted in pieces take whole numbers only."""
    unit = db.execute("SELECT unit FROM products WHERE id=?", (pid,)).fetchone()["unit"]
    if unit == "pcs" and qty is not None and abs(qty - round(qty)) > 1e-9:
        raise ApiError(f"{label} must be a whole number for products counted in pieces")


def product_fields(payload, current_unit=None):
    return {
        "sku": text(payload, "sku", required=True),
        "name": text(payload, "name", required=True),
        "category": text(payload, "category"),
        "spec": text(payload, "spec"),
        "unit": unit_of(payload, current_unit),
        "min_stock": num(payload.get("min_stock", 0) or 0, "min_stock", 0),
        "unit_cost": num(payload.get("unit_cost"), "unit_cost", 0, required=False),
        "prodata_code": text(payload, "prodata_code") or None,
        "barcode": text(payload, "barcode") or None,
    }


def set_material(db, pid, payload):
    """Link a product to the raw material it is made from (by SKU), with the amount used per 1,000 units.
    Only touches the link when the payload has the material_sku field, so older clients and CSVs keep it."""
    if "material_sku" not in payload:
        return
    sku = text(payload, "material_sku")
    if not sku:
        db.execute("UPDATE products SET material_id=NULL, material_per_1000=NULL WHERE id=?", (pid,))
        return
    m = db.execute("SELECT id FROM products WHERE lower(sku)=lower(?)", (sku,)).fetchone()
    if not m:
        raise ApiError(f"Raw material '{sku}' is not a product. Add it as a product first.")
    if m["id"] == pid:
        raise ApiError("A product can't be made from itself")
    if db.execute("SELECT 1 FROM products WHERE id=? AND material_id=?", (m["id"], pid)).fetchone():
        raise ApiError(f"'{sku}' is itself made from this product")
    per = num(payload.get("material_per_1000"), "Amount per 1,000", 0)
    if per <= 0:
        raise ApiError("Amount of material per 1,000 must be more than 0")
    db.execute("UPDATE products SET material_id=?, material_per_1000=? WHERE id=?", (m["id"], per, pid))


def duplicate_message(e, f):
    return f"Barcode '{f['barcode']}' is already used by another product" if "barcode" in str(e).lower() else f"SKU '{f['sku']}' already exists"


@app.post("/api/products")
def create_product():
    db = get_db()
    payload = request.get_json(force=True) or {}
    f = product_fields(payload)
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute(
            """INSERT INTO products(sku,name,category,spec,unit,min_stock,unit_cost,prodata_code,barcode)
               VALUES (:sku,:name,:category,:spec,:unit,:min_stock,:unit_cost,:prodata_code,:barcode)""",
            f,
        )
        set_material(db, cur.lastrowid, payload)
        db.execute("COMMIT")
    except sqlite3.IntegrityError as e:
        db.execute("ROLLBACK")
        raise ApiError(duplicate_message(e, f), 409)
    except Exception:
        db.execute("ROLLBACK")
        raise
    return jsonify(id=cur.lastrowid), 201


@app.put("/api/products/<int:pid>")
def update_product(pid):
    db = get_db()
    payload = request.get_json(force=True) or {}
    cur_row = db.execute("SELECT unit FROM products WHERE id=?", (pid,)).fetchone()
    f = product_fields(payload, cur_row["unit"] if cur_row else None)
    f["id"] = pid
    f["active"] = 1 if payload.get("active", True) else 0
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute(
            """UPDATE products SET sku=:sku,name=:name,category=:category,spec=:spec,unit=:unit,
               min_stock=:min_stock,unit_cost=:unit_cost,prodata_code=:prodata_code,barcode=:barcode,active=:active
               WHERE id=:id""",
            f,
        )
        if cur.rowcount == 0:
            raise ApiError("Product not found", 404)
        set_material(db, pid, payload)
        db.execute("COMMIT")
    except sqlite3.IntegrityError as e:
        db.execute("ROLLBACK")
        raise ApiError(duplicate_message(e, f), 409)
    except Exception:
        db.execute("ROLLBACK")
        raise
    return jsonify(ok=True)


@app.delete("/api/products/<int:pid>")
def delete_product(pid):
    """Admin only (see auth.allowed). A product with no movements is removed outright. One that has movements is
    refused unless the body says {"with_history": true}, which also deletes its movements and stocktake counts."""
    db = get_db()
    payload = request.get_json(silent=True) or {}
    with_history = payload.get("with_history") is True
    db.execute("BEGIN IMMEDIATE")
    try:
        p = db.execute("SELECT sku, name FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            raise ApiError("Product not found", 404)
        made = [r["sku"] for r in db.execute("SELECT sku FROM products WHERE material_id=? ORDER BY sku", (pid,))]
        if made:
            raise ApiError(f"'{p['sku']}' is the raw material of {', '.join(made[:5])}{'...' if len(made) > 5 else ''}. "
                           "Clear \"Made from\" on those products first", 409)
        moves = db.execute("SELECT COUNT(*) FROM movements WHERE product_id=?", (pid,)).fetchone()[0]
        if moves and not with_history:
            err = ApiError(f"'{p['sku']}' has {moves} stock movement{'s' if moves != 1 else ''}. "
                           "Deactivate it instead, or delete it together with its history", 409)
            err.extra = {"movements": moves}
            raise err
        db.execute("DELETE FROM counts WHERE product_id=?", (pid,))
        db.execute("DELETE FROM movements WHERE product_id=?", (pid,))
        db.execute("DELETE FROM products WHERE id=?", (pid,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return jsonify(ok=True, deleted_sku=p["sku"], movements_deleted=moves if with_history else 0)


@app.get("/api/lookup")
def lookup():
    """Find a product from a scanned code: barcode first, then SKU, then Prodata code."""
    code = request.args.get("code", "").strip()
    if not code:
        raise ApiError("code is required")
    p = get_db().execute(
        """SELECT id, sku, name, unit FROM products
           WHERE active=1 AND (barcode=? OR lower(sku)=lower(?) OR lower(prodata_code)=lower(?))
           ORDER BY (barcode=?) DESC LIMIT 1""",
        (code, code, code, code),
    ).fetchone()
    if not p:
        raise ApiError(f"No product matches '{code}'", 404)
    return jsonify(dict(p))


# ---------------------------------------------------------------- locations
@app.get("/api/locations")
def list_locations():
    db = get_db()
    return jsonify([dict(r) for r in db.execute("SELECT * FROM locations ORDER BY name")])


@app.post("/api/locations")
def create_location():
    db = get_db()
    name = text(request.get_json(force=True) or {}, "name", required=True)
    try:
        cur = db.execute("INSERT INTO locations(name) VALUES (?)", (name,))
    except sqlite3.IntegrityError:
        raise ApiError(f"Location '{name}' already exists", 409)
    return jsonify(id=cur.lastrowid), 201


@app.put("/api/locations/<int:lid>")
def update_location(lid):
    db = get_db()
    payload = request.get_json(force=True) or {}
    name = text(payload, "name", required=True)
    active = 1 if payload.get("active", True) else 0
    if not active:
        on_hand = db.execute("SELECT COALESCE(SUM(qty),0) FROM movements WHERE location_id=?", (lid,)).fetchone()[0]
        if abs(on_hand) > 0.0005:
            raise ApiError("Location still holds stock - transfer it out before deactivating")
    try:
        cur = db.execute("UPDATE locations SET name=?, active=? WHERE id=?", (name, active, lid))
    except sqlite3.IntegrityError:
        raise ApiError(f"Location '{name}' already exists", 409)
    if cur.rowcount == 0:
        raise ApiError("Location not found", 404)
    return jsonify(ok=True)


# ---------------------------------------------------------------- stock views
@app.get("/api/stock")
def get_stock():
    return jsonify(stock_summary(get_db()))


@app.get("/api/stock/detail")
def stock_detail():
    db = get_db()
    pid = request.args.get("product_id", type=int)
    rows = db.execute(
        """SELECT l.name AS location, m.lot, ROUND(SUM(m.qty),3) AS qty
           FROM movements m JOIN locations l ON l.id=m.location_id
           WHERE m.product_id=? GROUP BY m.location_id, m.lot
           HAVING ABS(SUM(m.qty)) > 0.0005 ORDER BY l.name, m.lot""",
        (pid,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/availability")
def availability():
    db = get_db()
    pid = request.args.get("product_id", type=int)
    lid = request.args.get("location_id", type=int)
    lot = request.args.get("lot", "").strip()
    if not pid or not lid:
        return jsonify(qty=None)
    return jsonify(qty=balance(db, pid, lid, lot))


@app.get("/api/dashboard")
@cached
def dashboard():
    db = get_db()
    stock = stock_summary(db)
    today = db.execute("SELECT COUNT(*) FROM movements WHERE date(created_at)=date('now','localtime')").fetchone()[0]
    value = sum(s["total"] * s["unit_cost"] for s in stock if s["unit_cost"] is not None and s["total"] > 0)
    low = [s for s in stock if s["status"] in ("low", "out") and s["min_stock"] > 0]
    low.sort(key=lambda s: (s["total"] / s["min_stock"]) if s["min_stock"] else 0)
    recent = db.execute(MOVEMENT_SELECT + " ORDER BY m.id DESC LIMIT 10").fetchall()
    return jsonify(
        products=len(stock),
        low_count=len([s for s in stock if s["status"] == "low"]),
        out_count=len([s for s in stock if s["status"] == "out"]),
        stock_value=round(value, 2),
        movements_today=today,
        low=low[:10],
        recent=[dict(r) for r in recent],
        activity=activity_series(db),
    )


# ---------------------------------------------------------------- movements
MOVEMENT_SELECT = """SELECT m.id, m.created_at, m.kind, m.qty, m.lot, m.reference, m.note, m.operator,
       m.transfer_id, m.prodata_synced, m.product_id, m.reversal_of,
       EXISTS(SELECT 1 FROM movements r WHERE r.reversal_of = m.id) AS reversed,
       p.sku, p.name AS product, p.unit, l.name AS location
       FROM movements m JOIN products p ON p.id=m.product_id JOIN locations l ON l.id=m.location_id"""


def query_movements(db, limit):
    q, kind, pid = request.args.get("q", "").strip(), request.args.get("kind", "").strip(), request.args.get("product_id", type=int)
    where, args = [], []
    if q:
        where.append("(p.sku LIKE ? OR p.name LIKE ? OR m.reference LIKE ? OR m.lot LIKE ? OR l.name LIKE ?)")
        args += [f"%{q}%"] * 5
    if kind:
        where.append("m.kind=?")
        args.append(kind)
    if pid:
        where.append("m.product_id=?")
        args.append(pid)
    sql = MOVEMENT_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY m.id DESC LIMIT ?"
    return db.execute(sql, args + [limit]).fetchall()


@app.get("/api/movements")
def list_movements():
    limit = min(request.args.get("limit", 300, type=int), 5000)
    return jsonify([dict(r) for r in query_movements(get_db(), limit)])


@app.post("/api/movements")
def post_movement():
    """kind: IN | OUT | TRANSFER | ADJUST (stocktake: send `counted`, the real quantity found)."""
    db = get_db()
    d = request.get_json(force=True) or {}
    kind = text(d, "kind", required=True).upper()
    if kind not in ("IN", "OUT", "TRANSFER", "ADJUST"):
        raise ApiError("Unknown movement type")
    lot, reference, note = (text(d, k) for k in ("lot", "reference", "note"))
    operator = g.user["username"]  # taken from the login, not from the request
    pid, lid = d.get("product_id"), d.get("location_id")

    db.execute("BEGIN IMMEDIATE")  # serialise writers so two people can't both take the last box
    try:
        require_product(db, pid)
        require_location(db, lid, "Location")

        if kind == "IN":
            qty = num(d.get("qty"), "Quantity", 0.001)
            check_whole(db, pid, qty)
            insert_movement(db, pid, lid, "IN", qty, lot, reference, note, operator)

        elif kind == "OUT":
            qty = num(d.get("qty"), "Quantity", 0.001)
            check_whole(db, pid, qty)
            have = balance(db, pid, lid, lot)
            if qty > have + 1e-9:
                raise ApiError(f"Not enough stock{' in lot ' + lot if lot else ''}: {have:g} available, {qty:g} requested")
            insert_movement(db, pid, lid, "OUT", -qty, lot, reference, note, operator)

        elif kind == "TRANSFER":
            qty = num(d.get("qty"), "Quantity", 0.001)
            check_whole(db, pid, qty)
            to_id = d.get("to_location_id")
            require_location(db, to_id, "Destination")
            if int(to_id) == int(lid):
                raise ApiError("Source and destination must differ")
            have = balance(db, pid, lid, lot)
            if qty > have + 1e-9:
                raise ApiError(f"Not enough stock{' in lot ' + lot if lot else ''}: {have:g} available, {qty:g} requested")
            tid = uuid.uuid4().hex[:12]
            insert_movement(db, pid, lid, "TRANSFER_OUT", -qty, lot, reference, note, operator, tid)
            insert_movement(db, pid, to_id, "TRANSFER_IN", qty, lot, reference, note, operator, tid)

        else:  # ADJUST
            counted = num(d.get("counted"), "Counted quantity", 0)
            check_whole(db, pid, counted, "Counted quantity")
            have = balance(db, pid, lid, lot)
            delta = round(counted - have, 3)
            mid = None
            if abs(delta) >= 0.0005:
                mid = insert_movement(db, pid, lid, "ADJUST", delta, lot, reference,
                                      (note + " | " if note else "") + f"Stocktake: counted {counted:g}, system had {have:g}", operator)
            # every count is recorded, matching or not, so count accuracy can be measured
            db.execute("INSERT INTO counts(product_id, location_id, lot, system_qty, counted, movement_id, operator) VALUES (?,?,?,?,?,?,?)",
                       (pid, lid, lot or "", have, counted, mid, operator))
            if mid is None:
                db.execute("COMMIT")
                return jsonify(ok=True, changed=False, message="Count matches the system - no adjustment needed")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return jsonify(ok=True, changed=True), 201


REVERSE_KIND = {"IN": "OUT", "OUT": "IN", "ADJUST": "ADJUST", "TRANSFER_IN": "TRANSFER_OUT", "TRANSFER_OUT": "TRANSFER_IN"}


@app.post("/api/movements/<int:mid>/reverse")
def reverse_movement(mid):
    """Admin only (see auth.allowed). Books the opposite movement - both legs of a transfer - so a
    mistake is undone without rewriting history."""
    db = get_db()
    reason = text(request.get_json(force=True) or {}, "note")
    db.execute("BEGIN IMMEDIATE")
    try:
        m = db.execute("SELECT * FROM movements WHERE id=?", (mid,)).fetchone()
        if not m:
            raise ApiError("Movement not found", 404)
        if m["reversal_of"] is not None:
            raise ApiError("A reversal cannot be reversed - book a new movement instead")
        legs = db.execute("SELECT * FROM movements WHERE transfer_id=?", (m["transfer_id"],)).fetchall() if m["transfer_id"] else [m]
        marks = ",".join("?" * len(legs))
        if db.execute(f"SELECT 1 FROM movements WHERE reversal_of IN ({marks}) LIMIT 1", [r["id"] for r in legs]).fetchone():
            raise ApiError("This movement has already been reversed")
        new_ids = []
        for r in legs:
            note = f"Reversal of #{r['id']}" + (f": {reason}" if reason else "")
            cur = db.execute(
                """INSERT INTO movements(product_id, location_id, kind, qty, lot, reference, note, operator, reversal_of)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (r["product_id"], r["location_id"], REVERSE_KIND[r["kind"]], -r["qty"], r["lot"], r["reference"], note,
                 g.user["username"], r["id"]),
            )
            new_ids.append(cur.lastrowid)
        for r in legs:  # taking stock back out must not push the location or the lot below zero
            if -r["qty"] < 0 and (balance(db, r["product_id"], r["location_id"]) < -1e-9
                                  or (r["lot"] and balance(db, r["product_id"], r["location_id"], r["lot"]) < -1e-9)):
                raise ApiError("Cannot reverse: that stock has already been used or moved")
        # a reversed stocktake was a wrong count: leave it out of count accuracy
        db.executemany("DELETE FROM counts WHERE movement_id = ?", [(r["id"],) for r in legs])
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return jsonify(ok=True, reversal_ids=new_ids), 201


# ---------------------------------------------------------------- CSV import / export
def csv_response(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(
        "﻿" + buf.getvalue(),  # BOM so Excel opens it with correct encoding
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/export/stock.csv")
def export_stock():
    rows = []
    for s in stock_summary(get_db()):
        locs = "; ".join(f"{x['location']}: {x['qty']:g}" for x in s["locations"])
        rows.append([s["sku"], s["name"], s["category"], s["spec"], s["unit"], s["total"], s["min_stock"], s["status"], locs, s["unit_cost"] or ""])
    return csv_response("stock.csv", ["SKU", "Name", "Category", "Spec", "Unit", "On hand", "Min stock", "Status", "By location", "Unit cost"], rows)


@app.get("/api/export/movements.csv")
def export_movements():
    rows = [[r["created_at"], r["kind"], r["sku"], r["product"], r["location"], r["qty"], r["unit"], r["lot"], r["reference"], r["note"], r["operator"]]
            for r in query_movements(get_db(), 100000)]
    return csv_response("movements.csv", ["Date", "Type", "SKU", "Product", "Location", "Qty", "Unit", "Lot", "Reference", "Note", "Operator"], rows)


def insights_params():
    lead = request.args.get("lead_days", 14, type=int)
    window = request.args.get("window", 90, type=int)
    if lead is None or not 1 <= lead <= 365:
        raise ApiError("Lead time must be between 1 and 365 days")
    if window is None or not 14 <= window <= 730:
        raise ApiError("History window must be between 14 and 730 days")
    return lead, window


@app.get("/api/insights")
@cached
def get_insights():
    db = get_db()
    lead, window = insights_params()
    return jsonify(insights.compute(db, stock_summary(db), lead_days=lead, window=window))


@app.get("/api/insights/value")
@cached
def get_value_insights():
    db = get_db()
    _, window = insights_params()
    return jsonify(insights.value_and_turnover(db, stock_summary(db), window=window))


@app.get("/api/export/turnover.csv")
def export_turnover():
    db = get_db()
    _, window = insights_params()
    data = insights.value_and_turnover(db, stock_summary(db), window=window)
    rows = [[r["sku"], r["name"], r["category"], r["value_now"], r["avg_value"], r["issued_value"], r["turnover"], r["days"]]
            for r in data["products"]]
    return csv_response("turnover.csv", ["SKU", "Name", "Category", "Value now (€)", f"Average value, last {window} days (€)",
                                         f"Value issued, last {window} days (€)", "Turnover per year", "Days of inventory"], rows)


# ---------------------------------------------------------------- analytics module
def analytics_filter():
    """Shared filters: from / to (YYYY-MM-DD, default the last 90 days), category, location_id."""
    def day(key, default):
        v = request.args.get(key, "").strip()
        if not v:
            return default
        try:
            return date.fromisoformat(v)
        except ValueError:
            raise ApiError(f"{key} must be a date like 2026-01-31")
    end = day("to", date.today())
    start = day("from", end - timedelta(days=89))
    if start > end:
        raise ApiError("The start date must be before the end date")
    if (end - start).days > 1100:
        raise ApiError("Choose a period of at most 3 years")
    return {"start": start, "end": end, "category": request.args.get("category", "").strip() or None,
            "location_id": request.args.get("location_id", type=int)}


@app.get("/api/analytics/overview")
@cached
def analytics_overview():
    db = get_db()
    return jsonify(analytics.overview(db, analytics_filter(), stock_summary(db)))


@app.get("/api/analytics/product/<int:pid>")
@cached
def analytics_product(pid):
    db = get_db()
    row = next((s for s in stock_summary(db) if s["id"] == pid), None)
    if row is None:
        raise ApiError("Product not found", 404)
    f = analytics_filter()
    if not request.args.get("from"):
        f["start"] = f["end"] - timedelta(days=364)   # a product's story needs a year by default
    return jsonify(analytics.product(db, pid, f, row))


@app.get("/api/analytics/activity")
@cached
def analytics_activity():
    return jsonify(analytics.activity(get_db(), analytics_filter()))


@app.get("/api/analytics/stocktake")
@cached
def analytics_stocktake():
    return jsonify(analytics.stocktake(get_db(), analytics_filter()))


@app.get("/api/export/insights.csv")
def export_insights():
    db = get_db()
    lead, window = insights_params()
    data = insights.compute(db, stock_summary(db), lead_days=lead, window=window)
    label = {"order_now": "Order now", "watch": "Watch", "ok": "OK", "no_demand": "No demand"}
    rows = [[i["sku"], i["name"], i["unit"], label[i["status"]], i["on_hand"], i["avg_daily"], i["cover_days"],
             i["stockout_date"], i["reorder_point"], i["safety_stock"], i["order_qty"], i["forecast_30d"],
             i["trend_pct"], (i["abc"] or "") + (i["xyz"] or ""), i["history_days"]] for i in data["items"]]
    return csv_response("insights.csv", ["SKU", "Name", "Unit", "Status", "On hand", "Avg issued per day", "Days of stock left",
                                         "Expected stock-out", "Reorder point", "Safety stock", "Suggested order",
                                         "Forecast next 30 days", "Trend % (last 4 weeks)", "Class (ABC/XYZ)",
                                         "Days of history"], rows)


@app.get("/api/export/reorder.csv")
def export_reorder():
    """Everything at or below its minimum stock, with the shortfall - a shopping list for purchasing."""
    rows = [[s["sku"], s["name"], s["unit"], s["total"], s["min_stock"], round(s["min_stock"] - s["total"], 3),
             s["prodata_code"] or "", s["barcode"] or ""]
            for s in stock_summary(get_db()) if s["min_stock"] > 0 and s["total"] <= s["min_stock"]]
    return csv_response("reorder.csv", ["SKU", "Name", "Unit", "On hand", "Min stock", "Shortfall to minimum", "Prodata code", "Barcode"], rows)


def read_csv(payload):
    raw = (payload or {}).get("csv", "")
    if not raw.strip():
        raise ApiError("The file is empty")
    reader = csv.DictReader(io.StringIO(raw.lstrip("﻿")))
    reader.fieldnames = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    return reader


@app.post("/api/import/products")
def import_products():
    """Columns: sku, name, category, spec, unit, min_stock, unit_cost, barcode, prodata_code,
    material_sku, material_per_1000. Existing SKUs are updated."""
    db = get_db()
    reader = read_csv(request.get_json(force=True))
    if "sku" not in reader.fieldnames or "name" not in reader.fieldnames:
        raise ApiError("CSV needs at least the columns: sku, name")
    created = updated = 0
    errors = []
    rows = list(reader)
    db.execute("BEGIN IMMEDIATE")
    try:
        for i, row in enumerate(rows, start=2):
            try:
                old = db.execute("SELECT unit FROM products WHERE sku=?", ((row.get("sku") or "").strip(),)).fetchone()
                f = product_fields(row, old["unit"] if old else None)
                exists = old
                if exists:
                    db.execute("""UPDATE products SET name=:name,category=:category,spec=:spec,unit=:unit,min_stock=:min_stock,
                                  unit_cost=:unit_cost,prodata_code=:prodata_code,barcode=:barcode WHERE sku=:sku""", f)
                    updated += 1
                else:
                    db.execute("""INSERT INTO products(sku,name,category,spec,unit,min_stock,unit_cost,prodata_code,barcode)
                                  VALUES (:sku,:name,:category,:spec,:unit,:min_stock,:unit_cost,:prodata_code,:barcode)""", f)
                    created += 1
            except ApiError as e:
                errors.append(f"Row {i}: {e.message}")
            except sqlite3.IntegrityError as e:
                errors.append(f"Row {i}: {duplicate_message(e, f)}")
        if not errors and "material_sku" in reader.fieldnames:
            for i, row in enumerate(rows, start=2):
                try:
                    pid = db.execute("SELECT id FROM products WHERE sku=?", (text(row, "sku"),)).fetchone()["id"]
                    set_material(db, pid, row)
                except ApiError as e:
                    errors.append(f"Row {i}: {e.message}")
        db.execute("COMMIT" if not errors else "ROLLBACK")
    except Exception:
        db.execute("ROLLBACK")
        raise
    if errors:
        return jsonify(error="Nothing was imported. " + " | ".join(errors[:5])), 400
    return jsonify(created=created, updated=updated)


@app.post("/api/import/opening-stock")
def import_opening_stock():
    """Columns: sku, location, qty, lot (optional). Books each row as an IN movement with reference OPENING."""
    db = get_db()
    d = request.get_json(force=True)
    operator = g.user["username"]
    reader = read_csv(d)
    if not {"sku", "location", "qty"} <= set(reader.fieldnames):
        raise ApiError("CSV needs the columns: sku, location, qty (lot is optional)")
    count = 0
    errors = []
    db.execute("BEGIN IMMEDIATE")
    try:
        for i, row in enumerate(reader, start=2):
            try:
                sku, loc_name = (row.get("sku") or "").strip(), (row.get("location") or "").strip()
                p = db.execute("SELECT id FROM products WHERE sku=? AND active=1", (sku,)).fetchone()
                if not p:
                    raise ApiError(f"unknown SKU '{sku}'")
                if not loc_name:
                    raise ApiError("location is empty")
                qty = num(row.get("qty"), "qty", 0.001)
                check_whole(db, p["id"], qty, "qty")
                loc = db.execute("SELECT id FROM locations WHERE name=?", (loc_name,)).fetchone()
                loc_id = loc["id"] if loc else db.execute("INSERT INTO locations(name) VALUES (?)", (loc_name,)).lastrowid
                insert_movement(db, p["id"], loc_id, "IN", qty, (row.get("lot") or "").strip(), "OPENING", "Opening stock import", operator)
                count += 1
            except ApiError as e:
                errors.append(f"Row {i}: {e.message}")
        db.execute("COMMIT" if not errors else "ROLLBACK")
    except Exception:
        db.execute("ROLLBACK")
        raise
    if errors:
        return jsonify(error="Nothing was imported. " + " | ".join(errors[:5])), 400
    return jsonify(imported=count)


# ---------------------------------------------------------------- Prodata (optional)
@app.get("/api/prodata/status")
def prodata_status():
    db = get_db()
    pending = db.execute("SELECT COUNT(*) FROM movements WHERE prodata_synced=0").fetchone()[0]
    return jsonify({**prodata.status(), "pending_movements": pending})


@app.post("/api/prodata/sync")
def prodata_sync():
    try:
        result = prodata.sync(get_db())
    except prodata.ProdataNotConfigured as e:
        raise ApiError(str(e), 501)
    return jsonify(result)


@app.get("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return "ok"


init_db()
auth.init(app, get_db, connect, ApiError)  # login, roles, user management; guards every /api route

if __name__ == "__main__":
    # Local use. 127.0.0.1 = this PC only; set VINEX_HOST=0.0.0.0 to let other PCs/phones on the LAN open it.
    # Hosted use (Railway etc.) runs `gunicorn app:app` - see Procfile.
    # Hosting platforms (Railway etc.) set PORT and need the app reachable from outside the container.
    default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    app.run(host=os.environ.get("VINEX_HOST", default_host), port=int(os.environ.get("PORT") or os.environ.get("VINEX_PORT", "5000")))
