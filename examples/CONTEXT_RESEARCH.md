# What four enterprise-shaped experiments taught us

The useful question is not “does more context help?” It is:

> **Which context, for which demand, survives a clean answer key and a fresh
> holdout without hiding regressions?**

We tested increasingly difficult public proxies. The results changed the
recommended investment more than once.

| workload | intervention | held-out result | decision |
|---|---|---:|---|
| theLook, 7 BigQuery e-commerce tables | authored business rules | +20.0 points, 6 fixed / 0 broke | feasibility signal; synthetic and one repetition |
| BEAVER, 97-table anonymized warehouse | broad rules | -0.7 points on development | reject blanket rule writing |
| BEAVER, fresh 69-question holdout | verified-query examples | +9.7 points, 8 fixed / 1 broke, p=0.0391 | real gain; keep regression visible |
| TPC-DS, 24-table DuckDB stress test | execute-and-retry | +1.7 points on 20 held-out questions, 0 stable fixes / 0 breaks | underpowered; repaired execution more than semantics |
| BIRD, 11 professional-domain databases | targeted expert evidence + catalog docs | **+19.5 points**, 15 fixed / 3 broke, p=0.0044 | real gain on the 25.4% policy-covered cohort |

These are not leaderboard-comparable scores. The datasets, agents, context,
and sample sizes differ. They are a sequence of engineering decisions.

## The learning loop

### 1. Gate quality can dominate model quality

The fresh BEAVER gate retained only 266 of 1,200 sampled questions. The BIRD
gate was cleaner but still flagged 154 of 1,534. A benchmark row that times
out, truncates, omits a required parameter, or returns an unstable limit is not
an agent failure. Treating it as one can erase or invent lift.

### 2. General prose was the weakest lever

The BEAVER rules arm moved development accuracy backward. TPC-DS examples did
not lift a five-question inner evaluation at all. Context volume is not a
strategy: every additional token can distract the agent as easily as inform
it.

### 3. Verified examples help when the task has reusable query shapes

On BEAVER, leak-checked verified queries won candidate selection (+12.8 points,
9 fixed, 0 broke) and then transferred to the fresh holdout (+9.7 points). The
final bank excluded three template twins. That exclusion belongs beside the
result because near-duplicate demand makes transfer look easier.

### 4. Retry repairs mechanics before meaning

On TPC-DS, retry reduced `DID_NOT_RUN` outcomes, but many became executable
queries with wrong values or row counts. The final +1.7-point movement was not
proved and had no stable question-level fix. When triage moves from execution
failure to semantic mismatch, spend the next round on definitions and
relationships—not another syntax retry.

### 5. Context should be routed, and sometimes withheld

BIRD's broad evidence-plus-catalog arm gained +8.8 points on development but
broke 11 stable questions. An inspectable lexical policy found that explicit
formula and literal definitions had the best development tradeoff. Freezing
that policy before a new reserve produced +19.5 points on 65 covered questions.

This is selective deployment, not cherry-picking after the verdict: the policy
was learned on one set, frozen in code, and evaluated once on another. It also
means coverage is only 25.4%. A system that reports lift without coverage makes
a targeted win sound universal.

### 6. A large average is still not a clean win

BIRD fixed 15 stable questions and broke 3. Some domains declined even though
the aggregate rose. BEAVER fixed 8 and broke 1. The deploy decision therefore
has two axes:

* **value:** paired lift, interval, and question-clustered significance;
* **safety:** stable fixed, broken, and unstable question IDs.

Do not collapse those into one green badge.

## What to do on a real estate

1. Pull demand from query history and remove service-account pipelines before
   hashing or sampling.
2. Review questions independently of the evaluated agent; gate every gold.
3. Split before authoring context. Keep enough reserved questions to resolve
   the lift that would change the investment decision.
4. Establish a three-repetition schema-only baseline.
5. Use triage to choose distinct arms: definitions, verified examples, routed
   table docs, relationships, or retry. Do not combine everything first.
6. Select a candidate on working questions against the same baseline.
7. Measure router quality separately. The BIRD result uses annotated relevant
   tables and is explicitly an oracle-routing upper bound.
8. Freeze the intervention and read one fresh holdout once.
9. Report lift and its interval in the same paragraph as broken IDs, coverage,
   template overlap, reviewer provenance, and repetitions.

## Next research, in priority order

1. Replace BIRD's annotated table routing with an actual router and measure the
   end-to-end loss from retrieval errors.
2. Confirm the targeted BIRD policy on a third untouched split or a new BIRD
   release; the current final pool has been consumed.
3. Investigate the three stable regressions with leave-one-context-component
   arms, never by editing and rereading the consumed holdout.
4. Test whether verified examples and targeted definitions are additive on a
   new BEAVER reserve.
5. On customer data, compare gains by analyst group and domain; an aggregate
   win can conceal a high-value segment that regressed.

For exact BIRD provenance, commands, exclusions, cost, and limitations, see
[`bird/ENTERPRISE_RUNBOOK.md`](bird/ENTERPRISE_RUNBOOK.md). For the instant,
dependency-free result tour, run `make example-bird-enterprise`.
