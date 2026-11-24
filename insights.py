"""Analytics computed from the movement ledger (standard library only).

Per product: demand per day, days of stock left, reorder point with safety stock,
suggested order quantity, ABC (value) and XYZ (variability) classes, a 30-day
forecast and trend. Also lists slow-moving stock and unusual movements.

Demand = issues (kind OUT). Reversed issues and reversal entries are ignored, and
transfers / stocktake adjustments are not demand.
"""
import math
import statistics
from datetime import date, timedelta

Z_95 = 1.645          # service level 95 %
ORDER_CYCLE_DAYS = 30  # how many days of demand one order should cover on top of the reorder point
SLOW_DAYS = 60
MIN_HISTORY_DAYS = 28  # below this the numbers are flagged "limited history"

_NOT_REVERSED = """kind='OUT' AND reversal_of IS NULL
    AND NOT EXISTS (SELECT 1 FROM movements r WHERE r.reversal_of = m.id)"""


def round_up(q):
    """Round an order quantity up to a sensible figure."""
    if q <= 0:
        return 0
    step = 1000 if q >= 10000 else 100 if q >= 1000 else 10 if q >= 100 else 1
    return int(math.ceil(q / step) * step)


def _weekly(daily):
    """Complete 7-day buckets counted back from today, oldest first."""
    weeks = len(daily) // 7
    return [sum(daily[len(daily) - 7 * (j + 1): len(daily) - 7 * j]) for j in reversed(range(weeks))]


def _ses(values, alpha=0.3):
    level = values[0]
    for v in values[1:]:
        level = alpha * v + (1 - alpha) * level
    return level


def _classify_abc(items):
    ranked = sorted((i for i in items if i["consumption_value"] > 0), key=lambda i: -i["consumption_value"])
    total = sum(i["consumption_value"] for i in ranked)
    cum = 0
    for i in ranked:
        before = cum / total
        cum += i["consumption_value"]
        i["abc"] = "A" if before < 0.8 else "B" if before < 0.95 else "C"


