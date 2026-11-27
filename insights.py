"""Analytics computed from the movement ledger (standard library only).

Per product: demand per day, days of stock left, reorder point with safety stock,
suggested order quantity, ABC (value) and XYZ (variability) classes, a 30-day
forecast and trend. Also lists slow-moving stock and unusual movements.

Each product's weekly demand is classified (Syntetos-Boylan: ADI 1.32 / CV2 0.49 into smooth, erratic,
intermittent or lumpy) and forecast with the matching method: SES for smooth demand, SBA (bias-corrected
Croston) for intermittent / erratic / lumpy, TSB when demand seems to be dying out, and a plain average
while there is too little history.

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
ADI_CUTOFF = 1.32      # average weeks between demands
CV2_CUTOFF = 0.49      # squared variation of the demand sizes
MIN_WEEKS = 8          # fewer weeks, or fewer than MIN_DEMAND_WEEKS weeks with demand: not enough to classify
MIN_DEMAND_WEEKS = 3
CROSTON_ALPHA = 0.1    # smoothing of sizes and intervals
TSB_BETA = 0.05        # smoothing of the probability of demand

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


def demand_pattern(weekly):
    """Classify weekly demand. Returns (pattern, adi, cv2); pattern is None when there is too little history."""
    nz = [v for v in weekly if v > 0]
    if len(weekly) < MIN_WEEKS or len(nz) < MIN_DEMAND_WEEKS:
        return None, None, None
    adi = len(weekly) / len(nz)
    mean = statistics.mean(nz)
    cv2 = (statistics.stdev(nz) / mean) ** 2 if mean > 0 else 0.0
    regular = adi < ADI_CUTOFF
    steady = cv2 < CV2_CUTOFF
    pattern = "smooth" if regular and steady else "erratic" if regular else "intermittent" if steady else "lumpy"
    return pattern, adi, cv2


def croston_sba(weekly, alpha=CROSTON_ALPHA):
    """Syntetos-Boylan approximation: Croston's size / interval smoothing, corrected for its upward bias."""
    first = next((i for i, v in enumerate(weekly) if v > 0), None)
    if first is None:
        return 0.0
    size, interval, gap = weekly[first], first + 1, 1
    for v in weekly[first + 1:]:
        if v > 0:
            size += alpha * (v - size)
            interval += alpha * (gap - interval)
            gap = 1
        else:
            gap += 1
    return (1 - alpha / 2) * size / interval


def tsb(weekly, alpha=CROSTON_ALPHA, beta=TSB_BETA):
    """Teunter-Syntetos-Babai: updates the probability of demand every week, so the forecast fades during
    long runs without demand (products that are dying out)."""
    first = next((i for i, v in enumerate(weekly) if v > 0), None)
    if first is None:
        return 0.0
    prob = sum(1 for v in weekly if v > 0) / len(weekly)
    size = weekly[first]
    for v in weekly:
        prob += beta * ((1 if v > 0 else 0) - prob)
        if v > 0:
            size += alpha * (v - size)
    return prob * size


def weekly_forecast(weekly):
    """Pick the method that suits the demand pattern. Returns (method, per_week, pattern, adi, cv2);
    per_week is None with fewer than 4 weeks. Methods: ses, sba, tsb, average."""
    if len(weekly) < 4:
        return None, None, None, None, None
    pattern, adi, cv2 = demand_pattern(weekly)
    if pattern is None:
        return "average", statistics.mean(weekly), None, None, None
    if pattern == "smooth":
        return "ses", _ses(weekly), pattern, adi, cv2
    idle = next((k for k, v in enumerate(reversed(weekly)) if v > 0), len(weekly))  # weeks since the last demand
    if idle > 2 * adi:
        return "tsb", tsb(weekly), pattern, adi, cv2
    return "sba", croston_sba(weekly), pattern, adi, cv2


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
        method, per_week, pattern, adi, cv2 = weekly_forecast(weekly)
        forecast = round(per_week * 30 / 7) if per_week is not None else None
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
            "pattern": pattern, "forecast_method": method,
            "adi": None if adi is None else round(adi, 2), "cv2": None if cv2 is None else round(cv2, 2),
            "history_days": n, "limited": n < MIN_HISTORY_DAYS, "spark": [round(v, 2) for v in spark],
            "consumption_value": total * cost if cost else 0,
            "value_on_hand": on_hand * cost if cost and on_hand > 0 else 0,
            "last_issue": max((e["d"] for e in ev), default=None),
            "material_id": s.get("material_id"), "material_per_1000": s.get("material_per_1000"),
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
                                  "reason": f"Dalja është {e['q'] / med:.1f}x më e madhe se zakonisht ({round(med):,})"})
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
                              "reason": f"Inventarizimi hoqi {pct:.1f}% të stokut"})
    anomalies.sort(key=lambda a: (a["date"], a["id"]), reverse=True)

    materials = material_requirements(items, lead_days)

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
        "materials_short": sum(1 for m in materials if m["status"] == "order_now"),
    }
    return {"params": {"lead_days": lead_days, "window": window, "service_level": 95,
                       "order_cycle_days": ORDER_CYCLE_DAYS, "slow_days": SLOW_DAYS},
            "summary": summary, "items": items, "materials": materials, "slow": slow, "anomalies": anomalies[:15]}


