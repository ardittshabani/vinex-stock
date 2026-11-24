"""Load sample fastener data so you can try the app. Safe to skip; delete stock.db to start clean."""
import sys

import app as stock

PRODUCTS = [
    # sku, name, category, spec, unit, min_stock, unit_cost
    ("HB-M8x40-88-ZN", "Hex bolt M8x40", "Bolts", "DIN 933, class 8.8, zinc", "pcs", 5000, 0.045),
    ("HB-M10x60-88-ZN", "Hex bolt M10x60", "Bolts", "DIN 933, class 8.8, zinc", "pcs", 3000, 0.09),
    ("HB-M12x80-109-BL", "Hex bolt M12x80", "Bolts", "DIN 931, class 10.9, black", "pcs", 1500, 0.21),
    ("HN-M8-8-ZN", "Hex nut M8", "Nuts", "DIN 934, class 8, zinc", "pcs", 8000, 0.015),
    ("HN-M10-8-ZN", "Hex nut M10", "Nuts", "DIN 934, class 8, zinc", "pcs", 6000, 0.024),
    ("WS-M8-ZN", "Flat washer M8", "Washers", "DIN 125, zinc", "pcs", 10000, 0.008),
    ("SC-4.8x38-ZN", "Self-tapping screw 4.8x38", "Screws", "DIN 7981, zinc", "pcs", 20000, 0.012),
    ("RM-10B21-8", "Wire rod 10B21, 8 mm", "Raw material", "Cold-heading steel coil", "kg", 2000, 0.78),
    ("PK-BOX-25", "Carton box 25 kg", "Packaging", "Printed Vinex carton", "pcs", 300, 0.55),
]

# sku, location, qty, lot
OPENING = [
    ("HB-M8x40-88-ZN", "Main warehouse", 18000, "L2611-A"),
    ("HB-M8x40-88-ZN", "Packing area", 2500, "L2611-A"),
    ("HB-M10x60-88-ZN", "Main warehouse", 2400, "L2610-C"),   # below minimum -> low-stock alert
    ("HB-M12x80-109-BL", "Main warehouse", 4200, "L2609-B"),
    ("HN-M8-8-ZN", "Main warehouse", 30000, "L2611-N"),
    ("HN-M10-8-ZN", "Main warehouse", 14000, "L2610-N"),
    ("WS-M8-ZN", "Main warehouse", 52000, ""),
    ("SC-4.8x38-ZN", "Main warehouse", 9000, "L2611-S"),      # below minimum
    ("RM-10B21-8", "Raw material store", 11500, "HEAT-88231"),
    ("PK-BOX-25", "Packing area", 640, ""),
]


def main():
    db = stock.connect()
    if db.execute("SELECT COUNT(*) FROM products").fetchone()[0]:
        print("Database already has products - not seeding. Delete stock.db first for a clean demo.")
        return 1
    loc_ids = {}
    for name in ("Main warehouse", "Raw material store", "Packing area"):
        row = db.execute("SELECT id FROM locations WHERE name=?", (name,)).fetchone()
        loc_ids[name] = row["id"] if row else db.execute("INSERT INTO locations(name) VALUES (?)", (name,)).lastrowid
    pid = {}
    for sku, name, cat, spec, unit, min_stock, cost in PRODUCTS:
        pid[sku] = db.execute(
            "INSERT INTO products(sku,name,category,spec,unit,min_stock,unit_cost) VALUES (?,?,?,?,?,?,?)",
            (sku, name, cat, spec, unit, min_stock, cost),
        ).lastrowid
    for sku, loc, qty, lot in OPENING:
        stock.insert_movement(db, pid[sku], loc_ids[loc], "IN", qty, lot, "DEMO", "Demo opening stock", "demo")
    stock.insert_movement(db, pid["HB-M8x40-88-ZN"], loc_ids["Main warehouse"], "OUT", -4000, "L2611-A", "ORDER-1042", "Shipped to customer", "demo")
    stock.insert_movement(db, pid["RM-10B21-8"], loc_ids["Raw material store"], "OUT", -1500, "HEAT-88231", "WO-318", "Issued to cold heading", "demo")
    print(f"Seeded {len(PRODUCTS)} products and {len(OPENING) + 2} movements.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
