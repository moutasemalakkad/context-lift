# Write business context

[Documentation](README.md) / Write business context

Write down the definitions an agent cannot infer from a schema. Each rule
should identify the business concept, the table it applies to, and the exact
condition or calculation. Measure whether it helps before adopting it.

**Prerequisites:** an agreed dataset scope, a data owner who can verify the
rules, and a scored baseline over the working questions. Use
[Evaluate your data](EVALUATE.md) for those steps.

## On this page

- [Choose what to document](#1-choose-what-to-document)
- [Use the dataset template](#2-use-the-dataset-template)
- [Review the draft](#3-review-the-draft)
- [Install and measure](#4-install-and-measure)
- [Evidence and field notes](#evidence-and-field-notes)

## 1. Choose what to document

Start with the agreed scope and the baseline's working-set failures. Read a
few failing question/SQL pairs together. A group of rate questions returning
extra columns may need one reporting convention rather than many examples.

Use [query demand](EVALUATE.md#bigquery) or an owner-provided dataset list to
choose scope. Confirm that the schema shown to the agent includes the tables
needed by the questions. Scope cannot be inferred from dataset names.

If you have a LookML project, inspect the descriptions already present:

```bash
contextlift --workspace /path/to/evaluation semantic --lookml /path/to/lookml
```

Definitions, join paths, and written descriptions are different inputs. The
[BI-model case study](CASE_STUDY.md#measure-the-description-share-before-counting-on-a-bi-model-0-of-9-20-points)
shows why measuring the descriptions matters.

## 2. Use the dataset template

Start from the [dataset template](../examples/onboarding/TEMPLATE.md).
See the [completed theLook example](../examples/onboarding/EXAMPLE-thelook.md)
for the intended level of detail. Use one page per dataset and fill in:

| Section | What to write |
|---|---|
| Purpose | The business process and the questions this dataset answers |
| Tables | Fully qualified names and what one row represents |
| Sources | Where the data comes from and which source is authoritative |
| Business terms | Map each term to a column or condition, naming its table |
| Exclusions | Rows the metric excludes, with the business reason |
| Worked questions | Reviewed examples drawn only from the permitted example pool |

### Make each rule specific

```text
On `project.sales.orders`, net revenue is SUM(order_total - refund_total).
Exclude rows where order_type = 'TEST'.
Report one row per calendar month using the order_date column.
```

This is an illustrative format, not a rule to copy into your warehouse.
Check the real names, literals, grain, and metric definition with the owner.
Avoid a formula such as “sum(a) - sum(b)” without naming what it measures.

### Treat examples as a separate choice

An example needs to be relevant to the question receiving it. A bank sent to
every question can distract the agent; a matched example can help. Keep the
example pool disjoint from scored questions and respect the template-twin
checks. A question must never be its own example.

For curation-set iteration, split the working set again if needed to build a
disjoint example pool. Do not read holdout answers to choose examples. See
[the worked-example findings](PRACTICES.md#13-an-example-handed-to-the-wrong-question-is-a-distractor-measured).

## 3. Review the draft

Before installing context, have a person check:

- Does every rule have a subject and a table scope?
- Are metric definitions, status values, and exclusions verified?
- Is the reporting grain explicit where it matters?
- Does a rule express reusable business knowledge rather than an answer to
  one evaluation question?
- Are examples relevant, permitted, and separate from the questions scored?
- Are the sources and any unresolved ambiguities recorded?

`draft-rules` and `draft-context` produce drafts. Estimate paid drafting first,
then use an approved budget. A draft is not ready merely because it parses;
a person must review it before `context-write` installs it.

## 4. Install and measure

Use the workspace containing the baseline. After reviewing the draft:

```bash
contextlift --workspace /path/to/evaluation context-write \
  --source-file /path/to/evaluation/context/rules_draft.md \
  --to /path/to/evaluation/context/rules.md
contextlift --workspace /path/to/evaluation leak \
  --predictions /path/to/evaluation/holdout.csv \
  --context /path/to/evaluation/context/rules.md
contextlift --workspace /path/to/evaluation next
```

Follow the [comparison steps](EVALUATE.md#5-write-and-compare-context): answer
the same working questions with context, score each repetition once, and
compare with the baseline reports. Later `context-write` calls replace the
working file unless you use `--add`; the workspace retains its history.

**Expected result:** a versioned context file, a leak check, and a measured
working-set comparison. Report both gains and broken questions. Use attribution
to investigate a rule, then confirm it with an arm that removes that rule.

For a catalog arm, read [Context sources and catalogs](CONTEXT-SOURCES.md).
A catalog may serve fewer context channels than a file-based arm, so inspect
what is read back before comparing the conditions.

## Evidence and field notes

The original guide mixed the writing checklist with dated experiments and
operational incidents. Those observations, including the worked-example
correction and service-account deployment record, are preserved in
[Field notes](FIELD-NOTES.md#context-writing-observations).

Use [Case studies](CASE_STUDY.md) for source-backed benchmark results and
[Practices](PRACTICES.md) for the evidence behind the recommendations.

[Documentation index](README.md) · Next: [Compare your context](EVALUATE.md#5-write-and-compare-context)

<details>
<summary>Links to the former sections</summary>

<a id="if-you-only-have-one-hour"></a>

[If you only have one hour](FIELD-NOTES.md#if-you-only-have-one-hour)

<a id="first-pick-the-datasets-do-not-guess-this"></a>

[First: pick the datasets. Do not guess this.](FIELD-NOTES.md#first-pick-the-datasets-do-not-guess-this)

<a id="before-any-of-it-we-already-have-looker-cant-you-just-read-that"></a>

[Before any of it: "we already have Looker, can't you just read that?"](FIELD-NOTES.md#before-any-of-it-we-already-have-looker-cant-you-just-read-that)

<a id="then-per-dataset-in-this-order"></a>

[Then, per dataset, in this order](FIELD-NOTES.md#then-per-dataset-in-this-order)

<a id="corrected-2026-09-12-and-the-correction-is-the-finding"></a>

[Corrected 2026-09-12, and the correction is the finding](FIELD-NOTES.md#corrected-2026-09-12-and-the-correction-is-the-finding)

<a id="what-to-tell-somebody-who-has-15-minutes"></a>

[What to tell somebody who has 15 minutes](FIELD-NOTES.md#what-to-tell-somebody-who-has-15-minutes)

<a id="why-add-some-business-rules-fails-as-an-instruction"></a>

[Why "add some business rules" fails as an instruction](FIELD-NOTES.md#why-add-some-business-rules-fails-as-an-instruction)

<a id="the-template-one-page-per-dataset"></a>

[The template: one page per dataset](FIELD-NOTES.md#the-template-one-page-per-dataset)

<a id="why-each-section-is-there"></a>

[Why each section is there](FIELD-NOTES.md#why-each-section-is-there)

<a id="what-this-is-for--2-to-4-sentences"></a>

["What this is for" — 2 to 4 sentences](FIELD-NOTES.md#what-this-is-for--2-to-4-sentences)

<a id="the-tables--fully-qualified-every-time"></a>

["The tables" — fully qualified, every time](FIELD-NOTES.md#the-tables--fully-qualified-every-time)

<a id="where-the-data-comes-from"></a>

["Where the data comes from"](FIELD-NOTES.md#where-the-data-comes-from)

<a id="words-we-use-that-the-columns-do-not--and-name-the-table"></a>

["Words we use that the columns do not" — and name the table](FIELD-NOTES.md#words-we-use-that-the-columns-do-not--and-name-the-table)

<a id="rows-people-usually-do-not-want"></a>

["Rows people usually do not want"](FIELD-NOTES.md#rows-people-usually-do-not-want)

<a id="two-questions-and-their-sql--do-not-skip-this"></a>

["Two questions and their SQL" — do not skip this](FIELD-NOTES.md#two-questions-and-their-sql--do-not-skip-this)

<a id="what-not-to-write-each-one-measured"></a>

[What NOT to write, each one measured](FIELD-NOTES.md#what-not-to-write-each-one-measured)

<a id="getting-it-into-the-agent"></a>

[Getting it into the agent](FIELD-NOTES.md#getting-it-into-the-agent)

<a id="then-measure-and-check-the-set-can-answer-before-you-pay"></a>

[Then measure, and check the set can answer before you pay](FIELD-NOTES.md#then-measure-and-check-the-set-can-answer-before-you-pay)

<a id="running-it-without-a-persons-credential"></a>

[Running it without a person's credential](FIELD-NOTES.md#running-it-without-a-persons-credential)

</details>
