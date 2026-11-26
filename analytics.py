"""Analytics module: management overview, product deep-dive, warehouse activity, stocktake accuracy.

All functions take a filter dict f = {start, end (date), category (str|None), location_id (int|None)}.
Values use today's unit cost. Reversed movements and the reversal entries are left out of flows.
Standard library only.
"""
import math
import statistics
from datetime import date, timedelta

import insights

CLEAN = "m.reversal_of IS NULL AND NOT EXISTS (SELECT 1 FROM movements r WHERE r.reversal_of = m.id)"
WEEKDAYS = ["Hën", "Mar", "Mër", "Enj", "Pre", "Sht", "Die"]


def _where(f, start=None, end=None, alias="m"):
    """SQL filter on date range, category and location. Returns (sql, args)."""
    start, end = start or f["start"], end or f["end"]
    sql = [f"date({alias}.created_at) BETWEEN ? AND ?"]
    args = [start.isoformat(), end.isoformat()]
    if f.get("category"):
        sql.append("p.category = ?")
        args.append(f["category"])
    if f.get("location_id"):
        sql.append(f"{alias}.location_id = ?")
        args.append(f["location_id"])
    return " AND ".join(sql), args


def _pct(now, before):
    if not before:
        return None
    return round((now - before) / abs(before) * 100, 1)


def _flows(db, f, start, end):
    w, a = _where(f, start, end)
    rows = db.execute(
        f"""SELECT m.kind, COUNT(*) AS lines, SUM(m.qty) AS qty, SUM(m.qty * COALESCE(p.unit_cost, 0)) AS value,
                   COUNT(DISTINCT m.product_id) AS products
            FROM movements m JOIN products p ON p.id = m.product_id
            WHERE {w} AND {CLEAN} GROUP BY m.kind""", a).fetchall()
    k = {r["kind"]: r for r in rows}
    g = lambda kind, col: (k[kind][col] or 0) if kind in k else 0
    return {
        "issued_value": round(-g("OUT", "value"), 2),
        "received_value": round(g("IN", "value"), 2),
        "issue_lines": g("OUT", "lines"),
        "receipt_lines": g("IN", "lines"),
        "movements": sum(r["lines"] for r in rows if r["kind"] != "TRANSFER_IN"),
        "products_issued": g("OUT", "products"),
        "stocktake_net_value": round(g("ADJUST", "value"), 2),
    }


def _week_start(d):
    return d - timedelta(days=d.weekday())


