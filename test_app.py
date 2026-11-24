"""Run with:  python -m unittest test_app -v   (uses a throwaway database)"""
import os
import tempfile
import unittest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["VINEX_STOCK_DB"] = _tmp.name

import app as stock  # noqa: E402  (must come after the env var)


class StockTests(unittest.TestCase):
    def setUp(self):
        db = stock.connect()
        for t in ("movements", "products", "locations"):
            db.execute(f"DELETE FROM {t}")
        db.execute("INSERT INTO locations(name) VALUES ('Main'),('Packing')")
        db.close()
        self.c = stock.app.test_client()
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
        self.assertEqual(self.c.post("/api/prodata/sync").status_code, 501)
        self.assertFalse(self.c.get("/api/prodata/status").get_json()["configured"])


if __name__ == "__main__":
    unittest.main()
