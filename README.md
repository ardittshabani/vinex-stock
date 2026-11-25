# Vinex Fasteners – Stock Management

Web app for tracking stock of fasteners and raw material. Use it on a PC, or host it (Railway) and open it from a phone.

## Run it on a Windows PC

1. Install Python 3.10+ from python.org (tick "Add Python to PATH").
2. Double-click `run.bat`. It installs Flask, opens http://localhost:5000 and keeps a console window open.
3. **First sign-in:** on the very first start the console prints a box with the admin username (`admin`) and a random password. Sign in with it, then use **Password** (top right) to set your own.
   To choose the password yourself, set `VINEX_ADMIN_PASSWORD` before the first start (`set VINEX_ADMIN_PASSWORD=your-password` in the console, then `python app.py`).
4. Optional: `python seed_demo.py` loads sample products and movements. Delete `stock.db` to start clean.

Other devices on the same Wi-Fi/LAN: start with `set VINEX_HOST=0.0.0.0` and open `http://<this-PC-address>:5000`. Everyone needs a login, but the connection is plain HTTP – fine inside the factory network, not over the internet.

## Run it on Railway

1. Connect the GitHub repo to a Railway service. The `Procfile` starts it with gunicorn.
2. **Add a Volume** to the service and mount it at `/data`. Without a volume the database is wiped on every redeploy.
3. Set these **Variables**:

   | Variable | Value |
   |---|---|
   | `VINEX_STOCK_DB` | `/data/stock.db` |
   | `VINEX_ADMIN_PASSWORD` | a strong password for the first admin (used only when there are no users yet) |
   | `VINEX_ADMIN_USER` | optional, default `admin` |

   If you don't set `VINEX_ADMIN_PASSWORD`, a random one is printed once in the deploy log.
4. Keep **one instance** (SQLite is a single file).
5. Optional: set the health-check path to `/healthz`.

Locked out? Set `VINEX_ADMIN_PASSWORD` to a new password and `VINEX_ADMIN_RESET=1`, redeploy, sign in, then **remove `VINEX_ADMIN_RESET`**.

## Users and roles

Admins manage users in the **Users** tab.

| Role | Can do |
|---|---|
| `admin` | everything: products, locations, CSV imports, users |
| `staff` | view everything, book movements (receive / issue / transfer / stocktake) |
| `viewer` | read-only |

Every movement records the signed-in user. Passwords are hashed; 5 wrong attempts lock that user from that address for 5 minutes; changing or resetting a password signs out that user's other sessions; sessions last 7 days.

## What it does

- **Products** – SKU, name, category, spec, unit, minimum stock, unit cost, optional barcode / QR value, optional Prodata code.
- **Locations** – warehouse zones, raw-material store, packing area, racks/bins.
- **Movements** – Receive, Issue, Transfer, Stocktake (enter the counted quantity; the app books the difference), with lot / heat number and reference.
- **Safety checks** – cannot issue more than is on hand (per location and per lot); transfers are all-or-nothing; two people saving at once can't both take the last box.
- **Barcode / QR scanning** – scan with the phone camera (needs an https address) to fill a product in a movement, or to find it on the Stock page. Matches barcode first, then SKU, then Prodata code.
- **Reverse a movement** (admin) – books an opposite entry instead of deleting; both legs of a transfer are reversed, and it is refused if it would make stock negative.
- **Insights** (data science, from the movement history) – demand per day with a 4-week trend, days of stock left and expected stock-out date, reorder point with safety stock (95% service level, adjustable supplier lead time), suggested order quantity, 30-day forecast (exponential smoothing), ABC (usage value) and XYZ (demand variability) classes, slow-moving stock, unusual movements (outlier issues, large stocktake losses), and raw material needed: link a finished product to its wire rod with the kg per 1,000 pcs (Products → Edit → Made from, or the `material_sku` / `material_per_1000` CSV columns) and Insights shows how much material production needs now and per 30 days, how long it lasts, and what to order. A second view, *Value and turnover*, shows stock value by month and category, inventory turnover and days of inventory per category and product (CSV download). The **Analytics** section adds an overview with period comparison, a product deep-dive with forecast, warehouse activity and stocktake accuracy (`analytics.py`, endpoints under `/api/analytics/`). See `ROADMAP.md` for what comes next. Downloadable as CSV. Logic in `insights.py`, standard library only.
- **Dashboard** – low / out of stock, stock value, 14-day activity chart, recent movements, reorder list (CSV download). **Stock** view with sortable columns, per-location and per-lot detail, per-product history. Tables become cards on phones.
- **CSV** – import products (columns include `barcode`), import opening stock (go-live), export stock, movements and the reorder list.

Quantities are never edited directly: each is the sum of an append-only movement ledger, so every number traces back to who did what and when.

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask server, API, SQLite database |
| `auth.py` | Login, roles, user management |
| `static/index.html` | The whole UI (no build step), works on phone screens |
| `insights.py` | Analytics behind the Insights page |
| `prodata.py` | Placeholder for the Prodata integration |
| `seed_demo.py` | Demo data |
| `test_app.py` | `python -m unittest test_app -v` |
| `Procfile` | Start command for Railway |
| `static/vendor/` | html5-qrcode (Apache-2.0) for scanning, loaded only when you tap Scan; IBM Plex Sans (OFL) |

## Install as an app

Open the site in Chrome or Edge and click the install icon in the address bar (or menu → *Install Vinex Stock*). It then runs in its own window with its own icon, and has shortcuts to Stock and Insights. On iPhone: Share → *Add to Home Screen*; on Android: menu → *Install app*. Stock data is never cached on the device; it always comes live from the server.

## Prodata

Not connected yet; the app works fully without it. Movements carry a `prodata_synced` flag and products a `prodata_code`, so the sync can be added in `prodata.py` once the Prodata API documentation is available. Note: a cloud-hosted app can only reach Prodata if Prodata's API is reachable from the internet.

## Good to know

- **Back up `stock.db`** (on Railway: the volume's file; also export CSVs regularly).
- Prices are shown in €.
- Cookies are marked secure automatically on Railway; for other HTTPS hosts set `VINEX_COOKIE_SECURE=1` and `VINEX_BEHIND_PROXY=1`.
