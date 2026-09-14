-- Queries an analyst would run against bigquery-public-data.thelook_ecommerce.
-- `make example-thelook-seed` runs each one so it lands in the project's
-- query history, which is what `contextlift log` reads and `contextlift mine` ranks.
-- The definitions below are the ones a bare model gets wrong: "revenue"
-- excludes cancelled and returned items, "completed" is the exact status
-- value, "delivery time" is shipped_at to delivered_at, "margin" is sale
-- price minus product cost. A few shapes repeat with a different literal, so
-- mining sees them more than once.

-- revenue by month, 2023 (revenue = sale price of items neither cancelled nor returned)
SELECT FORMAT_TIMESTAMP('%Y-%m', oi.created_at) AS month, ROUND(SUM(oi.sale_price), 2) AS revenue
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
WHERE oi.status NOT IN ('Cancelled', 'Returned') AND EXTRACT(YEAR FROM oi.created_at) = 2023
GROUP BY month ORDER BY month;

-- same shape, 2024
SELECT FORMAT_TIMESTAMP('%Y-%m', oi.created_at) AS month, ROUND(SUM(oi.sale_price), 2) AS revenue
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
WHERE oi.status NOT IN ('Cancelled', 'Returned') AND EXTRACT(YEAR FROM oi.created_at) = 2024
GROUP BY month ORDER BY month;

-- top 10 product categories by revenue, all time
SELECT p.category, ROUND(SUM(oi.sale_price), 2) AS revenue
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id
WHERE oi.status NOT IN ('Cancelled', 'Returned')
GROUP BY p.category ORDER BY revenue DESC LIMIT 10;

-- top 5 brands by revenue in the Jeans category
SELECT p.brand, ROUND(SUM(oi.sale_price), 2) AS revenue
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id
WHERE oi.status NOT IN ('Cancelled', 'Returned') AND p.category = 'Jeans'
GROUP BY p.brand ORDER BY revenue DESC LIMIT 5;

-- same shape, another category
SELECT p.brand, ROUND(SUM(oi.sale_price), 2) AS revenue
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id
WHERE oi.status NOT IN ('Cancelled', 'Returned') AND p.category = 'Outerwear & Coats'
GROUP BY p.brand ORDER BY revenue DESC LIMIT 5;

-- gross margin by department (margin = sale price minus product cost)
SELECT p.department, ROUND(SUM(oi.sale_price - p.cost), 2) AS gross_margin
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id
WHERE oi.status NOT IN ('Cancelled', 'Returned')
GROUP BY p.department ORDER BY gross_margin DESC;

-- completed orders per month in 2023 (completed = status 'Complete')
SELECT FORMAT_TIMESTAMP('%Y-%m', created_at) AS month, COUNT(*) AS completed_orders
FROM `bigquery-public-data.thelook_ecommerce.orders`
WHERE status = 'Complete' AND EXTRACT(YEAR FROM created_at) = 2023
GROUP BY month ORDER BY month;

-- share of orders by status
SELECT status, COUNT(*) AS orders, ROUND(COUNT(*) / SUM(COUNT(*)) OVER (), 4) AS share
FROM `bigquery-public-data.thelook_ecommerce.orders`
GROUP BY status ORDER BY orders DESC;

-- average delivery time in hours by distribution center (shipped_at to delivered_at, delivered items only)
SELECT dc.name AS distribution_center,
       ROUND(AVG(TIMESTAMP_DIFF(oi.delivered_at, oi.shipped_at, HOUR)), 1) AS avg_delivery_hours
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id
JOIN `bigquery-public-data.thelook_ecommerce.distribution_centers` dc ON dc.id = p.distribution_center_id
WHERE oi.delivered_at IS NOT NULL AND oi.shipped_at IS NOT NULL
GROUP BY dc.name ORDER BY avg_delivery_hours;

-- the 10 categories with the highest return rate, counting only categories with at least 500 non-cancelled items (returned items over items that were not cancelled)
SELECT p.category,
       ROUND(COUNTIF(oi.status = 'Returned') / COUNTIF(oi.status <> 'Cancelled'), 4) AS return_rate
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id
GROUP BY p.category HAVING COUNTIF(oi.status <> 'Cancelled') >= 500
ORDER BY return_rate DESC LIMIT 10;