def compute(db, stock, lead_days=14, window=90, today=None):
    today = today or date.today()
    start = today - timedelta(days=window - 1)
    s_iso = start.isoformat()

    first_seen = {r["product_id"]: r["d"] for r in db.execute(
        "SELECT product_id, MIN(date(created_at)) AS d FROM movements GROUP BY product_id")}
    events = db.execute(
        f"""SELECT m.id, m.product_id, date(m.created_at) AS d, -m.qty AS q, m.operator FROM movements m
            WHERE {_NOT_REVERSED} AND date(m.created_at) >= ? ORDER BY m.id""", (s_iso,)).fetchall()
    by_product = {}
    for e in events:
        by_product.setdefault(e["product_id"], []).append(e)

    items = []
    for s in stock:
        ev = by_product.get(s["id"], [])
        seen = date.fromisoformat(first_seen[s["id"]]) if s["id"] in first_seen else today
        begin = max(start, seen)
        n = (today - begin).days + 1
        day_qty = {}
        for e in ev:
            day_qty[e["d"]] = day_qty.get(e["d"], 0) + e["q"]
        daily = [day_qty.get((begin + timedelta(days=i)).isoformat(), 0) for i in range(n)]
        total = sum(daily)
        mean = total / n if n else 0
        sd = statistics.pstdev(daily) if n > 1 else 0
        on_hand = s["total"]
        cost = s["unit_cost"]

        safety = Z_95 * sd * math.sqrt(lead_days)
        rop = mean * lead_days + safety
        cover = max(on_hand, 0) / mean if mean > 0 else None
        order_up_to = rop + mean * ORDER_CYCLE_DAYS
        order_qty = round_up(order_up_to - on_hand) if mean > 0 and on_hand <= rop else 0

        if mean == 0:
            status = "no_demand"
        elif on_hand <= rop:
            status = "order_now"
        elif on_hand <= rop * 1.5:
            status = "watch"
        else:
            status = "ok"

        weekly = _weekly(daily)
        xyz = None
        if len(weekly) >= 4 and statistics.mean(weekly) > 0:
            cv = statistics.pstdev(weekly) / statistics.mean(weekly)
            xyz = "X" if cv < 0.5 else "Y" if cv < 1.0 else "Z"
        forecast = round(_ses(weekly) * 30 / 7) if len(weekly) >= 4 else None
        trend = None
        if n >= 56:
            last, prev = sum(daily[-28:]), sum(daily[-56:-28])
            if prev > 0:
                trend = round((last - prev) / prev * 100)

        spark = ([0] * max(0, 30 - n) + daily)[-30:]
        items.append({
            "id": s["id"], "sku": s["sku"], "name": s["name"], "category": s["category"], "unit": s["unit"],
            "on_hand": on_hand, "min_stock": s["min_stock"], "unit_cost": cost,
            "avg_daily": round(mean, 2), "cover_days": None if cover is None else round(cover),
            "stockout_date": None if cover is None or cover > 730 else (today + timedelta(days=int(cover))).isoformat(),
            "safety_stock": round(safety), "reorder_point": round(rop), "order_qty": order_qty,
            "status": status, "abc": None, "xyz": xyz, "trend_pct": trend, "forecast_30d": forecast,
            "history_days": n, "limited": n < MIN_HISTORY_DAYS, "spark": [round(v, 2) for v in spark],
            "consumption_value": total * cost if cost else 0,
            "value_on_hand": on_hand * cost if cost and on_hand > 0 else 0,
            "last_issue": max((e["d"] for e in ev), default=None),
        })
    _classify_abc(items)

    # ---- slow / dead stock: stock on hand, no issues for SLOW_DAYS, and long enough history to say so
    cutoff = (today - timedelta(days=SLOW_DAYS)).isoformat()
    slow = []
    for i in items:
        old_enough = first_seen.get(i["id"]) and first_seen[i["id"]] <= cutoff
        if i["on_hand"] > 0 and old_enough and (i["last_issue"] is None or i["last_issue"] < cutoff):
            slow.append({k: i[k] for k in ("id", "sku", "name", "unit", "on_hand", "last_issue", "value_on_hand")})
    slow.sort(key=lambda r: -r["value_on_hand"])

    # ---- unusual movements
    anomalies = []
    sku = {s["id"]: s for s in stock}
    for pid, ev in by_product.items():
        qs = [e["q"] for e in ev]
        if len(qs) < 8 or pid not in sku:
            continue
        med = statistics.median(qs)
        mad = statistics.median(abs(q - med) for q in qs) or med * 0.1 or 1
        for e in ev:
            z = 0.6745 * (e["q"] - med) / mad
            if z > 3.5 and e["q"] > 2 * med:
                p = sku[pid]
                anomalies.append({"id": e["id"], "date": e["d"], "sku": p["sku"], "name": p["name"], "unit": p["unit"],
                                  "kind": "OUT", "qty": e["q"], "operator": e["operator"],
                                  "reason": f"Issue is {e['q'] / med:.1f}x the typical {round(med):,}"})
    adj = db.execute(
        """SELECT m.id, date(m.created_at) AS d, m.qty, m.operator, p.id AS pid, p.sku, p.name, p.unit
           FROM movements m JOIN products p ON p.id=m.product_id
           WHERE m.kind='ADJUST' AND m.qty < 0 AND m.reversal_of IS NULL AND date(m.created_at) >= ?
             AND NOT EXISTS (SELECT 1 FROM movements r WHERE r.reversal_of = m.id)""", (s_iso,)).fetchall()
    for a in adj:
        on_hand = sku[a["pid"]]["total"] if a["pid"] in sku else 0
        base = on_hand + -a["qty"]
        pct = -a["qty"] / base * 100 if base > 0 else 100
        if pct >= 2:
            anomalies.append({"id": a["id"], "date": a["d"], "sku": a["sku"], "name": a["name"], "unit": a["unit"],
                              "kind": "ADJUST", "qty": a["qty"], "operator": a["operator"],
                              "reason": f"Stocktake removed {pct:.1f}% of the stock"})
    anomalies.sort(key=lambda a: (a["date"], a["id"]), reverse=True)

    rank = {"order_now": 0, "watch": 1, "ok": 2, "no_demand": 3}
    items.sort(key=lambda i: (rank[i["status"]], i["cover_days"] if i["cover_days"] is not None else 10 ** 9, i["sku"]))
    covers = [i["cover_days"] for i in items if i["cover_days"] is not None]
    summary = {
        "order_now": sum(1 for i in items if i["status"] == "order_now"),
        "watch": sum(1 for i in items if i["status"] == "watch"),
        "median_cover_days": round(statistics.median(covers)) if covers else None,
        "slow_value": round(sum(r["value_on_hand"] for r in slow), 2),
        "slow_count": len(slow),
        "anomalies": len(anomalies),
        "limited_history": sum(1 for i in items if i["limited"]),
        "products": len(items),
    }
    return {"params": {"lead_days": lead_days, "window": window, "service_level": 95,
                       "order_cycle_days": ORDER_CYCLE_DAYS, "slow_days": SLOW_DAYS},
            "summary": summary, "items": items, "slow": slow, "anomalies": anomalies[:15]}
