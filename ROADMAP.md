# Roadmap: data analytics and data science

Built one at a time. Each item lands on the Insights page unless noted.

## Done
- [x] Demand per day with 4-week trend, days of stock left, expected stock-out date
- [x] Reorder point with safety stock (95% service level), suggested order quantity
- [x] 30-day forecast (exponential smoothing), ABC / XYZ classes
- [x] Slow-moving stock, unusual movements
- [x] **Stock value over time and inventory turnover**: monthly stock value by category; turnover and days of inventory per product and category
- [x] **Raw material forecast**: link finished products to their wire rod (kg per 1,000 pcs) and see the material needed for production now and per 30 days, how long it lasts, and what to order

- [x] **Analytics module** (menu group *Analytics*, shared period / category / location filters, CSV on every page):
  - Overview: issued, received and movements compared with the previous period, weekly value chart, top products and categories, alerts
  - Product analysis: stock level over time, weekly use with a 12-week forecast and likely range (Holt), weekday and monthly patterns, references, lots, people
  - Warehouse activity: weekday x hour heatmap, lines per day, by person, by location
  - Stocktake accuracy: every count recorded (also matching ones), accuracy %, value lost and found by location, person and product

- [x] **Forecast method by demand pattern**: weekly demand classified by ADI / CV2 (steady, erratic, occasional, lumpy); smooth items use exponential smoothing, the rest SBA, or TSB when demand is fading; a weekly average with under 8 weeks of history

## Next
- [ ] **Forecast log and accuracy**: store every forecast and score it against what happened (bias, RMSSE), so the method choice can be checked on Vinex's own data
- [ ] **Seasonal forecast**: add yearly seasonality (Holt-Winters) once there are two years of history
- [ ] **Weekly report for management**: Monday summary of what to order, what ran low, unusual movements and stock value
- [ ] **Heat number traceability**: from a lot, back to the wire-rod heat number and forward to every delivery from the same heat
- [ ] **Products used together**: which items are issued on the same reference (bolt + nut + washer kits)
- [ ] **Supplier lead-time learning**: record order dates and learn each supplier's real lead time and its variation

## Later (needs more data or other systems)
- [ ] Production link: book finished goods from machine data (MQTT / InfluxDB), planned vs actual output
- [ ] Demand by customer and early warning on dropping orders (Prodata sales orders)
- [ ] Price and margin analysis: steel cost per kg vs selling price per product (Prodata)
- [ ] Optimal minimum / maximum levels by simulation (holding cost vs stock-out cost)
