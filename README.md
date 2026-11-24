# Vinex Fasteners – Stock Management (prototype)

Desktop web app for tracking stock of fasteners and raw material. Runs on one Windows PC; open it in any browser.

## Run it

1. Install Python 3.10+ (python.org) – tick "Add Python to PATH".
2. Double-click `run.bat` (first run installs Flask, then opens http://localhost:5000).
3. Optional: `python seed_demo.py` loads sample products and movements. Delete `stock.db` to start clean.

## What it does

- **Products** – SKU, name, category, spec (standard/class/finish), unit, minimum stock, unit cost, optional Prodata code.
- **Locations** – warehouse zones, raw-material store, packing area, racks/bins.
- **Movements** – Receive, Issue, Transfer between locations, Stocktake (enter the counted quantity; the app books the difference). Lot / heat number and reference (order, work order, delivery note) on every movement.
- **Safety checks** – cannot issue more than is on hand (per location, and per lot if a lot is given); transfers are all-or-nothing; two people saving at once can't both take the last box.
- **Dashboard** – low / out of stock, stock value, recent movements. **Stock** view with per-location and per-lot breakdown.
- **CSV** – import products, import opening stock (for go-live), export stock and movements (opens in Excel).

Stock is never edited directly: every quantity is the sum of an append-only movement ledger, so each number can be traced to who did what and when.

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask server, API, SQLite database (`stock.db`, created on first run) |
| `static/index.html` | The whole UI (no build step) |
| `prodata.py` | Placeholder for the Prodata integration |
| `seed_demo.py` | Demo data |
| `test_app.py` | `python -m unittest test_app -v` |

## Prodata

Not connected yet – the app works fully without it. Movements already carry a `prodata_synced` flag and products a `prodata_code`, so the sync can be added in `prodata.py` once the Prodata API documentation is available (base URL, authentication, item and stock-document endpoints).

## Before real use

- **Back up `stock.db`** regularly (copy the file while the app is stopped, or use SQLite's backup).
- **No user accounts.** By default the app listens on this PC only. Setting `VINEX_HOST=0.0.0.0` lets other PCs open it, but anyone on the network could change stock – add login first.
- The "Operator" name in the header is a typed label, not authentication.
- Prices are shown in €.
- It runs on Flask's built-in server, fine for a single desk; for several users move to a production server and a proper database (the schema is plain SQL and ports to MSSQL).
