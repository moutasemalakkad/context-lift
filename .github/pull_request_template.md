## What this changes

<!-- One or two sentences. What does it do that it did not do before. -->

## The run behind any number

<!--
If this change makes the tool print a figure, or changes one, put the command
and its output here. A number without a run behind it cannot be reviewed, and
three of them once reached this project's README.

If it changes no numbers, write "no numbers changed" and delete the rest.
-->

```
```

## How it was verified

<!--
`make test` output, and anything else you ran. If you could not verify part of
it, say which part and why. "I could not check X" is a better review than a
confident guess.
-->

```
make test
```

## What it does not establish

<!--
Optional but encouraged. The limits of a change are part of it: one database,
one model, offline only, untested against a live catalog. Saying so here saves
a reader from assuming more.
-->

---

- [ ] No LLM attribution anywhere in the commits or this description — no
      `Co-Authored-By` naming an assistant, no tool name, no emoji marking
      generated work.
- [ ] Any new figure is recomputed by a test from a committed file.
- [ ] `make test` is green.