def material_requirements(items, lead_days):
    """Raw material needed for the finished goods made from it.

    For each raw material: the amount needed to make what finished goods are short of right now
    (their suggested order quantity, i.e. what to produce), plus the amount the finished-goods
    forecast will use per day. Compared with material on hand and the material's supplier lead time.
    """
    by_id = {i["id"]: i for i in items}
    groups = {}
    for i in items:
        if i["material_id"] and i["material_per_1000"] and i["material_id"] in by_id:
            groups.setdefault(i["material_id"], []).append(i)
    out = []
    for mid, products in groups.items():
        m = by_id[mid]
        per_day = now = 0
        parts = []
        for p in products:
            f = p["material_per_1000"] / 1000
            daily = (p["forecast_30d"] / 30) if p["forecast_30d"] is not None else p["avg_daily"]
            per_day += daily * f
            now += p["order_qty"] * f
            parts.append({"id": p["id"], "sku": p["sku"], "name": p["name"], "unit": p["unit"],
                          "per_1000": p["material_per_1000"], "make_now": p["order_qty"],
                          "need_now": round(p["order_qty"] * f, 1), "forecast_30d": round(daily * 30),
                          "need_30d": round(daily * 30 * f, 1)})
        on_hand = max(m["on_hand"], 0)
        needed_by_arrival = now + per_day * lead_days
        if per_day == 0 and now == 0:
            status, order = "no_demand", 0
        elif on_hand < needed_by_arrival:
            status, order = "order_now", round_up(needed_by_arrival + per_day * ORDER_CYCLE_DAYS - on_hand)
        elif on_hand < needed_by_arrival + per_day * ORDER_CYCLE_DAYS:
            status, order = "watch", 0
        else:
            status, order = "ok", 0
        left_after_now = on_hand - now
        cover = None if per_day == 0 else max(left_after_now, 0) / per_day
        parts.sort(key=lambda r: -(r["need_now"] + r["need_30d"]))
        out.append({"id": mid, "sku": m["sku"], "name": m["name"], "unit": m["unit"], "on_hand": m["on_hand"],
                    "need_now": round(now, 1), "need_per_day": round(per_day, 2), "need_30d": round(per_day * 30, 1),
                    "need_lead_time": round(needed_by_arrival, 1), "cover_days": None if cover is None else round(cover),
                    "status": status, "order_qty": order, "products": parts})
    rank = {"order_now": 0, "watch": 1, "ok": 2, "no_demand": 3}
    out.sort(key=lambda r: (rank[r["status"]], r["cover_days"] if r["cover_days"] is not None else 10 ** 9, r["sku"]))
    return out


def _month_ends(today, months):
    """Last day of each of the previous `months - 1` months, then today."""
    ends = []
    first = today.replace(day=1)
    for _ in range(months - 1):
        last = first - timedelta(days=1)
        ends.append(last)
        first = last.replace(day=1)
    return list(reversed(ends)) + [today]


