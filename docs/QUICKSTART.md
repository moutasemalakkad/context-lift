# Quickstart

[Documentation](README.md) / Quickstart

Run a small BIRD example locally, see what context fixed and broke, then
choose a guide for your own data.

## Prerequisites

- Python 3.10+, Git, and Make.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) on your path.
- Network access to clone and for first-time environment setup.

The BIRD example itself uses committed SQLite databases and model predictions.
It does not need an API key, cloud account, or network access to score them.

<a id="2-see-a-real-result--no-key-no-cloud-one-minute"></a>

## 1. Run the example

```bash
git clone https://github.com/moutasemalakkad/context-lift
cd context-lift
make example-bird
```

The command audits the answer key, scores predictions, groups failures, and
compares the saved context versions. Its final comparison includes:

```text
paired ablation on 39 questions (execution accuracy)
  without context :  30.8%  [18.6%, 46.4%]
  with context    :  61.5%  [45.9%, 75.1%]
  lift            : +30.8%  [+11.8%, +49.8%]
  context fixed   : 15 questions
  context broke   : 3 questions
  McNemar p       : 0.0075   significant at 0.05
```

## 2. Read the result

The context improved accuracy by **30.8 percentage points**, with a paired 95%
interval of **+11.8 to +49.8**. It fixed 15 questions and broke 3; inspect the
[broken IDs and example qualifications](../examples/bird/README.md#what-you-will-see).

This is an instructional example with **one repetition per arm and no reserved
holdout**. Recomputing its committed predictions does not produce independent
samples. For your own measurement, use three repetitions and reserve questions
before curating context.

## 3. Choose your next step

| Your goal | Continue with |
|---|---|
| Understand a complete experiment | [Walkthrough](WALKTHROUGH.md) |
| Run on SQLite or BigQuery | [Evaluate your data](EVALUATE.md) |
| Prepare business definitions | [Write business context](ONBOARDING.md) |
| Explore a larger benchmark | [Examples](../examples/README.md) |

<a id="1-see-what-a-round-looks-like--no-key-no-cloud-014-seconds"></a>

## Try the toy demo

To run the animated example from the README, install the local package first:

```bash
make install
source .venv/bin/activate
contextlift --workspace /tmp/contextlift-demo demo
```

It builds a seven-row warehouse and compares answers with and without one
business rule. The model answers are canned; the SQL execution and statistics
are real. [Watch the recording](assets/demo.gif) or
[view its still frame](assets/demo-still.png).

<a id="3-point-it-at-your-own-data"></a>

The former warehouse setup section now lives in
[Evaluate your data](EVALUATE.md). Costs, authentication, and troubleshooting
are covered there.

[Documentation index](README.md) · Next: [Evaluate your data](EVALUATE.md)

<details>
<summary>Links to the former warehouse sections</summary>

<a id="start-with-ten-of-your-own-queries-not-two-thousand"></a>

[Start with ten of your own queries, not two thousand](EVALUATE.md)

<a id="then-the-full-pass"></a>

[Then the full pass](EVALUATE.md)

<a id="write-it-down-as-a-script-not-as-a-chat-history"></a>

[Write it down as a script, not as a chat history](EVALUATE.md)

<a id="what-it-costs"></a>

[What it costs](EVALUATE.md)

<a id="what-to-expect-on-your-first-round"></a>

[What to expect on your first round](EVALUATE.md#troubleshooting)

<a id="what-leaves-your-machine"></a>

[What leaves your machine](EVALUATE.md)

</details>
