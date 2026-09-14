# Security

## Reporting

Report privately through GitHub's **Report a vulnerability** button on the
Security tab of this repository. That opens a private advisory only the
maintainers can read.

Please do not open a public issue for a vulnerability. If the button is
unavailable to you, open a normal issue saying only *"security report, please
contact me"* with no details, and someone will find a private channel.

Expect a first reply within a week. This is a small project; there is no
paid on-call and pretending otherwise would set an expectation it cannot meet.

## What is in scope

This tool runs SQL written by a language model against a warehouse you point
it at. Most of its security surface is about keeping that contained.

**The read-only rail.** `executor.py` refuses any statement that writes or
changes schema — `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`,
`MERGE`, `GRANT`, `REVOKE`, `ATTACH`, `DETACH`, `VACUUM`, `PRAGMA`, and
`TRUNCATE`/`REPLACE` when used as statements rather than as functions. The
keywords are refused anywhere in the statement, not only at the start, so a
data-changing CTE such as `WITH t AS (DELETE ...)` cannot slip past. A `SELECT`
containing the literal word `DROP` inside a string is refused too; for
untrusted model output that is the safe direction to be wrong in. **A way past
this rail is a vulnerability.**

**Calls that leave the engine.** `EXTERNAL_QUERY`, `AI.*` and the remote `ML.*`
functions are refused, because they run somewhere else or send your rows to a
model. A way to make the tool issue one of these is a vulnerability.

**`share`'s allowlist.** `share` exists to hand someone a run without handing
them your data. A field reaching a shared artifact that the allowlist does not
name is a vulnerability.

**`scan`.** `make scan` refuses a commit carrying a customer string. A pattern
that should match and does not is a defect worth reporting privately, because
the failure is silent by construction.

**Credential handling.** The tool should never print a token, an API key or a
full credential path. If you can make it do so, that is a report.

## What is out of scope

- The warehouse's own permissions. Point the tool at a role that can only read
  what you are willing to have read; the read-only rail is a second line, not
  the first.
- Cost. A query that scans more than you expected is a billing surprise, not a
  vulnerability. `round --cost` and `gate --estimate-only` exist for that.
- A model writing wrong SQL. That is the thing this tool measures.
- Denial of service against your own warehouse by running a large gold set.

## Handling your data

Nothing is sent anywhere except the model endpoint and the warehouse you
configure. There is no telemetry, no usage beacon and no crash reporter.

Evidence files under `docs/evidence/` are committed on purpose and are scrubbed
of project identifiers before they are written; a test asserts no committed
evidence file names a project or a customer.
