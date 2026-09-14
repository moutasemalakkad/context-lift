Conventions this business follows when answering a question. None of them is
in the schema: a column called `sale_price` does not say which sales count,
and a table called `users` does not say that "customer gender" means its
gender rather than the order's.

Each line below was written after reading failing answers side by side. The
counts in brackets are how many of the 28 failures on the working questions
each one addresses, and they are counts and not proof.

## Rules

- A question asking for a rate, a share or a percentage returns the grouping
  column and the rate, and not the numerator and the denominator it was
  computed from. "return rate by department" is two columns. [13]
- Identify a product, a customer, a brand or a distribution centre by its
  name. Do not return its numeric id unless the question asks for the id. [3]
- Customer attributes - gender, age, country, state, city, traffic source -
  come from the users table. The orders table carries a gender of its own and
  it is not the customer's. [1]
- A year or a month in a question is the date the order line was created,
  unless the question names a different event. "revenue lost to returns in
  2024" means lines created in 2024 that were returned, not lines returned
  in 2024. [1]
- Average markup is the average price minus the average cost, divided by the
  average cost. It is not the average of the per-product markup. [1]
- Round money and percentages to two decimal places. [2]
- Customer age bands are exactly: "under 25", "25 to 39", "40 to 59",
  "60 and over". [1]
