"""Vinex Fasteners - Stock Management (prototype).

Stock is an append-only ledger of movements; quantities on hand are always
computed from the ledger, so every number can be traced back to who did what.
"""
import csv
import io
import os
import sqlite3
import uuid

from flask import Flask, Response, g, jsonify, request, send_from_directory

import auth
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
    return db


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
    if db.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 0:
        db.execute("INSERT INTO locations(name) VALUES ('Main warehouse')")
    db.close()


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify(error=e.message), e.status


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


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------- products
@app.get("/api/products")
def list_products():
    db = get_db()
    include_inactive = request.args.get("all") == "1"
    q = "SELECT * FROM products" + ("" if include_inactive else " WHERE active=1") + " ORDER BY sku"
    return jsonify([dict(r) for r in db.execute(q)])


def product_fields(payload):
    return {
        "sku": text(payload, "sku", required=True),
        "name": text(payload, "name", required=True),
        "category": text(payload, "category"),
        "spec": text(payload, "spec"),
        "unit": text(payload, "unit", default="pcs") or "pcs",
        "min_stock": num(payload.get("min_stock", 0) or 0, "min_stock", 0),
        "unit_cost": num(payload.get("unit_cost"), "unit_cost", 0, required=False),
        "prodata_code": text(payload, "prodata_code") or None,
    }


@app.post("/api/products")
def create_product():
    db = get_db()
    f = product_fields(request.get_json(force=True) or {})
    try:
        cur = db.execute(
            """INSERT INTO products(sku,name,category,spec,unit,min_stock,unit_cost,prodata_code)
               VALUES (:sku,:name,:category,:spec,:unit,:min_stock,:unit_cost,:prodata_code)""",
            f,
        )
    except sqlite3.IntegrityError:
        raise ApiError(f"SKU '{f['sku']}' already exists", 409)
    return jsonify(id=cur.lastrowid), 201


@app.put("/api/products/<int:pid>")
def update_product(pid):
    db = get_db()
    payload = request.get_json(force=True) or {}
    f = product_fields(payload)
    f["id"] = pid
    f["active"] = 1 if payload.get("active", True) else 0
    try:
        cur = db.execute(
            """UPDATE products SET sku=:sku,name=:name,category=:category,spec=:spec,unit=:unit,
               min_stock=:min_stock,unit_cost=:unit_cost,prodata_code=:prodata_code,active=:active
               WHERE id=:id""",
            f,
        )
    except sqlite3.IntegrityError:
        raise ApiError(f"SKU '{f['sku']}' already exists", 409)
    if cur.rowcount == 0:
        raise ApiError("Product not found", 404)
    return jsonify(ok=True)


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
    )


# ---------------------------------------------------------------- movements
MOVEMENT_SELECT = """SELECT m.id, m.created_at, m.kind, m.qty, m.lot, m.reference, m.note, m.operator,
       m.transfer_id, m.prodata_synced, p.sku, p.name AS product, p.unit, l.name AS location
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
            insert_movement(db, pid, lid, "IN", qty, lot, reference, note, operator)

        elif kind == "OUT":
            qty = num(d.get("qty"), "Quantity", 0.001)
            have = balance(db, pid, lid, lot)
            if qty > have + 1e-9:
                raise ApiError(f"Not enough stock{' in lot ' + lot if lot else ''}: {have:g} available, {qty:g} requested")
            insert_movement(db, pid, lid, "OUT", -qty, lot, reference, note, operator)

        elif kind == "TRANSFER":
            qty = num(d.get("qty"), "Quantity", 0.001)
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
            have = balance(db, pid, lid, lot)
            delta = round(counted - have, 3)
            if abs(delta) < 0.0005:
                db.execute("COMMIT")
                return jsonify(ok=True, changed=False, message="Count matches the system - no adjustment needed")
            insert_movement(db, pid, lid, "ADJUST", delta, lot, reference,
                            (note + " | " if note else "") + f"Stocktake: counted {counted:g}, system had {have:g}", operator)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return jsonify(ok=True, changed=True), 201


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


def read_csv(payload):
    raw = (payload or {}).get("csv", "")
    if not raw.strip():
        raise ApiError("The file is empty")
    reader = csv.DictReader(io.StringIO(raw.lstrip("﻿")))
    reader.fieldnames = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    return reader


@app.post("/api/import/products")
def import_products():
    """Columns: sku, name, category, spec, unit, min_stock, unit_cost, prodata_code. Existing SKUs are updated."""
    db = get_db()
    reader = read_csv(request.get_json(force=True))
    if "sku" not in reader.fieldnames or "name" not in reader.fieldnames:
        raise ApiError("CSV needs at least the columns: sku, name")
    created = updated = 0
    errors = []
    db.execute("BEGIN IMMEDIATE")
    try:
        for i, row in enumerate(reader, start=2):
            try:
                f = product_fields(row)
                exists = db.execute("SELECT id FROM products WHERE sku=?", (f["sku"],)).fetchone()
                if exists:
                    db.execute("""UPDATE products SET name=:name,category=:category,spec=:spec,unit=:unit,min_stock=:min_stock,
                                  unit_cost=:unit_cost,prodata_code=:prodata_code WHERE sku=:sku""", f)
                    updated += 1
                else:
                    db.execute("""INSERT INTO products(sku,name,category,spec,unit,min_stock,unit_cost,prodata_code)
                                  VALUES (:sku,:name,:category,:spec,:unit,:min_stock,:unit_cost,:prodata_code)""", f)
                    created += 1
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
    app.run(host=os.environ.get("VINEX_HOST", "127.0.0.1"), port=int(os.environ.get("PORT") or os.environ.get("VINEX_PORT", "5000")))
