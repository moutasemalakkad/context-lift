# Run identity and job labels

[Documentation](README.md) / Run identity and job labels

This page covers service-call identification. For crediting or blaming a
context rule, see [Rule attribution](METHODOLOGY.md#6b-attribution-from-broke-3-to-this-rule-broke-it).

## On this page

- [The two things answer two different questions](#the-two-things-answer-two-different-questions)
- [The user agent](#the-user-agent)
- [Who started the run](#who-started-the-run)
- [What each service gives you, which is not the same everywhere](#what-each-service-gives-you-which-is-not-the-same-everywhere)
- [The BigQuery job labels](#the-bigquery-job-labels)
- [Turning it off](#turning-it-off)

Every request this tool sends to Google carries two pieces of text it did not
have to: a **user agent** naming the tool, and **job labels** on each BigQuery
job. This page says what they are, who can read them, and how to turn them
off.

Nothing here is telemetry. No extra request is sent, no new endpoint is
contacted, and no data leaves your network that was not already going to
Google in the request you asked for. Both are names written on letters that
were being posted anyway.

One naming note, so nothing here reads as a mismatch. The reported name is
`context-lift`, which is the name this is published under. The source
directory is still `contextlift`, the name it was built under. The reported
name is the published one on purpose: attribution to a name nobody can
install is worth nothing, and a tool that renames what it reports later
splits its own usage history in two.

## The two things answer two different questions

| Question | Mechanism | Who can read it |
|---|---|---|
| "What did running this tool cost **us**?" | BigQuery job labels | only you, in your own project |
| "Which projects run this tool at all?" | the user agent | Google, in its own request records |

Labels cannot answer the second question, because nobody outside your project
can read them. The user agent cannot answer the first, because you cannot read
Google's request records. So there are two.

## The user agent

```
context-lift/0.1.0 context-lift-caller-terminal/0.1.0 gl-python/3.14.6 grpc/1.83.1 gax/2.34.0
```

The first token is this tool and its version. The second says who started the
run. The rest is added by Google's own client library and would be there
regardless.

Every token is `name/version`, and a name holds only lower-case letters,
digits and hyphens. That is required, not stylistic: the software that reads
these strings extracts its fields from that exact shape, and a token written
any other way is filed under no name at all. It is why who-started-the-run is
part of the name (`context-lift-caller-terminal`) rather than sitting where a
version belongs.

If you wrap this tool in your own code and want your own name in the string
too, add it rather than losing ours:

```bash
export CONTEXT_LIFT_USER_AGENT_EXTRA="your-tool/1.4.2"
```

This is the field Google documents for the purpose. The BigQuery client's
`client_info` parameter is described as the thing to set "if you're developing
your own library or partner tool", and the Google Cloud Ready – BigQuery
partner programme requires partners to set one so usage is attributed
correctly. dbt and the BigQuery Jupyter magics both set one.

**What it does not contain:** no account, no email, no project name, no query
text, no table names, no hostname. Two strings and a version number.

It goes on every Google client this tool opens, not only BigQuery: the
Knowledge Catalog client, the business glossary client, the Conversational
Analytics client, and the Gemini client used to draft questions. A count taken
from one service is wrong by however much the others are used, and wrong in a
way nobody can see from outside. `tests/test_identity.py` fails if a new
client is added without one.

## Who started the run

The second token is one of four words:

| word | what it means |
|---|---|
| `terminal` | a person, at a keyboard, with a terminal attached |
| `agent` | a coding agent drove it (Claude Code, Gemini CLI, and similar) |
| `ci` | a continuous-integration job |
| `script` | no terminal and no agent: cron, a Makefile, something piped |

This exists for one reason. Adoption because a person read the documentation
and adoption because an agent found the library and used it look identical in
a usage count, and they are different stories that call for different work.

It is read from the environment: `AI_AGENT`, `CLAUDECODE` and
`CLAUDE_CODE_ENTRYPOINT` were observed set on a machine running Claude Code;
`CI` is near-universal. An agent marker wins over the terminal check, because
a host agent usually runs commands with a terminal attached and would
otherwise be counted as a person.

If your host is not on the list, name yourself and the guessing stops:

```bash
export CONTEXT_LIFT_CALLER=agent      # or terminal, ci, script
```

## What each service gives you, which is not the same everywhere

The user agent behaves the same on every Google service. What **you** can see
about this tool's cost does not, and the difference is not ours to fix:

| service this tool calls | what you can attribute to this tool | how |
|---|---|---|
| BigQuery | the exact bytes, and the cost | job labels (below) |
| Knowledge Catalog, business glossary | **nothing at tool level** | Dataplex API calls carry no per-request billing labels, so your bill shows the Dataplex API as one line |
| Conversational Analytics, Vertex AI | nothing at tool level | same reason: no per-request label |
| Looker, if you use the Looker reader | the API call count | a custom user agent appears in Looker's own System Activity, under API Usage |

So the honest summary: for BigQuery you get a number, and for the catalog and
the model you get the service-level line on your bill and nothing finer. We
say so rather than let the section below imply otherwise. If the catalog
calls matter to you, they are cheap and few — a handful of reads per table
per round — and the BigQuery figure is where the money is.

One extra note if you use the Looker reader. When Looker generates SQL and
runs it against BigQuery, BigQuery sees **Looker** as the caller, not this
tool. Turning on Looker's context comments makes Looker prepend a comment to
the generated SQL, which then shows up in your `INFORMATION_SCHEMA.JOBS`
query text. That is the only way to trace those warehouse jobs back here.

## The BigQuery job labels

Every job, dry runs included:

```
app=context-lift  caller=terminal  stage=mine
```

`stage` is the command that ran. They appear in two places, and the
difference matters:

- **Your job history**, always. `INFORMATION_SCHEMA.JOBS_BY_PROJECT` carries
  a `labels` column with no setup on your part. This gives you bytes.
- **Your Cloud Billing export, only if it is the Detailed export.** Job
  labels reach the detailed billing data, which carries resource-level cost.
  They do **not** appear in the Standard billing export. So if you want
  dollars rather than bytes, the detailed export has to be switched on
  first.

One `WHERE` clause then separates this tool's spend from dbt's, from
Looker's, and from a person querying by hand:

```sql
SELECT
  (SELECT l.value FROM UNNEST(labels) l WHERE l.key = 'stage') AS stage,
  COUNT(*) AS jobs,
  SUM(total_bytes_billed) / POW(2, 40) * 6.25 AS usd
FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE EXISTS (SELECT 1 FROM UNNEST(labels) l
              WHERE l.key = 'app' AND l.value = 'context-lift')
  AND creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY stage ORDER BY usd DESC
```

That is the query that tells you mining cost $1.94 and the round cost $0.40,
rather than one number for the whole tool. It reads your job history, so it
needs no billing export at all — the byte figures are priced at the on-demand
rate in the query itself.

There is a reason this page exists at all, and it is not politeness. While
building this tool we attributed $10.84 of BigQuery spend to one of our own
runs. The real figure was $1.94; the rest belonged to five other people
querying the same shared project during the same hour. We had filtered job
history by time window, because there was nothing else to filter on. The
`app` label is what makes that query correct the first time, for you as much
as for us.

## Turning it off

Both are set in one file, `src/context_lift/identity.py`. Editing `NAME` or
returning `None` from `client_info()` removes the user agent; returning `{}`
from `job_labels()` removes the labels. Nothing else in the tool reads them,
and nothing breaks.

This is said plainly because it is true and because pretending otherwise would
be worse: anyone who forks this can remove the string in one line, and then
their usage is not counted. That is the nature of attribution. It is a count
of people who did not mind being counted.

[Documentation index](README.md) · Next: [Security and data](SECURITY-AND-DATA.md)