def overview(db, f, stock):
    n = (f["end"] - f["start"]).days + 1
    prev_end = f["start"] - timedelta(days=1)
    prev_start = prev_end - timedelta(days=n - 1)
    now, before = _flows(db, f, f["start"], f["end"]), _flows(db, f, prev_start, prev_end)
    change = {k: _pct(now[k], before[k]) for k in now}

    # weekly issued / received value
    w, a = _where(f)
    weeks = {}
    for r in db.execute(
            f"""SELECT date(m.created_at) AS d, m.kind, SUM(m.qty * COALESCE(p.unit_cost, 0)) AS v
                FROM movements m JOIN products p ON p.id = m.product_id
                WHERE {w} AND {CLEAN} AND m.kind IN ('IN', 'OUT') GROUP BY d, m.kind""", a):
        ws = _week_start(date.fromisoformat(r["d"])).isoformat()
        b = weeks.setdefault(ws, {"issued": 0, "received": 0})
        b["issued" if r["kind"] == "OUT" else "received"] += abs(r["v"] or 0)
    series, ws = [], _week_start(f["start"])
    while ws <= f["end"]:
        b = weeks.get(ws.isoformat(), {"issued": 0, "received": 0})
        series.append({"week": ws.isoformat(), "issued": round(b["issued"], 2), "received": round(b["received"], 2)})
        ws += timedelta(days=7)

    top = db.execute(
        f"""SELECT p.id, p.sku, p.name, p.unit, -SUM(m.qty) AS qty, -SUM(m.qty * COALESCE(p.unit_cost, 0)) AS value
            FROM movements m JOIN products p ON p.id = m.product_id
            WHERE {w} AND {CLEAN} AND m.kind = 'OUT' GROUP BY p.id ORDER BY value DESC, qty DESC LIMIT 10""", a).fetchall()
    cats = db.execute(
        f"""SELECT COALESCE(NULLIF(p.category, ''), 'Uncategorised') AS category, -SUM(m.qty * COALESCE(p.unit_cost, 0)) AS value
            FROM movements m JOIN products p ON p.id = m.product_id
            WHERE {w} AND {CLEAN} AND m.kind = 'OUT' GROUP BY category ORDER BY value DESC""", a).fetchall()
    total_cat = sum(c["value"] or 0 for c in cats) or 0

    # stock position now, with the same category / location filter
    rows = [s for s in stock if not f.get("category") or s["category"] == f["category"]]
    if f.get("location_id"):
        on_hand = lambda s: sum(l["qty"] for l in s["locations"] if l["location_id"] == f["location_id"])
    else:
        on_hand = lambda s: s["total"]
    value_now = sum(max(on_hand(s), 0) * (s["unit_cost"] or 0) for s in rows)
    out_now = sum(1 for s in rows if s["status"] == "out")
    low_now = sum(1 for s in rows if s["status"] == "low")

    adv = insights.compute(db, rows)
    alerts = {
        "order_now": [{k: i[k] for k in ("id", "sku", "name", "unit", "on_hand", "cover_days", "order_qty")}
                      for i in adv["items"] if i["status"] == "order_now"][:6],
        "order_now_count": adv["summary"]["order_now"],
        "order_soon_count": adv["summary"]["watch"],
        "materials_short": adv["summary"]["materials_short"],
        "slow_value": adv["summary"]["slow_value"],
        "slow_count": adv["summary"]["slow_count"],
        "anomalies": [a for a in adv["anomalies"] if f["start"].isoformat() <= a["date"] <= f["end"].isoformat()][:5],
    }
    return {
        "period": {"start": f["start"].isoformat(), "end": f["end"].isoformat(), "days": n,
                   "prev_start": prev_start.isoformat(), "prev_end": prev_end.isoformat()},
        "kpi": now, "previous": before, "change": change,
        "stock": {"value": round(value_now, 2), "out": out_now, "low": low_now, "products": len(rows)},
        "weekly": series,
        "top_products": [{**dict(r), "qty": round(r["qty"], 3), "value": round(r["value"] or 0, 2)} for r in top],
        "categories": [{"category": c["category"], "value": round(c["value"] or 0, 2),
                        "share": round((c["value"] or 0) / total_cat * 100, 1) if total_cat else 0} for c in cats],
        "alerts": alerts,
    }


# ------------------------------------------------------------------ product deep-dive
def holt_forecast(values, horizon=12, alpha=0.3, beta=0.1, z=1.28):
    """Holt's linear exponential smoothing with an 80 % likely range (z = 1.28).
    Returns [{"step", "mid", "low", "high"}] or [] when there is too little history."""
    if len(values) < 8:
        return []
    level = values[0]
    trend = statistics.mean(values[i + 1] - values[i] for i in range(min(4, len(values) - 1)))
    errors = []
    for v in values[1:]:
        pred = level + trend
        errors.append(v - pred)
        new_level = alpha * v + (1 - alpha) * (level + trend)
        trend = beta * (new_level - level) + (1 - beta) * trend
        level = new_level
    sd = statistics.pstdev(errors) if len(errors) > 1 else 0
    out = []
    for h in range(1, horizon + 1):
        mid = max(level + trend * h, 0)
        spread = z * sd * math.sqrt(h)
        out.append({"step": h, "mid": round(mid, 1), "low": round(max(mid - spread, 0), 1), "high": round(mid + spread, 1)})
    return out


