# Cold run: prove the loop as an analyst, not as a model

Instructions for an agent doing a cold run. A cold run answers one question:
can an ordinary analyst, using only the commands in this repository, find
out whether business context makes a data agent more accurate?

Every earlier run of this loop was driven by someone who had read the
source. That is not the test. This one is.

## Who you are for this run

You are a data analyst. You know SQL and you know your business. You have
not read this repository's source code and you will not read it. You read
`README.md`, `AGENTS.md` and `--help`, and nothing
else.

**The hard rule: do not be clever.** Every time you feel the urge to work
around the tool, stop. Write down what you wanted to do and why, then do
the plain thing the tool told you to do, even if you think it is wrong. A
run where you fixed things by hand proves nothing, because no analyst will
do what you did.

In particular you may not:

- write or edit a rule by hand, or repair a draft the tool produced
- write SQL to patch around a command that refused
- skip the review step, or approve every row without reading it
- read the reserved questions before the end, or look at them twice
- edit any file under `src/`, or run anything from `src/` directly
- decide a number is wrong and recompute it another way

If a command refuses, times out, prints something you cannot act on, or
gives you a number you cannot explain, that is a finding. Record it and
carry on with what the tool offers next.

## Step 0: find a dataset

You need real data, not a toy. Pick one and say why you picked it.

What makes a good candidate:

- **Real business data**, public and freely redistributable. Check the
  licence before you download anything.
- **Meaning that lives outside the schema.** The point of the exercise is
  context, so the data must hold facts a column name does not tell you:
  status codes, stored spellings and letter case, a metric that is a
  formula, a default filter every analyst applies, a join nobody guesses.
  If a fluent reader can answer every question from the table names alone,
  the dataset cannot show anything.
- **Real questions, or a real query log.** Best is a set of questions
  people actually asked with verified SQL. Second best is a query log you
  can mine with `contextlift mine`. Worst is questions you invent, because you
  will invent the ones you can answer.
- **Enough questions.** Aim for 60 or more. The tool will tell you what a
  smaller set can prove; at 12 questions it can only prove a lift above
  about 29 points.
- **Small enough to run.** It should score locally on SQLite, or cost a few
  dollars on a warehouse. Say the expected cost before you start.

Public sets worth considering: BIRD, Spider, KaggleDBQA, WikiSQL, BEAVER,
and any government or industry open-data warehouse with a published query
history. This repository already ships BIRD and KaggleDBQA examples, so
prefer something they do not cover, or you are re-running a known result.

Write your choice, the licence, the question count and the reason into
`DATASET.md` in your working directory before you run anything.

## Step 1: run the loop

Start here and let the tool lead:

```bash
contextlift --workspace WS next
```

`next` prints the step, why it matters, and the exact command. Run the
command it prints. Then run `next` again. Repeat until it says the loop is
closed.

The order it will walk you through:

| stage | what it answers |
|---|---|
| `gate` | is the answer key itself sound? |
| `split` | which questions are reserved, and never looked at until the end |
| `schema` | what the agent is told about the tables |
| `generate` + `score` | how accurate is the agent with no context at all |
| `triage` | what kind of wrong were the failures |
| `draft-rules` | the rules the largest failure group asks for |
| `context-write` | install the draft as the context file |
| `leak` | does the context contain the reserved answers |
| `generate` + `score` | the same questions again, with the context |
| `ablate` | did it help, and is the difference real |
| the reserved reading | the number you report, read once |
| `bundle` | the verdict as one page |

Before every command that costs money, run it once with `--estimate-only`,
write the estimate down, then run it with `--budget` set. Record the
estimate and what it actually spent. They will differ; that difference is
data.

## Step 2: what to record

Keep one file, `RUN.md`, and write to it as you go, not afterwards.

For every stop, write four things:

```
STOP <n>: <one line saying what went wrong>
command: <the exact command you ran>
output:  <the exact lines it printed, quoted>
expected: <what the docs or the tool led you to expect>
what you did next: <and whether you had to be clever>
```

For every number, write the command that produced it. A number without its
command is not a result.

At the end, write:

- the two accuracy rates, with their intervals
- the lift, its interval, how many questions were fixed and how many broke,
  and the p-value
- the ruling word the tool printed
- the reserved reading, separately, and confirm you looked once
- the total spend, model and warehouse separately
- every stop, in order

## Step 3: the honest ending

Report what happened, not what would have been nice.

- No lift is a normal result and a useful one. If the context did not help,
  say so and say what the failures actually were.
- A number that is not significant is not a lift. Quote the tool's ruling
  word, not your own summary of it.
- If you had to be clever to finish, that run does not count. Say where.

## What a good cold run produces

Three files in your working directory: `DATASET.md`, `RUN.md`, and the
verdict page `bundle` writes. Plus the workspace, which holds every command
you ran and its output.

The measure of success is not a large lift. It is that an analyst got from
a warehouse to a defensible number without anyone helping them, and that
every stop on the way is written down so it can be fixed.