def value_and_turnover(db, products, window=90, months=12, today=None):
    """Stock value over time (valued at today's unit cost) and inventory turnover.

    Turnover = value issued in the window / average stock value in the window, scaled to a year.
    Days of inventory = average stock value / value issued per day.
    """
    today = today or date.today()
    start = today - timedelta(days=window - 1)
    costed = {p["id"]: p for p in products if p["unit_cost"]}
    missing_cost = sum(1 for p in products if not p["unit_cost"])
    cat = lambda p: p["category"] or "Uncategorised"

    # ---- month-end values
    ends = _month_ends(today, months)
    first_end = ends[0]
    before = {r["product_id"]: r["q"] for r in db.execute(
        "SELECT product_id, SUM(qty) AS q FROM movements WHERE date(created_at) <= ? GROUP BY product_id", (first_end.isoformat(),))}
    deltas = db.execute(
        """SELECT product_id, date(created_at) AS d, SUM(qty) AS q FROM movements
           WHERE date(created_at) > ? AND date(created_at) <= ? GROUP BY product_id, d ORDER BY d""",
        (first_end.isoformat(), today.isoformat())).fetchall()
    bal = dict(before)
    series, k = [], 0
    for i, end in enumerate(ends):
        if i:
            while k < len(deltas) and deltas[k]["d"] <= end.isoformat():
                bal[deltas[k]["product_id"]] = bal.get(deltas[k]["product_id"], 0) + deltas[k]["q"]
                k += 1
        by_cat = {}
        for pid, q in bal.items():
            if pid in costed and q > 0:
                c = cat(costed[pid])
                by_cat[c] = by_cat.get(c, 0) + q * costed[pid]["unit_cost"]
        series.append({"date": end.isoformat(), "total": round(sum(by_cat.values()), 2),
                       "by_category": {c: round(v, 2) for c, v in by_cat.items()}})

    # ---- daily balances in the window -> average value per product
    start_bal = {r["product_id"]: r["q"] for r in db.execute(
        "SELECT product_id, SUM(qty) AS q FROM movements WHERE date(created_at) < ? GROUP BY product_id", (start.isoformat(),))}
    day_moves = {}
    for r in db.execute("""SELECT product_id, date(created_at) AS d, SUM(qty) AS q FROM movements
                           WHERE date(created_at) >= ? GROUP BY product_id, d""", (start.isoformat(),)):
        day_moves.setdefault(r["product_id"], {})[r["d"]] = r["q"]
    issued = {r["product_id"]: r["q"] for r in db.execute(
        f"""SELECT m.product_id, -SUM(m.qty) AS q FROM movements m
            WHERE {_NOT_REVERSED} AND date(m.created_at) >= ? GROUP BY m.product_id""", (start.isoformat(),))}
    dates = [(start + timedelta(days=i)).isoformat() for i in range(window)]

    rows = []
    for pid, p in costed.items():
        q, total = start_bal.get(pid, 0), 0
        moves = day_moves.get(pid, {})
        for d in dates:
            q += moves.get(d, 0)
            total += max(q, 0)
        avg_qty = total / window
        avg_value = avg_qty * p["unit_cost"]
        issued_value = issued.get(pid, 0) * p["unit_cost"]
        if avg_value == 0 and issued_value == 0:
            continue
        rows.append({"id": pid, "sku": p["sku"], "name": p["name"], "category": cat(p),
                     "avg_value": avg_value, "issued_value": issued_value,
                     "value_now": max(p["total"], 0) * p["unit_cost"]})

    def ratios(avg_value, issued_value):
        turnover = issued_value / avg_value * 365 / window if avg_value > 0 else None
        dio = avg_value / (issued_value / window) if issued_value > 0 else None
        return (None if turnover is None else round(turnover, 1)), (None if dio is None else round(dio))

    for r in rows:
        r["turnover"], r["days"] = ratios(r["avg_value"], r["issued_value"])
    cats = {}
    for r in rows:
        c = cats.setdefault(r["category"], {"category": r["category"], "avg_value": 0, "issued_value": 0, "value_now": 0, "products": 0})
        for f in ("avg_value", "issued_value", "value_now"):
            c[f] += r[f]
        c["products"] += 1
    total_avg = sum(c["avg_value"] for c in cats.values())
    for c in cats.values():
        c["turnover"], c["days"] = ratios(c["avg_value"], c["issued_value"])
        c["share"] = round(c["avg_value"] / total_avg * 100, 1) if total_avg else 0
    overall_t, overall_d = ratios(total_avg, sum(c["issued_value"] for c in cats.values()))

    rnd = lambda d: {k: (round(v, 2) if isinstance(v, float) and "value" in k else v) for k, v in d.items()}
    rows.sort(key=lambda r: -r["avg_value"])
    cat_rows = sorted(cats.values(), key=lambda c: -c["avg_value"])
    order = [c["category"] for c in cat_rows]
    now_v = series[-1]["total"]
    ago = series[-4]["total"] if len(series) >= 4 else None
    return {
        "params": {"window": window, "months": months},
        "summary": {"value_now": now_v, "value_3m_ago": ago,
                    "change_pct": round((now_v - ago) / ago * 100, 1) if ago else None,
                    "avg_value": round(total_avg, 2), "turnover": overall_t, "days": overall_d,
                    "missing_cost": missing_cost},
        "months": series, "category_order": order,
        "categories": [rnd(c) for c in cat_rows], "products": [rnd(r) for r in rows],
    }
