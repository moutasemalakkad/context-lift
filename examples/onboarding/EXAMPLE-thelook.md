# TheLook e-commerce   (`thelook_ecommerce`)

A worked example of `TEMPLATE.md`, filled in for a **public** dataset so it
can ship. Nothing here is anybody's private data.

It is not invented, either: the definitions and exclusions below are the ones
this framework measured a **+50 point** effect from on a 104-question gold
set, and the numbers in brackets are how many of 28 observed failures each
line addressed. See `examples/thelook/`.

---

## What this is for

Online apparel retail: the order lifecycle from browsing session to delivery
or return. Analysts use it for revenue and margin reporting, delivery
performance, return rates, and funnel conversion from session to purchase.
One order can contain several items, and most business questions are about
the **items**, not the orders.

## The tables

- `bigquery-public-data.thelook_ecommerce.order_items`: one row is one item on
  one order. Carries `sale_price` and the item-level `status`. **This is where
  revenue lives** — start here for any money question.
- `bigquery-public-data.thelook_ecommerce.orders`: one row is one order.
  Carries an order-level status and its own `gender` column, which is **not**
  the customer's (see below).
- `bigquery-public-data.thelook_ecommerce.products`: one row is one product,
  with `cost` and `retail_price`. Margin needs this joined to `order_items`.
- `bigquery-public-data.thelook_ecommerce.users`: one row is one customer.
  **All customer attributes come from here** — gender, age, country, state,
  city, traffic source.
- `bigquery-public-data.thelook_ecommerce.inventory_items`: one row is one
  physical unit. A unit not yet sold is on hand.
- `bigquery-public-data.thelook_ecommerce.events`: one row is one session
  event. Conversion questions are answered here.
- `bigquery-public-data.thelook_ecommerce.distribution_centers`: one row is
  one warehouse, with its name.

## Where the data comes from

A single synthetic e-commerce platform, so there are no competing source
systems and no reconciliation questions. On a real estate this section is
where you say "orders from S4 via HANA, product master from MDM" — an agent
uses it to decide which table is authoritative when two of them disagree.

## Words we use that the columns do not

- "Revenue" means the sale price of items that are **neither Cancelled nor
  Returned**, on `...thelook_ecommerce.order_items`. A plain
  `SUM(sale_price)` overstates it.
- "Margin" means sale price minus product cost, joining
  `...order_items` to `...products`.
- "A completed order" means `status` exactly `'Complete'`, on `...order_items`.
- "Delivery time" runs from `shipped_at` to `delivered_at`, delivered items
  only, on `...order_items`.
- "Return rate" is returned items over items that were **not cancelled** —
  the denominator excludes cancellations, on `...order_items`.
- "Average order value" is revenue per completed order.
- "Inventory on hand" means inventory items not yet sold, on
  `...inventory_items`.
- "Purchase conversion" is sessions with a purchase event over all sessions,
  on `...events`.

**Status values are capitalised exactly: `Complete`, `Cancelled`, `Returned`,
`Processing`, `Shipped`.** A query written with `'shipped'` in lower case
returns zero rows, and an empty result looks like an answer rather than a
mistake. This is the single most load-bearing line in the file.

- "Customer gender", and every other customer attribute, comes from
  `...thelook_ecommerce.users`. The `orders` table carries a `gender` of its
  own and it is not the customer's. [1]

## Rows people usually do not want

- Cancelled and returned items, when the question is about revenue, on
  `...order_items`. Include them only when the question is about
  cancellations or returns.
- Cancelled items in the **denominator** of a return rate, on `...order_items`.

## How to answer, not just what the words mean

Three conventions that are about the shape of the answer rather than the
data. They came from reading failing answers side by side, and the counts are
how many of 28 failures each addressed.

- A question asking for a rate, a share or a percentage returns the grouping
  column and the rate — **not** the numerator and denominator it came from.
  "Return rate by department" is two columns. [13]
- Identify a product, customer, brand or distribution centre **by its name**.
  Do not return a numeric id unless the question asks for the id. [3]
- A year or a month in a question is when the order line was **created**,
  unless the question names a different event. "Revenue lost to returns in
  2024" means lines created in 2024 that were returned, not lines returned in
  2024. [1]

## Two questions and their SQL

Both are illustrations of the shape. Neither is in the scored gold set —
`contextlift leak` refuses an example that answers a scored question, and it
caught 25 such twins on one build.

**Q: How much did we sell in each product department last month?**

```sql
SELECT p.department, SUM(oi.sale_price) AS revenue
FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
JOIN `bigquery-public-data.thelook_ecommerce.products` AS p
  ON p.id = oi.product_id
WHERE oi.status NOT IN ('Cancelled', 'Returned')
  AND DATE_TRUNC(DATE(oi.created_at), MONTH)
      = DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH)
GROUP BY p.department
ORDER BY revenue DESC
```

Note what it demonstrates: revenue excludes two statuses, the department
comes from a join, the date is the **created** date, and the answer is two
columns rather than four.

**Q: What share of items get returned, by department?**

```sql
SELECT p.department,
       COUNTIF(oi.status = 'Returned') / COUNT(*) AS return_rate
FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
JOIN `bigquery-public-data.thelook_ecommerce.products` AS p
  ON p.id = oi.product_id
WHERE oi.status != 'Cancelled'
GROUP BY p.department
ORDER BY return_rate DESC
```

Note the denominator excludes cancellations, the status strings are
capitalised exactly, and the result is the grouping column plus the rate —
not the counts behind it.
