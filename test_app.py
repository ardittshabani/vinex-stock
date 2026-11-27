"""Run with:  python -m unittest test_app -v   (uses a throwaway database)"""
import os
import tempfile
import unittest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["VINEX_STOCK_DB"] = _tmp.name
os.environ["VINEX_ADMIN_PASSWORD"] = "test-admin-pw"

import app as stock  # noqa: E402  (must come after the env vars)
import auth  # noqa: E402

ADMIN_PW = "test-admin-pw"


def client_as(username, password):
    c = stock.app.test_client()
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.get_json()
    return c


class StockTests(unittest.TestCase):
    def setUp(self):
        db = stock.connect()
        for t in ("counts", "movements", "products", "locations"):
            db.execute(f"DELETE FROM {t}")
        db.execute("INSERT INTO locations(name) VALUES ('Main'),('Packing')")
        db.close()
        self.c = client_as("admin", ADMIN_PW)
        self.pid = self.c.post("/api/products", json={"sku": "B1", "name": "Bolt", "min_stock": 100}).get_json()["id"]
        self.main, self.pack = [l["id"] for l in self.c.get("/api/locations").get_json()]

    def move(self, kind, qty=None, loc=None, **kw):
        body = {"kind": kind, "product_id": self.pid, "location_id": loc or self.main, **kw}
        if qty is not None:
            body["qty"] = qty
        return self.c.post("/api/movements", json=body)

    def total(self):
        return self.c.get("/api/stock").get_json()[0]["total"]

    def test_receive_and_issue(self):
        self.assertEqual(self.move("IN", 500, lot="L1").status_code, 201)
        self.assertEqual(self.move("OUT", 120, lot="L1").status_code, 201)
        self.assertEqual(self.total(), 380)

    def test_cannot_issue_more_than_on_hand(self):
        self.move("IN", 50)
        r = self.move("OUT", 51)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Stok i pamjaftueshëm", r.get_json()["error"])
        self.assertEqual(self.total(), 50)

    def test_lot_is_checked_separately(self):
        self.move("IN", 100, lot="A")
        self.move("IN", 100, lot="B")
        self.assertEqual(self.move("OUT", 150, lot="A").status_code, 400)  # only 100 in lot A
        self.assertEqual(self.move("OUT", 150).status_code, 201)           # no lot -> whole location

    def test_transfer_moves_stock_and_keeps_total(self):
        self.move("IN", 300, lot="L1")
        r = self.move("TRANSFER", 100, lot="L1", to_location_id=self.pack)
        self.assertEqual(r.status_code, 201)
        locs = {l["location"]: l["qty"] for l in self.c.get("/api/stock").get_json()[0]["locations"]}
        self.assertEqual(locs, {"Main": 200, "Packing": 100})
        self.assertEqual(self.total(), 300)

    def test_transfer_same_location_rejected(self):
        self.move("IN", 10)
        self.assertEqual(self.move("TRANSFER", 5, to_location_id=self.main).status_code, 400)

    def test_stocktake_adjusts_to_counted(self):
        self.move("IN", 200)
        r = self.c.post("/api/movements", json={"kind": "ADJUST", "product_id": self.pid, "location_id": self.main, "counted": 180})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.total(), 180)
        again = self.c.post("/api/movements", json={"kind": "ADJUST", "product_id": self.pid, "location_id": self.main, "counted": 180})
        self.assertFalse(again.get_json()["changed"])

    def test_low_stock_status(self):
        self.assertEqual(self.c.get("/api/stock").get_json()[0]["status"], "out")
        self.move("IN", 90)
        self.assertEqual(self.c.get("/api/stock").get_json()[0]["status"], "low")
        self.move("IN", 100)
        self.assertEqual(self.c.get("/api/stock").get_json()[0]["status"], "ok")

    def test_bad_input(self):
        self.assertEqual(self.move("IN", 0).status_code, 400)
        self.assertEqual(self.move("IN", -5).status_code, 400)
        self.assertEqual(self.move("IN", "abc").status_code, 400)
        self.assertEqual(self.move("NOPE", 1).status_code, 400)
        self.assertEqual(self.c.post("/api/movements", json={"kind": "IN", "product_id": 999, "location_id": self.main, "qty": 1}).status_code, 404)

    def test_duplicate_sku(self):
        self.assertEqual(self.c.post("/api/products", json={"sku": "B1", "name": "Dup"}).status_code, 409)

    def test_delete_product_without_history(self):
        pid = self.c.post("/api/products", json={"sku": "TMP", "name": "Temp"}).get_json()["id"]
        r = self.c.delete(f"/api/products/{pid}", json={})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("TMP", [p["sku"] for p in self.c.get("/api/products?all=1").get_json()])
        self.assertEqual(self.c.delete(f"/api/products/{pid}", json={}).status_code, 404)

    def test_delete_product_with_movements_needs_confirmation_flag(self):
        self.move("IN", 10)
        self.move("OUT", 3)
        r = self.c.delete(f"/api/products/{self.pid}", json={})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["movements"], 2)
        self.assertEqual(len(self.c.get("/api/movements").get_json()), 2)  # nothing was touched
        r = self.c.delete(f"/api/products/{self.pid}", json={"with_history": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["movements_deleted"], 2)
        self.assertEqual(self.c.get("/api/movements").get_json(), [])
        self.assertEqual(self.c.get("/api/products?all=1").get_json(), [])

    def test_delete_product_removes_its_stocktake_counts_only(self):
        other = self.c.post("/api/products", json={"sku": "B2", "name": "Other"}).get_json()["id"]
        self.move("IN", 10)
        self.move("ADJUST", 8)
        self.c.post("/api/movements", json={"kind": "IN", "product_id": other, "location_id": self.main, "qty": 5})
        self.assertEqual(self.c.delete(f"/api/products/{self.pid}", json={"with_history": True}).status_code, 200)
        db = stock.connect()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM counts WHERE product_id=?", (self.pid,)).fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM movements WHERE product_id=?", (other,)).fetchone()[0], 1)
        db.close()

    def test_cannot_delete_raw_material_still_in_use(self):
        rm = self.c.post("/api/products", json={"sku": "RM", "name": "Rod", "unit": "kg"}).get_json()["id"]
        self.c.put(f"/api/products/{self.pid}", json={"sku": "B1", "name": "Bolt", "material_sku": "RM", "material_per_1000": 20})
        r = self.c.delete(f"/api/products/{rm}", json={"with_history": True})
        self.assertEqual(r.status_code, 409)
        self.assertIn("B1", r.get_json()["error"])

    def test_only_admin_can_delete_products(self):
        self.c.post("/api/users", json={"username": "kim", "password": "password-kim", "role": "staff"})
        kim = client_as("kim", "password-kim")
        self.assertEqual(kim.delete(f"/api/products/{self.pid}", json={"with_history": True}).status_code, 403)
        self.assertEqual(len(self.c.get("/api/products").get_json()), 1)

    def test_deactivate_location_with_stock_blocked(self):
        self.move("IN", 10)
        r = self.c.put(f"/api/locations/{self.main}", json={"name": "Main", "active": False})
        self.assertEqual(r.status_code, 400)

    def test_import_products_and_opening_stock(self):
        r = self.c.post("/api/import/products", json={"csv": "sku,name,unit,min_stock\nN1,Nut,pcs,50\nB1,Bolt renamed,pcs,100\n"})
        self.assertEqual(r.get_json(), {"created": 1, "updated": 1})
        r = self.c.post("/api/import/opening-stock", json={"csv": "sku,location,qty,lot\nN1,Rack 7,400,X1\nB1,Main,25,\n"})
        self.assertEqual(r.get_json(), {"imported": 2})
        by_sku = {s["sku"]: s["total"] for s in self.c.get("/api/stock").get_json()}
        self.assertEqual(by_sku, {"B1": 25, "N1": 400})

    def test_bad_import_is_all_or_nothing(self):
        r = self.c.post("/api/import/opening-stock", json={"csv": "sku,location,qty\nB1,Main,10\nNOPE,Main,5\n"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.total(), 0)

    def test_exports_and_dashboard(self):
        self.move("IN", 40)
        self.assertIn("B1", self.c.get("/api/export/stock.csv").get_data(as_text=True))
        self.assertIn("Pranim", self.c.get("/api/export/movements.csv").get_data(as_text=True))
        d = self.c.get("/api/dashboard").get_json()
        self.assertEqual((d["products"], d["low_count"], d["movements_today"]), (1, 1, 1))

    def test_barcode_lookup_and_uniqueness(self):
        self.c.put(f"/api/products/{self.pid}", json={"sku": "B1", "name": "Bolt", "min_stock": 100, "barcode": "5901234123457"})
        self.assertEqual(self.c.get("/api/lookup?code=5901234123457").get_json()["sku"], "B1")
        self.assertEqual(self.c.get("/api/lookup?code=b1").get_json()["id"], self.pid)  # SKU, case-insensitive
        self.assertEqual(self.c.get("/api/lookup?code=nope").status_code, 404)
        r = self.c.post("/api/products", json={"sku": "B2", "name": "Other", "barcode": "5901234123457"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("Barkodi", r.get_json()["error"])
        self.assertEqual(self.c.post("/api/products", json={"sku": "B3", "name": "x"}).status_code, 201)  # empty barcodes don't clash
        self.assertEqual(self.c.post("/api/products", json={"sku": "B4", "name": "y"}).status_code, 201)

    def test_reverse_receipt(self):
        self.move("IN", 100, lot="L1")
        mid = self.c.get("/api/movements").get_json()[0]["id"]
        self.assertEqual(self.c.post(f"/api/movements/{mid}/reverse", json={"note": "typo"}).status_code, 201)
        self.assertEqual(self.total(), 0)
        self.assertEqual(self.c.post(f"/api/movements/{mid}/reverse", json={}).status_code, 400)  # already reversed
        rows = self.c.get("/api/movements").get_json()
        self.assertTrue(rows[1]["reversed"])
        self.assertEqual(rows[0]["reversal_of"], mid)
        self.assertIn("typo", rows[0]["note"])
        self.assertEqual(self.c.post(f"/api/movements/{rows[0]['id']}/reverse", json={}).status_code, 400)  # a reversal can't be reversed

    def test_cannot_reverse_receipt_that_was_already_used(self):
        self.move("IN", 100)
        mid = self.c.get("/api/movements").get_json()[0]["id"]
        self.move("OUT", 80)
        self.assertEqual(self.c.post(f"/api/movements/{mid}/reverse", json={}).status_code, 400)
        self.assertEqual(self.total(), 20)

    def test_reverse_transfer_restores_both_locations(self):
        self.move("IN", 100)
        self.move("TRANSFER", 40, to_location_id=self.pack)
        leg = next(r for r in self.c.get("/api/movements").get_json() if r["kind"] == "TRANSFER_OUT")
        self.assertEqual(self.c.post(f"/api/movements/{leg['id']}/reverse", json={}).status_code, 201)
        locs = {l["location"]: l["qty"] for l in self.c.get("/api/stock").get_json()[0]["locations"]}
        self.assertEqual(locs, {"Main": 100})

    def test_reverse_lot_receipt_cannot_drive_location_negative(self):
        self.move("IN", 100, lot="X")
        mid = self.c.get("/api/movements").get_json()[0]["id"]
        self.move("OUT", 100)  # no lot given: drains the location
        self.assertEqual(self.c.post(f"/api/movements/{mid}/reverse", json={}).status_code, 400)

    def test_reorder_list_and_dashboard_activity(self):
        self.move("IN", 40)  # minimum is 100
        csv_text = self.c.get("/api/export/reorder.csv").get_data(as_text=True)
        self.assertIn("B1", csv_text)
        self.assertIn(",60", csv_text)  # shortfall
        act = self.c.get("/api/dashboard").get_json()["activity"]
        self.assertEqual(len(act), 14)
        self.assertEqual(act[-1]["in"], 1)

    def test_prodata_not_configured(self):
        self.assertEqual(self.c.post("/api/prodata/sync", json={}).status_code, 501)
        self.assertFalse(self.c.get("/api/prodata/status").get_json()["configured"])


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = client_as("admin", ADMIN_PW)
        for name, role in (("sam", "staff"), ("vic", "viewer")):
            cls.admin.post("/api/users", json={"username": name, "password": "password-" + name, "role": role})
        db = stock.connect()
        db.execute("DELETE FROM counts"); db.execute("DELETE FROM movements"); db.execute("DELETE FROM products"); db.execute("DELETE FROM locations")
        db.execute("INSERT INTO locations(name) VALUES ('Main')")
        db.close()
        cls.pid = cls.admin.post("/api/products", json={"sku": "AUTH1", "name": "Auth bolt"}).get_json()["id"]
        cls.lid = cls.admin.get("/api/locations").get_json()[0]["id"]

    def test_everything_requires_login(self):
        anon = stock.app.test_client()
        for path in ("/api/stock", "/api/products", "/api/dashboard", "/api/export/stock.csv", "/api/users", "/api/me"):
            self.assertEqual(anon.get(path).status_code, 401, path)
        self.assertEqual(anon.post("/api/movements", json={}).status_code, 401)
        self.assertEqual(anon.get("/healthz").status_code, 200)
        self.assertEqual(anon.get("/").status_code, 200)  # the page itself shows the login form

    def test_wrong_password_and_unknown_user(self):
        c = stock.app.test_client()
        self.assertEqual(c.post("/api/login", json={"username": "admin", "password": "nope"}).status_code, 401)
        self.assertEqual(c.post("/api/login", json={"username": "ghost", "password": "nope"}).status_code, 401)

    def test_staff_can_book_movements_but_not_edit_catalogue(self):
        sam = client_as("sam", "password-sam")
        r = sam.post("/api/movements", json={"kind": "IN", "product_id": self.pid, "location_id": self.lid, "qty": 5})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(sam.get("/api/movements").get_json()[0]["operator"], "sam")  # operator comes from the login
        self.assertEqual(sam.post("/api/products", json={"sku": "X", "name": "X"}).status_code, 403)
        self.assertEqual(sam.post("/api/locations", json={"name": "X"}).status_code, 403)
        self.assertEqual(sam.post("/api/import/products", json={"csv": "sku,name\nX,X"}).status_code, 403)
        self.assertEqual(sam.get("/api/users").status_code, 403)
        self.assertEqual(sam.post("/api/movements/1/reverse", json={}).status_code, 403)  # reversing is admin-only
        self.assertEqual(sam.post("/api/prodata/sync", json={}).status_code, 403)

    def test_operator_in_request_is_ignored(self):
        sam = client_as("sam", "password-sam")
        sam.post("/api/movements", json={"kind": "IN", "product_id": self.pid, "location_id": self.lid, "qty": 1, "operator": "someone else"})
        self.assertEqual(sam.get("/api/movements").get_json()[0]["operator"], "sam")

    def test_viewer_is_read_only(self):
        vic = client_as("vic", "password-vic")
        self.assertEqual(vic.get("/api/stock").status_code, 200)
        self.assertEqual(vic.post("/api/movements", json={"kind": "IN", "product_id": self.pid, "location_id": self.lid, "qty": 1}).status_code, 403)

    def test_json_required_for_writes(self):
        r = self.admin.post("/api/products", data="sku=A&name=B", content_type="application/x-www-form-urlencoded")
        self.assertEqual(r.status_code, 415)

    def test_change_own_password_signs_out_other_sessions(self):
        self.admin.post("/api/users", json={"username": "tmp", "password": "first-password", "role": "staff"})
        a, b = client_as("tmp", "first-password"), client_as("tmp", "first-password")
        self.assertEqual(a.post("/api/me/password", json={"current": "wrong", "new": "second-password"}).status_code, 400)
        self.assertEqual(a.post("/api/me/password", json={"current": "first-password", "new": "short"}).status_code, 400)
        self.assertEqual(a.post("/api/me/password", json={"current": "first-password", "new": "second-password"}).status_code, 200)
        self.assertEqual(a.get("/api/me").status_code, 200)   # this session stays signed in
        self.assertEqual(b.get("/api/me").status_code, 401)   # the other one is signed out
        client_as("tmp", "second-password")

    def test_deactivated_user_is_signed_out_and_cannot_log_in(self):
        uid = self.admin.post("/api/users", json={"username": "gone", "password": "gone-password", "role": "staff"}).get_json()["id"]
        c = client_as("gone", "gone-password")
        self.admin.put(f"/api/users/{uid}", json={"active": False})
        self.assertEqual(c.get("/api/me").status_code, 401)
        r = stock.app.test_client().post("/api/login", json={"username": "gone", "password": "gone-password"})
        self.assertEqual(r.status_code, 401)

    def test_admin_reset_of_password(self):
        uid = self.admin.post("/api/users", json={"username": "reset1", "password": "old-password", "role": "staff"}).get_json()["id"]
        old = client_as("reset1", "old-password")
        self.admin.put(f"/api/users/{uid}", json={"password": "new-password"})
        self.assertEqual(old.get("/api/me").status_code, 401)
        client_as("reset1", "new-password")

    def test_cannot_remove_last_admin(self):
        me = self.admin.get("/api/me").get_json()["id"]
        self.assertEqual(self.admin.put(f"/api/users/{me}", json={"role": "staff"}).status_code, 400)
        self.assertEqual(self.admin.put(f"/api/users/{me}", json={"active": False}).status_code, 400)

    def test_user_validation(self):
        self.assertEqual(self.admin.post("/api/users", json={"username": "a", "password": "long-enough-pw", "role": "staff"}).status_code, 400)
        self.assertEqual(self.admin.post("/api/users", json={"username": "okname", "password": "short", "role": "staff"}).status_code, 400)
        self.assertEqual(self.admin.post("/api/users", json={"username": "okname", "password": "long-enough-pw", "role": "boss"}).status_code, 400)
        self.assertEqual(self.admin.post("/api/users", json={"username": "SAM", "password": "long-enough-pw", "role": "staff"}).status_code, 409)  # case-insensitive

    def test_lockout_after_repeated_failures(self):
        c = stock.app.test_client()
        auth._fails.clear()
        for _ in range(auth.MAX_FAILS):
            self.assertEqual(c.post("/api/login", json={"username": "vic", "password": "bad"}).status_code, 401)
        self.assertEqual(c.post("/api/login", json={"username": "vic", "password": "password-vic"}).status_code, 429)  # even the right one
        auth._fails.clear()
        self.assertEqual(c.post("/api/login", json={"username": "vic", "password": "password-vic"}).status_code, 200)


class MigrationTests(unittest.TestCase):
    def test_old_database_is_upgraded(self):
        import sqlite3
        path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE products(id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '', spec TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT 'pcs',
                min_stock REAL NOT NULL DEFAULT 0, unit_cost REAL, prodata_code TEXT, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE movements(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, location_id INTEGER NOT NULL,
                kind TEXT NOT NULL, qty REAL NOT NULL, lot TEXT NOT NULL DEFAULT '', reference TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '', operator TEXT NOT NULL DEFAULT '', transfer_id TEXT,
                prodata_synced INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
            INSERT INTO products(sku, name) VALUES ('OLD1', 'Old product');""")
        old.commit()
        old.close()
        original = stock.DB_PATH
        stock.DB_PATH = path
        try:
            stock.init_db()
            stock.init_db()  # must be safe to repeat
            db = stock.connect()
            self.assertIn("barcode", {r["name"] for r in db.execute("PRAGMA table_info(products)")})
            self.assertIn("reversal_of", {r["name"] for r in db.execute("PRAGMA table_info(movements)")})
            self.assertEqual(db.execute("SELECT sku FROM products").fetchone()["sku"], "OLD1")
            db.close()
        finally:
            stock.DB_PATH = original


if __name__ == "__main__":
    unittest.main()


class InsightsTests(unittest.TestCase):
    """Analytics on a ledger with known demand."""

    def setUp(self):
        from datetime import date, timedelta
        import insights
        self.insights, self.today = insights, date.today()
        db = stock.connect()
        for t in ("counts", "movements", "products", "locations"):
            db.execute(f"DELETE FROM {t}")
        db.execute("INSERT INTO locations(name) VALUES ('Main')")
        loc = db.execute("SELECT id FROM locations").fetchone()[0]
        rows = [("STEADY", 1000, 1.0), ("SLOW", 0, 2.0), ("SPIKY", 0, 0.5)]
        self.ids = {}
        for sku, mn, cost in rows:
            self.ids[sku] = db.execute("INSERT INTO products(sku,name,min_stock,unit_cost) VALUES (?,?,?,?)", (sku, sku, mn, cost)).lastrowid
        start = self.today - timedelta(days=89)

        def mv(sku, kind, qty, d):
            db.execute("INSERT INTO movements(product_id,location_id,kind,qty,created_at,operator) VALUES (?,?,?,?,?,?)",
                       (self.ids[sku], loc, kind, qty, f"{d} 10:00:00", "t"))
        mv("STEADY", "IN", 20000, start)
        mv("SLOW", "IN", 500, start)
        mv("SPIKY", "IN", 10000, start)
        for i in range(90):                      # 100 a day, every day
            mv("STEADY", "OUT", -100, start + timedelta(days=i))
        for i in range(0, 90, 3):                # 10 every third day, one huge issue
            mv("SPIKY", "OUT", -(500 if i == 45 else 10), start + timedelta(days=i))
        db.commit()
        db.close()
        self.c = client_as("admin", ADMIN_PW)

    def item(self, data, sku):
        return next(i for i in data["items"] if i["sku"] == sku)

    def test_steady_demand_numbers(self):
        d = self.c.get("/api/insights?lead_days=10&window=90").get_json()
        s = self.item(d, "STEADY")
        self.assertEqual(s["on_hand"], 11000)
        self.assertEqual(s["avg_daily"], 100)
        self.assertEqual(s["cover_days"], 110)
        self.assertEqual(s["safety_stock"], 0)          # no variation -> no safety stock
        self.assertEqual(s["reorder_point"], 1000)      # 100/day * 10 days
        self.assertEqual(s["status"], "ok")
        self.assertEqual(s["xyz"], "X")
        self.assertEqual(s["abc"], "A")
        self.assertEqual(s["forecast_30d"], 3000)
        self.assertEqual(len(s["spark"]), 30)

    def test_steady_demand_is_smooth_and_uses_ses(self):
        s = self.item(self.c.get("/api/insights").get_json(), "STEADY")
        self.assertEqual((s["pattern"], s["forecast_method"]), ("smooth", "ses"))
        self.assertEqual(s["adi"], 1.0)

    def test_new_product_with_little_history_is_not_classified(self):
        from datetime import date, timedelta
        db = stock.connect()
        pid = db.execute("INSERT INTO products(sku, name, unit) VALUES ('NEWBIE', 'New', 'pcs')").lastrowid
        lid = db.execute("SELECT id FROM locations LIMIT 1").fetchone()["id"]
        for d in (27, 20, 13, 6):  # one issue a week for 4 full weeks
            db.execute("INSERT INTO movements(product_id, location_id, kind, qty, created_at) VALUES (?,?,?,?,?)",
                       (pid, lid, "OUT", -10, (date.today() - timedelta(days=d)).isoformat() + " 10:00:00"))
        db.commit()
        db.close()
        n = self.item(self.c.get("/api/insights").get_json(), "NEWBIE")
        self.assertIsNone(n["pattern"])
        self.assertEqual(n["forecast_method"], "average")
        self.assertIsNotNone(n["forecast_30d"])

    def test_order_now_and_quantity(self):
        d = self.c.get("/api/insights?lead_days=120&window=90").get_json()
        s = self.item(d, "STEADY")
        self.assertEqual(s["status"], "order_now")      # 11,000 on hand < 12,000 needed over the lead time
        self.assertEqual(s["order_qty"], 4000)          # up to 12,000 + 30 days (3,000), rounded up

    def test_slow_stock_and_no_demand(self):
        d = self.c.get("/api/insights").get_json()
        self.assertEqual([r["sku"] for r in d["slow"]], ["SLOW"])
        self.assertEqual(d["slow"][0]["value_on_hand"], 1000)
        self.assertEqual(self.item(d, "SLOW")["status"], "no_demand")

    def test_unusual_issue_is_flagged(self):
        d = self.c.get("/api/insights").get_json()
        flagged = [a for a in d["anomalies"] if a["sku"] == "SPIKY"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["qty"], 500)

    def test_reversed_issue_is_not_demand(self):
        mid = stock.connect().execute("SELECT MAX(id) FROM movements WHERE kind='OUT' AND product_id=?", (self.ids["STEADY"],)).fetchone()[0]
        self.assertEqual(self.c.post(f"/api/movements/{mid}/reverse", json={}).status_code, 201)
        s = self.item(self.c.get("/api/insights?window=90").get_json(), "STEADY")
        self.assertLess(s["avg_daily"], 100)

    def test_bad_params_and_csv(self):
        self.assertEqual(self.c.get("/api/insights?lead_days=0").status_code, 400)
        r = self.c.get("/api/export/insights.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Porosia e sugjeruar", r.get_data(as_text=True))

    def test_round_up(self):
        r = self.insights.round_up
        self.assertEqual([r(0), r(7.2), r(141), r(1201), r(12001)], [0, 8, 150, 1300, 13000])


class MaterialTests(unittest.TestCase):
    """Raw material requirements from finished goods."""

    def setUp(self):
        from datetime import date, timedelta
        db = stock.connect()
        for t in ("counts", "movements", "products", "locations"):
            db.execute(f"DELETE FROM {t}")
        db.execute("INSERT INTO locations(name) VALUES ('Main')")
        db.close()
        self.c = client_as("admin", ADMIN_PW)
        p = lambda **kw: self.c.post("/api/products", json=kw)
        self.wire = p(sku="WIRE", name="Wire rod", unit="kg").get_json()["id"]
        r = p(sku="BOLT", name="Bolt", material_sku="wire", material_per_1000=20)
        self.assertEqual(r.status_code, 201, r.get_json())
        self.bolt = r.get_json()["id"]
        db = stock.connect()
        loc = db.execute("SELECT id FROM locations").fetchone()[0]
        start = date.today() - timedelta(days=89)
        ins = lambda pid, kind, q, d: db.execute(
            "INSERT INTO movements(product_id,location_id,kind,qty,created_at,operator) VALUES (?,?,?,?,?,'t')", (pid, loc, kind, q, f"{d} 10:00:00"))
        ins(self.wire, "IN", 500, start)
        ins(self.bolt, "IN", 100000, start)
        for i in range(90):                                   # 1,000 bolts a day -> 20 kg wire a day
            ins(self.bolt, "OUT", -1000, start + timedelta(days=i))
        db.commit()
        db.close()

    def test_link_saved_and_validated(self):
        prod = next(x for x in self.c.get("/api/products").get_json() if x["sku"] == "BOLT")
        self.assertEqual((prod["material_id"], prod["material_per_1000"]), (self.wire, 20))
        bad = lambda **kw: self.c.put(f"/api/products/{self.bolt}", json={"sku": "BOLT", "name": "Bolt", **kw}).status_code
        self.assertEqual(bad(material_sku="NOPE", material_per_1000=5), 400)
        self.assertEqual(bad(material_sku="BOLT", material_per_1000=5), 400)
        self.assertEqual(bad(material_sku="WIRE", material_per_1000=0), 400)
        self.assertEqual(self.c.put(f"/api/products/{self.wire}", json={"sku": "WIRE", "name": "W", "material_sku": "BOLT", "material_per_1000": 1}).status_code, 400)
        self.assertEqual(bad(), 200)   # no material field -> link kept
        prod = next(x for x in self.c.get("/api/products").get_json() if x["sku"] == "BOLT")
        self.assertEqual(prod["material_id"], self.wire)
        self.assertEqual(bad(material_sku=""), 200)   # empty -> unlinked
        prod = next(x for x in self.c.get("/api/products").get_json() if x["sku"] == "BOLT")
        self.assertIsNone(prod["material_id"])

    def test_requirement_numbers(self):
        d = self.c.get("/api/insights?lead_days=5&window=90").get_json()
        m = d["materials"][0]
        self.assertEqual(m["sku"], "WIRE")
        self.assertEqual(m["need_per_day"], 20)
        self.assertEqual(m["need_30d"], 600)
        self.assertEqual(m["need_now"], 0)            # 10,000 bolts on hand cover the 5-day lead time
        self.assertEqual(m["cover_days"], 25)          # 500 kg / 20 kg a day
        self.assertEqual(m["status"], "watch")         # 500 < 100 + 600
        d = self.c.get("/api/insights?lead_days=30&window=90").get_json()
        m = d["materials"][0]
        self.assertEqual(m["need_now"], 1000)          # make 50,000 bolts (up to 30+30 days) x 20 kg/1000
        self.assertEqual(m["status"], "order_now")
        self.assertEqual(m["order_qty"], 1700)         # 1000 + 20*30 + 20*30 - 500 = 1700
        self.assertEqual(d["summary"]["materials_short"], 1)

    def test_csv_import_links(self):
        csv = "sku,name,unit,material_sku,material_per_1000\nNUT,Nut,pcs,ROD,5\nROD,Rod,kg,,\n"
        r = self.c.post("/api/import/products", json={"csv": csv})
        self.assertEqual(r.status_code, 200, r.get_json())
        prods = {x["sku"]: x for x in self.c.get("/api/products").get_json()}
        self.assertEqual(prods["NUT"]["material_id"], prods["ROD"]["id"])
        r = self.c.post("/api/import/products", json={"csv": "sku,name,material_sku,material_per_1000\nNUT,Nut,MISSING,5\n"})
        self.assertEqual(r.status_code, 400)


class ValueTurnoverTests(unittest.TestCase):
    def setUp(self):
        from datetime import date, timedelta
        db = stock.connect()
        for t in ("counts", "movements", "products", "locations"):
            db.execute(f"DELETE FROM {t}")
        db.execute("INSERT INTO locations(name) VALUES ('Main')")
        loc = db.execute("SELECT id FROM locations").fetchone()[0]
        pid = db.execute("INSERT INTO products(sku,name,category,unit_cost) VALUES ('P','P','Bolts',2)").lastrowid
        db.execute("INSERT INTO products(sku,name) VALUES ('NOCOST','N')")
        today = date.today()
        ins = lambda kind, q, d: db.execute(
            "INSERT INTO movements(product_id,location_id,kind,qty,created_at,operator) VALUES (?,?,?,?,?,'t')", (pid, loc, kind, q, f"{d} 10:00:00"))
        ins("IN", 300, today - timedelta(days=100))
        for i in range(30):                      # issue 10 and receive 10 each day: stock stays at 300
            d = today - timedelta(days=i)
            ins("OUT", -10, d)
            ins("IN", 10, d)
        db.commit()
        db.close()
        self.c = client_as("admin", ADMIN_PW)

    def test_turnover_numbers(self):
        d = self.c.get("/api/insights/value?window=30").get_json()
        s = d["summary"]
        self.assertEqual(s["value_now"], 600)
        self.assertEqual(s["avg_value"], 600)
        self.assertEqual(s["turnover"], 12.2)    # 600 issued / 600 average x 365/30
        self.assertEqual(s["days"], 30)
        self.assertEqual(s["missing_cost"], 1)
        self.assertEqual(d["categories"][0]["category"], "Bolts")
        self.assertEqual(d["months"][-1]["by_category"], {"Bolts": 600})
        self.assertEqual(len(d["months"]), 12)
        self.assertEqual(self.c.get("/api/export/turnover.csv").status_code, 200)


class CacheTests(unittest.TestCase):
    def test_saving_a_movement_refreshes_cached_numbers(self):
        c = client_as("admin", ADMIN_PW)
        db = stock.connect()
        for t in ("counts", "movements", "products", "locations"):
            db.execute(f"DELETE FROM {t}")
        loc = db.execute("INSERT INTO locations(name) VALUES ('Main')").lastrowid
        db.close()
        pid = c.post("/api/products", json={"sku": "C1", "name": "Cached"}).get_json()["id"]
        before = c.get("/api/dashboard").get_json()["movements_today"]
        self.assertEqual(c.get("/api/dashboard").get_json()["movements_today"], before)   # served from cache
        self.assertEqual(c.post("/api/movements", json={"kind": "IN", "product_id": pid, "location_id": loc, "qty": 5}).status_code, 201)
        self.assertEqual(c.get("/api/dashboard").get_json()["movements_today"], before + 1)

    def test_gzip(self):
        c = client_as("admin", ADMIN_PW)
        r = c.get("/", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
        import gzip as gz
        self.assertIn(b"<!DOCTYPE html>", gz.decompress(r.data))


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        from datetime import date, timedelta
        self.today = date.today()
        db = stock.connect()
        for t in ("counts", "movements", "products", "locations"):
            db.execute(f"DELETE FROM {t}")
        self.loc = db.execute("INSERT INTO locations(name) VALUES ('Main')").lastrowid
        self.loc2 = db.execute("INSERT INTO locations(name) VALUES ('Hall B')").lastrowid
        self.pid = db.execute("INSERT INTO products(sku,name,category,unit_cost) VALUES ('B1','Bolt','Bolts',0.5)").lastrowid
        self.nid = db.execute("INSERT INTO products(sku,name,category,unit_cost) VALUES ('N1','Nut','Nuts',0.1)").lastrowid
        ins = lambda pid, kind, q, d, h=10, op="t", loc=None: db.execute(
            "INSERT INTO movements(product_id,location_id,kind,qty,created_at,operator) VALUES (?,?,?,?,?,?)",
            (pid, loc or self.loc, kind, q, f"{d} {h:02d}:00:00", op))
        start = self.today - timedelta(days=179)
        ins(self.pid, "IN", 100000, start)
        ins(self.nid, "IN", 5000, start)
        for i in range(180):                         # 100 bolts a day for 180 days, at 9:00 on the last 90 days
            d = start + timedelta(days=i)
            ins(self.pid, "OUT", -100, d, h=9 if i >= 90 else 14, op="ana" if i % 2 else "ben")
        ins(self.nid, "OUT", -1000, self.today, h=8, op="ana")
        db.commit()
        db.close()
        self.c = client_as("admin", ADMIN_PW)

    def test_overview_period_comparison(self):
        d = self.c.get("/api/analytics/overview").get_json()     # last 90 days vs the 90 before
        self.assertEqual(d["kpi"]["issued_value"], 90 * 100 * 0.5 + 100)
        self.assertEqual(d["previous"]["issued_value"], 90 * 100 * 0.5)
        self.assertAlmostEqual(d["change"]["issued_value"], 2.2, places=1)
        self.assertEqual(d["top_products"][0]["sku"], "B1")
        self.assertEqual({c["category"] for c in d["categories"]}, {"Bolts", "Nuts"})
        d = self.c.get("/api/analytics/overview?category=Nuts").get_json()
        self.assertEqual(d["kpi"]["issued_value"], 100)
        self.assertEqual(d["previous"]["issued_value"], 0)
        self.assertIsNone(d["change"]["issued_value"])
        self.assertEqual(self.c.get(f"/api/analytics/overview?location_id={self.loc2}").get_json()["kpi"]["issued_value"], 0)

    def test_bad_filters(self):
        self.assertEqual(self.c.get("/api/analytics/overview?from=2026-02-30").status_code, 400)
        self.assertEqual(self.c.get("/api/analytics/overview?from=2026-05-01&to=2026-01-01").status_code, 400)
        self.assertEqual(self.c.get("/api/analytics/product/99999").status_code, 404)

    def test_product_deep_dive(self):
        d = self.c.get(f"/api/analytics/product/{self.pid}").get_json()
        self.assertEqual(d["period"]["days"], 180)                 # starts at the first movement, not a year back
        self.assertEqual(d["summary"]["issued"], 18000)
        self.assertEqual(d["summary"]["avg_per_day"], 100)
        self.assertEqual(d["daily"][-1]["stock"], 100000 - 18000)
        self.assertEqual(len(d["forecast"]), 12)
        self.assertAlmostEqual(d["forecast"][0]["mid"], 700, delta=1)   # steady 700 a week
        self.assertAlmostEqual(sum(w["share"] for w in d["weekday"]), 100, delta=0.5)   # rounded shares

    def test_holt_forecast(self):
        import analytics
        self.assertEqual(analytics.holt_forecast([5] * 6), [])
        f = analytics.holt_forecast([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        self.assertAlmostEqual(f[0]["mid"], 110, delta=0.5)
        self.assertLessEqual(f[0]["low"], f[0]["mid"])
        self.assertGreater(f[5]["high"] - f[5]["low"], f[0]["high"] - f[0]["low"] - 1e-9)

    def test_activity(self):
        d = self.c.get("/api/analytics/activity").get_json()
        self.assertEqual(d["totals"]["issued"], 91)
        self.assertEqual(d["totals"]["busiest_slot"]["hour"], 9)
        self.assertEqual(sum(map(sum, d["heatmap"])), 91)
        self.assertEqual({p["operator"] for p in d["people"]}, {"ana", "ben"})

    def test_stocktake_accuracy(self):
        post = lambda counted: self.c.post("/api/movements", json={"kind": "ADJUST", "product_id": self.nid, "location_id": self.loc, "counted": counted})
        self.assertEqual(post(4000).status_code, 200)                    # matches: recorded, no movement
        self.assertEqual(post(3990).status_code, 201)                    # 10 missing
        d = self.c.get("/api/analytics/stocktake").get_json()
        self.assertEqual(d["summary"]["counts"], 2)
        self.assertEqual(d["summary"]["accuracy"], 50)
        self.assertEqual(d["summary"]["loss"], 1.0)
        self.assertEqual(d["differences"][0]["diff"], -10)
        mid = stock.connect().execute("SELECT MAX(id) FROM movements WHERE kind='ADJUST'").fetchone()[0]
        self.assertEqual(self.c.post(f"/api/movements/{mid}/reverse", json={}).status_code, 201)
        d = self.c.get("/api/analytics/stocktake").get_json()
        self.assertEqual((d["summary"]["counts"], d["summary"]["accuracy"]), (1, 100))   # the wrong count is dropped


class UnitTests(unittest.TestCase):
    def setUp(self):
        db = stock.connect()
        for t in ("counts", "movements", "products", "locations"):
            db.execute(f"DELETE FROM {t}")
        self.loc = db.execute("INSERT INTO locations(name) VALUES ('Main')").lastrowid
        db.commit(); db.close()
        self.c = client_as("admin", ADMIN_PW)

    def test_kg_is_default_and_pieces_are_whole(self):
        kg = self.c.post("/api/products", json={"sku": "W", "name": "Wire"}).get_json()["id"]
        pcs = self.c.post("/api/products", json={"sku": "B", "name": "Bolt", "unit": "unit"}).get_json()["id"]
        units = {p["sku"]: p["unit"] for p in self.c.get("/api/products").get_json()}
        self.assertEqual(units, {"W": "kg", "B": "pcs"})
        mv = lambda pid, q: self.c.post("/api/movements", json={"kind": "IN", "product_id": pid, "location_id": self.loc, "qty": q})
        self.assertEqual(mv(kg, 12.5).status_code, 201)
        self.assertEqual(mv(pcs, 2.5).status_code, 400)
        self.assertEqual(mv(pcs, 3).status_code, 201)
        r = self.c.post("/api/movements", json={"kind": "ADJUST", "product_id": pcs, "location_id": self.loc, "counted": 1.5})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.post("/api/products", json={"sku": "X", "name": "X", "unit": "box"}).status_code, 400)


class OpeningStockTests(unittest.TestCase):
    def setUp(self):
        db = stock.connect()
        for t in ("counts", "movements", "products", "locations"):
            db.execute(f"DELETE FROM {t}")
        db.commit(); db.close()
        self.c = client_as("admin", ADMIN_PW)

    def test_dated_opening_stock_and_metres(self):
        csv = "sku,name,unit,unit_cost\nP1,Pahi elastike,M,10.45\nB1,Bulon,KG,1.4583\n"
        self.assertEqual(self.c.post("/api/import/products", json={"csv": csv}).status_code, 200)
        units = {p["sku"]: p["unit"] for p in self.c.get("/api/products").get_json()}
        self.assertEqual(units, {"P1": "m", "B1": "kg"})
        r = self.c.post("/api/import/opening-stock", json={"csv": "sku,location,qty,date\nP1,Magazina kryesore,1998.5,2025-12-31\nB1,Magazina kryesore,320,2025-12-31\n"})
        self.assertEqual(r.status_code, 200, r.get_json())
        dates = {r["created_at"] for r in self.c.get("/api/movements").get_json()}
        self.assertEqual(dates, {"2025-12-31 23:59:00"})
        # opening stock is stock that was already there: not a receipt in analytics
        o = self.c.get("/api/analytics/overview?from=2025-12-01&to=2025-12-31").get_json()
        self.assertEqual(o["kpi"]["received_value"], 0)
        self.assertAlmostEqual(o["stock"]["value"], 1998.5 * 10.45 + 320 * 1.4583, places=2)
        bad = self.c.post("/api/import/opening-stock", json={"csv": "sku,location,qty,date\nB1,Magazina kryesore,1,2099-01-01\n"})
        self.assertEqual(bad.status_code, 400)


class DemandMethodTests(unittest.TestCase):
    def test_classification_follows_adi_and_cv2(self):
        from insights import demand_pattern
        self.assertEqual(demand_pattern([5] * 12)[0], "smooth")
        self.assertEqual(demand_pattern([1, 9, 2, 12, 1, 15, 3, 10, 2, 14])[0], "erratic")
        self.assertEqual(demand_pattern([0, 0, 6, 0, 0, 0, 5, 0, 0, 0, 6, 0])[0], "intermittent")
        self.assertEqual(demand_pattern([0, 0, 1, 0, 0, 0, 20, 0, 0, 0, 2, 0])[0], "lumpy")
        self.assertIsNone(demand_pattern([5, 0, 5, 0, 5])[0])            # fewer than 8 weeks
        self.assertIsNone(demand_pattern([0] * 10 + [5, 5])[0])          # fewer than 3 weeks with demand

    def test_sba_is_close_to_the_true_rate(self):
        from insights import croston_sba
        y = ([0, 0, 4, 0] * 10)                                           # a delivery of 4 every 4th week = 1 per week
        self.assertAlmostEqual(croston_sba(y), 1.0, delta=0.15)
        self.assertEqual(croston_sba([0, 0, 0]), 0.0)

    def test_tsb_fades_when_demand_stops_but_sba_does_not(self):
        from insights import tsb, croston_sba, weekly_forecast
        y = [10, 0, 12, 0, 9, 0] + [0] * 14
        self.assertLess(tsb(y), tsb(y[:6]) * 0.5)
        self.assertEqual(weekly_forecast(y)[0], "tsb")
        self.assertGreater(croston_sba(y), tsb(y))

    def test_forecast_needs_four_weeks(self):
        from insights import weekly_forecast
        self.assertEqual(weekly_forecast([5, 5, 5])[:2], (None, None))
        self.assertEqual(weekly_forecast([5, 5, 5, 5])[0], "average")
