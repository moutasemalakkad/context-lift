# Business rules taken out of the questions

Each line was written in brackets on a query's comment in
`queries.sql`. A definition inside a question is context the bare arm
would get for free, so no written context could ever show a lift.
These belong in the spec repo, under `rules/` and `vocabulary/`.

- revenue = sale price of items neither cancelled nor returned
- margin = sale price minus product cost
- completed = status 'Complete'
- shipped_at to delivered_at, delivered items only
- returned items over items that were not cancelled
- value = revenue per completed order
- on hand = inventory items not yet sold
- sessions with a purchase event over all sessions
