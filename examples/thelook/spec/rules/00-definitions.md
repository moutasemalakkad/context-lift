Every line below was written in brackets on a query's comment in
`queries.sql`, then taken out of the question by `make_golds.py`. Left in the
question, the model gets the rule for free and no written context can show a
lift.

- Revenue is the sale price of items that are neither Cancelled nor Returned.
- Margin is sale price minus product cost.
- A completed order has status exactly 'Complete'.
- Delivery time runs from `shipped_at` to `delivered_at`, delivered items only.
- Return rate is returned items over items that were not cancelled.
- Average order value is revenue per completed order.
- Inventory on hand is inventory items not yet sold.
- Purchase conversion is sessions with a purchase event over all sessions.

Status values are capitalised exactly: Complete, Cancelled, Returned,
Processing, Shipped. A query written with 'shipped' in lower case returns zero
rows and looks like an empty result rather than a mistake.