-- top 10 states by number of customers
SELECT state, COUNT(*) AS customers
FROM `bigquery-public-data.thelook_ecommerce.users`
GROUP BY state ORDER BY customers DESC LIMIT 10;

-- new customers per month in 2024
SELECT FORMAT_TIMESTAMP('%Y-%m', created_at) AS month, COUNT(*) AS new_customers
FROM `bigquery-public-data.thelook_ecommerce.users`
WHERE EXTRACT(YEAR FROM created_at) = 2024
GROUP BY month ORDER BY month;

-- revenue by customer traffic source, 2023
SELECT u.traffic_source, ROUND(SUM(oi.sale_price), 2) AS revenue
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
JOIN `bigquery-public-data.thelook_ecommerce.users` u ON u.id = oi.user_id
WHERE oi.status NOT IN ('Cancelled', 'Returned') AND EXTRACT(YEAR FROM oi.created_at) = 2023
GROUP BY u.traffic_source ORDER BY revenue DESC;

-- average order value by customer age band (value = revenue per completed order)
SELECT CASE WHEN u.age < 25 THEN 'under 25' WHEN u.age < 40 THEN '25-39' WHEN u.age < 60 THEN '40-59' ELSE '60+' END AS age_band,
       ROUND(SUM(oi.sale_price) / COUNT(DISTINCT oi.order_id), 2) AS avg_order_value
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
JOIN `bigquery-public-data.thelook_ecommerce.users` u ON u.id = oi.user_id
WHERE oi.status = 'Complete'
GROUP BY age_band ORDER BY age_band;

-- top 10 customers by lifetime revenue
SELECT u.id AS user_id, u.email, ROUND(SUM(oi.sale_price), 2) AS lifetime_revenue
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
JOIN `bigquery-public-data.thelook_ecommerce.users` u ON u.id = oi.user_id
WHERE oi.status NOT IN ('Cancelled', 'Returned')
GROUP BY u.id, u.email ORDER BY lifetime_revenue DESC LIMIT 10;

-- repeat customers: customers with more than one completed order, by country
SELECT u.country, COUNT(*) AS repeat_customers
FROM (
  SELECT user_id, COUNT(*) AS completed
  FROM `bigquery-public-data.thelook_ecommerce.orders`
  WHERE status = 'Complete' GROUP BY user_id HAVING completed > 1
) r
JOIN `bigquery-public-data.thelook_ecommerce.users` u ON u.id = r.user_id
GROUP BY u.country ORDER BY repeat_customers DESC LIMIT 10;

-- products never sold (no order item at all)
SELECT p.category, COUNT(*) AS products_never_sold
FROM `bigquery-public-data.thelook_ecommerce.products` p
LEFT JOIN `bigquery-public-data.thelook_ecommerce.order_items` oi ON oi.product_id = p.id
WHERE oi.id IS NULL
GROUP BY p.category ORDER BY products_never_sold DESC LIMIT 10;

-- inventory on hand by distribution center (on hand = inventory items not yet sold)
SELECT dc.name AS distribution_center, COUNT(*) AS items_on_hand
FROM `bigquery-public-data.thelook_ecommerce.inventory_items` ii
JOIN `bigquery-public-data.thelook_ecommerce.distribution_centers` dc ON dc.id = ii.product_distribution_center_id
WHERE ii.sold_at IS NULL
GROUP BY dc.name ORDER BY items_on_hand DESC;

-- purchase conversion by traffic source (sessions with a purchase event over all sessions)
SELECT traffic_source,
       ROUND(COUNT(DISTINCT IF(event_type = 'purchase', session_id, NULL)) / COUNT(DISTINCT session_id), 4) AS conversion_rate
FROM `bigquery-public-data.thelook_ecommerce.events`
GROUP BY traffic_source ORDER BY conversion_rate DESC;

-- sessions by browser in 2024
SELECT browser, COUNT(DISTINCT session_id) AS sessions
FROM `bigquery-public-data.thelook_ecommerce.events`
WHERE EXTRACT(YEAR FROM created_at) = 2024
GROUP BY browser ORDER BY sessions DESC;