def product(db, pid, f, stock_row):
    loc = f.get("location_id")
    start, end = f["start"], f["end"]
    first = db.execute("SELECT MIN(date(created_at)) FROM movements WHERE product_id = ?", (pid,)).fetchone()[0]
    if first and date.fromisoformat(first) > start:
        start = min(date.fromisoformat(first), end)   # no empty months before the product's first movement
    la = [loc] if loc else []
    lsql = " AND m.location_id = ?" if loc else ""
    opening = db.execute(f"SELECT COALESCE(SUM(m.qty), 0) FROM movements m WHERE m.product_id = ? AND date(m.created_at) < ?{lsql}",
                         [pid, start.isoformat()] + la).fetchone()[0]
    per_day = {}
    for r in db.execute(
            f"""SELECT date(m.created_at) AS d, m.kind, SUM(m.qty) AS q
                FROM movements m WHERE m.product_id = ? AND date(m.created_at) BETWEEN ? AND ?{lsql} GROUP BY d, m.kind""",
            [pid, start.isoformat(), end.isoformat()] + la):
        per_day.setdefault(r["d"], {})[r["kind"]] = r["q"]
    issued_clean = {r["d"]: -r["q"] for r in db.execute(
        f"""SELECT date(m.created_at) AS d, SUM(m.qty) AS q FROM movements m
            WHERE m.product_id = ? AND m.kind = 'OUT' AND {CLEAN} AND date(m.created_at) BETWEEN ? AND ?{lsql} GROUP BY d""",
        [pid, start.isoformat(), end.isoformat()] + la)}
    received_clean = {r["d"]: r["q"] for r in db.execute(
        f"""SELECT date(m.created_at) AS d, SUM(m.qty) AS q FROM movements m
            WHERE m.product_id = ? AND m.kind = 'IN' AND {CLEAN} AND date(m.created_at) BETWEEN ? AND ?{lsql} GROUP BY d""",
        [pid, start.isoformat(), end.isoformat()] + la)}

    days, bal = [], opening
    d = start
    while d <= end:
        k = d.isoformat()
        bal += sum(per_day.get(k, {}).values())
        days.append({"date": k, "issued": round(issued_clean.get(k, 0), 3), "received": round(received_clean.get(k, 0), 3),
                     "stock": round(bal, 3)})
        d += timedelta(days=1)

    # weekly issued totals (complete weeks only) and forecast
    weekly = {}
    for x in days:
        ws = _week_start(date.fromisoformat(x["date"]))
        weekly[ws] = weekly.get(ws, 0) + x["issued"]
    this_week = _week_start(end)
    wk = [{"week": k.isoformat(), "issued": round(v, 1)} for k, v in sorted(weekly.items())
          if k >= start and k < this_week]
    fc = holt_forecast([w["issued"] for w in wk])
    for x in fc:
        x["week"] = (this_week + timedelta(weeks=x["step"] - 1)).isoformat()

    issued_total = sum(x["issued"] for x in days)
    weekday = [0] * 7
    for x in days:
        weekday[date.fromisoformat(x["date"]).weekday()] += x["issued"]
    months = db.execute(
        f"""SELECT strftime('%Y-%m', m.created_at) AS month, -SUM(m.qty) AS qty FROM movements m
            WHERE m.product_id = ? AND m.kind = 'OUT' AND {CLEAN} AND date(m.created_at) >= ?{lsql} GROUP BY month ORDER BY month""",
        [pid, (end.replace(day=1) - timedelta(days=730)).replace(day=1).isoformat()] + la).fetchall()
    refs = db.execute(
        f"""SELECT COALESCE(NULLIF(m.reference, ''), '(no reference)') AS reference, -SUM(m.qty) AS qty, COUNT(*) AS lines
            FROM movements m WHERE m.product_id = ? AND m.kind = 'OUT' AND {CLEAN} AND date(m.created_at) BETWEEN ? AND ?{lsql}
            GROUP BY reference ORDER BY qty DESC LIMIT 8""", [pid, start.isoformat(), end.isoformat()] + la).fetchall()
    people = db.execute(
        f"""SELECT m.operator, COUNT(*) AS lines, SUM(CASE WHEN m.kind = 'OUT' THEN -m.qty ELSE 0 END) AS issued
            FROM movements m WHERE m.product_id = ? AND {CLEAN} AND m.kind != 'TRANSFER_IN' AND date(m.created_at) BETWEEN ? AND ?{lsql}
            GROUP BY m.operator ORDER BY lines DESC LIMIT 8""", [pid, start.isoformat(), end.isoformat()] + la).fetchall()
    lots = db.execute(
        """SELECT l.name AS location, m.lot, ROUND(SUM(m.qty), 3) AS qty FROM movements m JOIN locations l ON l.id = m.location_id
           WHERE m.product_id = ? GROUP BY m.location_id, m.lot HAVING ABS(SUM(m.qty)) > 0.0005 ORDER BY l.name, m.lot""", (pid,)).fetchall()

    adv = insights.compute(db, [stock_row])["items"][0] if stock_row else None
    issue_days = sum(1 for x in days if x["issued"] > 0)
    return {
        "product": {k: stock_row[k] for k in ("id", "sku", "name", "category", "spec", "unit", "min_stock", "unit_cost", "total")} if stock_row else None,
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": len(days)},
        "summary": {"issued": round(issued_total, 3), "received": round(sum(x["received"] for x in days), 3),
                    "avg_per_day": round(issued_total / len(days), 2) if days else 0,
                    "max_day": max((x["issued"] for x in days), default=0), "issue_days": issue_days,
                    "issued_value": round(issued_total * (stock_row["unit_cost"] or 0), 2) if stock_row else 0},
        "advice": {k: adv[k] for k in ("status", "cover_days", "stockout_date", "reorder_point", "safety_stock", "order_qty",
                                        "abc", "xyz", "trend_pct")} if adv else None,
        "daily": days, "weekly": wk, "forecast": fc,
        "weekday": [{"day": WEEKDAYS[i], "qty": round(weekday[i], 1),
                     "share": round(weekday[i] / issued_total * 100, 1) if issued_total else 0} for i in range(7)],
        "months": [{"month": m["month"], "qty": round(m["qty"], 1)} for m in months],
        "references": [dict(r) for r in refs], "people": [dict(r) for r in people], "lots": [dict(r) for r in lots],
    }


