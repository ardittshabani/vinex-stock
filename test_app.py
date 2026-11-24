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
        for t in ("movements", "products", "locations"):
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
        self.assertIn("Not enough stock", r.get_json()["error"])
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
        self.assertIn("IN", self.c.get("/api/export/movements.csv").get_data(as_text=True))
        d = self.c.get("/api/dashboard").get_json()
        self.assertEqual((d["products"], d["low_count"], d["movements_today"]), (1, 1, 1))

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
        db.execute("DELETE FROM movements"); db.execute("DELETE FROM products"); db.execute("DELETE FROM locations")
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


if __name__ == "__main__":
    unittest.main()