-- cart abandonment: sessions with a cart event and no purchase, by month in 2024
SELECT FORMAT_TIMESTAMP('%Y-%m', created_at) AS month, COUNTIF(has_cart AND NOT has_purchase) AS abandoned_sessions
FROM (
  SELECT session_id, MIN(created_at) AS created_at,
         LOGICAL_OR(event_type = 'cart') AS has_cart,
         LOGICAL_OR(event_type = 'purchase') AS has_purchase
  FROM `bigquery-public-data.thelook_ecommerce.events`
  WHERE EXTRACT(YEAR FROM created_at) = 2024
  GROUP BY session_id
)
GROUP BY month ORDER BY month;

-- monthly revenue with month-over-month change, 2023
WITH monthly AS (
  SELECT FORMAT_TIMESTAMP('%Y-%m', created_at) AS month, SUM(sale_price) AS revenue
  FROM `bigquery-public-data.thelook_ecommerce.order_items`
  WHERE status NOT IN ('Cancelled', 'Returned') AND EXTRACT(YEAR FROM created_at) = 2023
  GROUP BY month
)
SELECT month, ROUND(revenue, 2) AS revenue,
       ROUND(revenue - LAG(revenue) OVER (ORDER BY month), 2) AS change_from_previous_month
FROM monthly ORDER BY month;

-- best-selling product per category in 2024 by units
SELECT category, name AS product, units
FROM (
  SELECT p.category, p.name, COUNT(*) AS units,
         ROW_NUMBER() OVER (PARTITION BY p.category ORDER BY COUNT(*) DESC, p.name) AS rn
  FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
  JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id
  WHERE oi.status NOT IN ('Cancelled', 'Returned') AND EXTRACT(YEAR FROM oi.created_at) = 2024
  GROUP BY p.category, p.name
)
WHERE rn = 1 ORDER BY units DESC LIMIT 10;

-- orders with more than 3 items, by month in 2024
SELECT FORMAT_TIMESTAMP('%Y-%m', created_at) AS month, COUNT(*) AS large_orders
FROM `bigquery-public-data.thelook_ecommerce.orders`
WHERE num_of_item > 3 AND EXTRACT(YEAR FROM created_at) = 2024
GROUP BY month ORDER BY month;

-- average days from order to delivery by country, delivered orders only
SELECT u.country, ROUND(AVG(TIMESTAMP_DIFF(o.delivered_at, o.created_at, HOUR)) / 24, 2) AS avg_days_to_deliver
FROM `bigquery-public-data.thelook_ecommerce.orders` o
JOIN `bigquery-public-data.thelook_ecommerce.users` u ON u.id = o.user_id
WHERE o.delivered_at IS NOT NULL
GROUP BY u.country ORDER BY avg_days_to_deliver LIMIT 10;

-- revenue by gender and department, 2023
SELECT o.gender, p.department, ROUND(SUM(oi.sale_price), 2) AS revenue
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
JOIN `bigquery-public-data.thelook_ecommerce.orders` o ON o.order_id = oi.order_id
JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id
WHERE oi.status NOT IN ('Cancelled', 'Returned') AND EXTRACT(YEAR FROM oi.created_at) = 2023
GROUP BY o.gender, p.department ORDER BY o.gender, p.department;

-- discount depth: products sold below retail price, share by category
SELECT p.category, ROUND(COUNTIF(oi.sale_price < p.retail_price) / COUNT(*), 4) AS share_sold_below_retail
FROM `bigquery-public-data.thelook_ecommerce.order_items` oi
JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id = oi.product_id
WHERE oi.status NOT IN ('Cancelled', 'Returned')
GROUP BY p.category ORDER BY share_sold_below_retail DESC LIMIT 10;

-- cancelled order share by month in 2024
SELECT FORMAT_TIMESTAMP('%Y-%m', created_at) AS month,
       ROUND(COUNTIF(status = 'Cancelled') / COUNT(*), 4) AS cancelled_share
FROM `bigquery-public-data.thelook_ecommerce.orders`
WHERE EXTRACT(YEAR FROM created_at) = 2024
GROUP BY month ORDER BY month;