# ------------------------------------------------------------------ warehouse activity
def activity(db, f):
    w, a = _where(f)
    base = f"FROM movements m JOIN products p ON p.id = m.product_id WHERE {w} AND m.kind != 'TRANSFER_IN'"
    heat = [[0] * 24 for _ in range(7)]
    for r in db.execute(f"SELECT strftime('%w', m.created_at) AS wd, CAST(strftime('%H', m.created_at) AS INTEGER) AS h, COUNT(*) AS c {base} GROUP BY wd, h", a):
        heat[(int(r["wd"]) + 6) % 7][r["h"]] = r["c"]
    group = "CASE m.kind WHEN 'IN' THEN 'received' WHEN 'OUT' THEN 'issued' WHEN 'ADJUST' THEN 'stocktake' ELSE 'transfer' END"
    per_day = {}
    for r in db.execute(f"SELECT date(m.created_at) AS d, {group} AS g, COUNT(*) AS c {base} GROUP BY d, g", a):
        per_day.setdefault(r["d"], {})[r["g"]] = r["c"]
    daily, d = [], f["start"]
    while d <= f["end"]:
        x = per_day.get(d.isoformat(), {})
        daily.append({"date": d.isoformat(), **{g: x.get(g, 0) for g in ("received", "issued", "transfer", "stocktake")}})
        d += timedelta(days=1)
    people = db.execute(
        f"""SELECT COALESCE(NULLIF(m.operator, ''), '(unknown)') AS operator, COUNT(*) AS lines,
                   SUM(m.kind = 'IN') AS received, SUM(m.kind = 'OUT') AS issued, SUM(m.kind = 'TRANSFER_OUT') AS transfers,
                   SUM(m.kind = 'ADJUST') AS stocktakes, SUM(m.reversal_of IS NOT NULL) AS reversals,
                   COUNT(DISTINCT m.product_id) AS products, MAX(m.created_at) AS last
            {base} GROUP BY operator ORDER BY lines DESC""", a).fetchall()
    places = db.execute(
        f"""SELECT l.name AS location, COUNT(*) AS lines,
                   SUM(CASE WHEN m.qty > 0 THEN m.qty * COALESCE(p.unit_cost, 0) ELSE 0 END) AS value_in,
                   SUM(CASE WHEN m.qty < 0 THEN -m.qty * COALESCE(p.unit_cost, 0) ELSE 0 END) AS value_out
            FROM movements m JOIN products p ON p.id = m.product_id JOIN locations l ON l.id = m.location_id
            WHERE {w} GROUP BY l.id ORDER BY lines DESC""", a).fetchall()
    totals = {g: sum(x[g] for x in daily) for g in ("received", "issued", "transfer", "stocktake")}
    busiest_slot = max(((heat[i][h], i, h) for i in range(7) for h in range(24)), default=(0, 0, 0))
    work_days = [x for x in daily if sum(x[g] for g in totals)]
    return {
        "period": {"start": f["start"].isoformat(), "end": f["end"].isoformat(), "days": len(daily)},
        "totals": {**totals, "all": sum(totals.values()), "active_days": len(work_days),
                   "per_active_day": round(sum(totals.values()) / len(work_days), 1) if work_days else 0,
                   "busiest_slot": {"day": WEEKDAYS[busiest_slot[1]], "hour": busiest_slot[2], "lines": busiest_slot[0]} if busiest_slot[0] else None},
        "heatmap": heat, "daily": daily,
        "people": [dict(r) for r in people],
        "locations": [{**dict(r), "value_in": round(r["value_in"] or 0, 2), "value_out": round(r["value_out"] or 0, 2)} for r in places],
    }


