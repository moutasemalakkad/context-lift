# <Dataset display name>   (`<dataset_id>`)

<!--
One file per DATASET, not per table. Ten minutes for somebody who knows the
data. Delete any section you have nothing true to put in: an empty heading is
better than a guess, and a wrong rule measured worse than a missing one.

Every section that names data must name it as project.dataset.table. A bare
table name cannot tell two copies of the same data apart, and on one estate
that alone made two answer keys unscoreable.

Read docs/ONBOARDING.md for why each section is here and what it measured.
-->

## What this is for

<2-4 sentences. What business process does this describe? Who uses it, to
answer what? Write it for somebody who has never opened the tables.>

## The tables

<Fully qualified, every time. Say what ONE ROW is.>

- `project.dataset.table`: one row is <...>. Used for <...>.
- `project.dataset.table`: one row is <...>. Used for <...>.

## Where the data comes from

<Source systems, in your words. An agent cannot guess this and the schema
does not say it. It is also how an agent knows which table wins when two
disagree.>

## Words we use that the columns do not

<Business word -> the column or condition, AND the table it applies to.>

- "<business word>" means <column or condition>, on `project.dataset.table`.

## Rows people usually do not want

<Exclusions. The highest-value thing your team knows and the least likely to
be written down anywhere.>

- <filter>, on `project.dataset.table`, because <reason>.

## Two questions and their SQL

<The section people skip and the one that measured largest. Real questions
somebody asked last week, with the SQL your team would write. NOT questions
you plan to score the agent on -- an example that answers a scored question
makes the score meaningless, and `contextlift leak` will refuse it.>

**Q: <a real question, in the words somebody used>**

```sql
-- your SQL
```

**Q: <a second one, a different shape>**

```sql
-- your SQL
```
