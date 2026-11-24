# Roadmap: data analytics and data science

Built one at a time. Each item lands on the Insights page unless noted.

## Done
- [x] Demand per day with 4-week trend, days of stock left, expected stock-out date
- [x] Reorder point with safety stock (95% service level), suggested order quantity
- [x] 30-day forecast (exponential smoothing), ABC / XYZ classes
- [x] Slow-moving stock, unusual movements
- [x] **Stock value over time and inventory turnover**: monthly stock value by category; turnover and days of inventory per product and category
- [x] **Raw material forecast**: link finished products to their wire rod (kg per 1,000 pcs) and see the material needed for production now and per 30 days, how long it lasts, and what to order

## Next
- [ ] **Weekly report for management**: Monday summary of what to order, what ran low, unusual movements and stock value
- [ ] **Seasonal forecast with a likely range**: Holt-Winters on weekly demand, with a prediction interval
- [ ] **Stocktake accuracy**: count differences per product, location and person
- [ ] **Heat number traceability**: from a lot, back to the wire-rod heat number and forward to every delivery from the same heat
- [ ] **Products used together**: which items are issued on the same reference (bolt + nut + washer kits)
- [ ] **Supplier lead-time learning**: record order dates and learn each supplier's real lead time and its variation

## Later (needs more data or other systems)
- [ ] Production link: book finished goods from machine data (MQTT / InfluxDB), planned vs actual output
- [ ] Demand by customer and early warning on dropping orders (Prodata sales orders)
- [ ] Price and margin analysis: steel cost per kg vs selling price per product (Prodata)
- [ ] Optimal minimum / maximum levels by simulation (holding cost vs stock-out cost)