# ------------------------------------------------------------------ stocktake accuracy
def stocktake(db, f):
    w, a = _where(f, alias="c")
    rows = db.execute(
        f"""SELECT c.id, c.created_at, c.system_qty, c.counted, c.counted - c.system_qty AS diff, c.lot, c.operator,
                   p.id AS pid, p.sku, p.name, p.unit, COALESCE(p.unit_cost, 0) AS cost, l.name AS location
            FROM counts c JOIN products p ON p.id = c.product_id JOIN locations l ON l.id = c.location_id
            WHERE {w} ORDER BY c.created_at DESC, c.id DESC""", a).fetchall()
    tol = lambda r: abs(r["diff"]) < 0.0005

    def agg(key):
        out = {}
        for r in rows:
            k = r[key]
            g = out.setdefault(k, {"name": k, "counts": 0, "exact": 0, "loss": 0.0, "gain": 0.0, "abs_qty_diff": 0.0})
            g["counts"] += 1
            g["exact"] += tol(r)
            v = r["diff"] * r["cost"]
            g["loss" if v < 0 else "gain"] += abs(v)
            g["abs_qty_diff"] += abs(r["diff"])
        res = []
        for g in out.values():
            g["accuracy"] = round(g["exact"] / g["counts"] * 100, 1)
            g["net"] = round(g["gain"] - g["loss"], 2)
            g["loss"], g["gain"] = round(g["loss"], 2), round(g["gain"], 2)
            res.append(g)
        return sorted(res, key=lambda g: (g["net"], -g["counts"]))

    loss = sum(-r["diff"] * r["cost"] for r in rows if r["diff"] < 0)
    gain = sum(r["diff"] * r["cost"] for r in rows if r["diff"] > 0)
    by_product = agg("sku")
    names = {r["sku"]: (r["name"], r["unit"], r["pid"]) for r in rows}
    for g in by_product:
        g["product"], g["unit"], g["id"] = names[g["name"]]
    return {
        "period": {"start": f["start"].isoformat(), "end": f["end"].isoformat()},
        "summary": {"counts": len(rows), "exact": sum(1 for r in rows if tol(r)),
                    "accuracy": round(sum(1 for r in rows if tol(r)) / len(rows) * 100, 1) if rows else None,
                    "loss": round(loss, 2), "gain": round(gain, 2), "net": round(gain - loss, 2),
                    "products": len({r["pid"] for r in rows})},
        "by_location": agg("location"), "by_person": agg("operator"),
        "by_product": [g for g in by_product if g["abs_qty_diff"] > 0.0005][:25],
        "differences": [{"id": r["id"], "date": r["created_at"], "sku": r["sku"], "name": r["name"], "unit": r["unit"],
                         "location": r["location"], "lot": r["lot"], "system": r["system_qty"], "counted": r["counted"],
                         "diff": round(r["diff"], 3), "value": round(r["diff"] * r["cost"], 2), "operator": r["operator"]}
                        for r in rows if not tol(r)][:50],
    }
