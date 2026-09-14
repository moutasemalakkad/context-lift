# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The pinned comparison as one static file: verdict.html plus verdict.json.

`collect` reads a workspace (index, manifests, saved reports, prediction
copies), checks that the two pinned arms differ only in context, and runs
the library's own ablate / triage / attribute in-process. `render` turns
that into HTML with inline CSS and no script, so the file opens from
file:// in any mail client or locked-down browser. Numbers are computed
here, by the CLI, never by a page. Exports: collect, render, write_bundle,
bundle_inventory.

What leaves with the file: every question, the answer key's SQL and the
model's, the first rows of both result sets for each wrong answer, the
rules file, and in verdict.json the --db spec (the billing project on
BigQuery, the path on SQLite). `include_rows=False` (`bundle --no-rows`)
drops the row previews; `bundle_inventory` counts the rest so the CLI can
say what is inside before anyone sends it on.
"""
from __future__ import annotations

import csv
import dataclasses
import html
import json
import pathlib
import string
from collections import Counter

from . import __version__
from .ablate import (UNGATED_WORD, AblationReport, ablate, pool, safety_word,
                     verdict_word)
from .attribute import attribute, parse_rules
from .backends_db import parse_db_spec
from .harness import EvalReport, load_report
from .metrics import McNemarResult, Proportion, proportion
from .review import NO, YES, answer_key_caveat
from .triage import BUCKET_MEANINGS, triage
from .workspace import (answer_key_review, fully_scored, has_context, input_paths,
                        only_holdout, rebuild_index, reserved_pair)

GATE_MEANINGS = {
    "NON_SELECT": "not a read-only SELECT/WITH",
    "PLACEHOLDER_COMMENT": "the SQL is a placeholder or comment, not a query",
    "EXEC_FAIL": "the gold raises when executed; fix the SQL",
    "OVER_BUDGET": "the byte ceiling turned the query away before it ran; "
                   "raise --max-bytes or narrow the question",
    "NO_ACCESS": "the platform refused the call (403); whoever owns the policy fixes it, "
                 "not the query's author",
    "TABLE_GONE": "the query names a table that no longer exists; drop the gold or "
                  "point it at the replacement",
    "TIMEOUT": "the gold ran past the time bound",
    "ZERO_ROWS": "the gold returns no rows; often a wrong filter or stale data",
    "DEGENERATE": "the gold returns a single trivial cell (NULL or 0)",
    "TRUNCATED": "the gold hit the row cap",
    "UNSTABLE": "two runs returned different rows (non-deterministic gold)",
    "TEXT_AGGREGATE": "sums or averages a text column; the ranking is meaningless",
}


# the luck sentence when the defended p is 1: the terminal report and
# verdict.html all print this one
EVEN_SWING = ("This swing is exactly what chance produces: as many questions moved one way "
              "as the other, or too few moved to tell.")


def fmt_p(p: float) -> str:
    """Two significant figures, formatted once here so the terminal and the
    file never disagree (0.625 printed as 0.63 on one and 0.62 on the other)."""
    return f"{p:.2g}"


def _manifest(workspace: pathlib.Path, run_id: str) -> dict:
    return json.loads((workspace / "runs" / run_id / "manifest.json").read_text())


def _arm_material(workspace: pathlib.Path, index: dict, arm: dict) -> dict:
    """Every scored rep of one arm: the report, and its predictions by id."""
    runs = {r["run_id"]: r for r in index["runs"]}
    # One score per distinct predictions file, the newest: re-scoring the
    # same file is a duplicate observation, not a new repetition.
    newest_by_predictions: dict[str, str] = {}
    for run_id in sorted(arm["score_runs"], key=lambda rid: runs[rid]["started_at"] or ""):
        newest_by_predictions[runs[run_id]["inputs"]["predictions"]] = run_id
    score_runs = sorted(newest_by_predictions.values(),
                        key=lambda rid: runs[rid]["started_at"] or "")
    if not score_runs:
        raise SystemExit(f"arm {arm['arm_id']} ({arm['label']}) has no scored run; "
                         "run `contextlift score` on its predictions first")
    reports: list[EvalReport] = []
    predictions: list[dict[str, tuple[str, str, str]]] = []
    db_hashes: set[str] = set()
    for run_id in score_runs:
        manifest = _manifest(workspace, run_id)
        run_dir = workspace / "runs" / run_id
        report_copy = manifest["outputs"]["save_report"][0]["copy"]
        reports.append(load_report(run_dir / report_copy))
        preds_copy = manifest["inputs"]["predictions"]["copy"]
        with open(run_dir / preds_copy, newline="") as handle:
            predictions.append({
                row["id"]: (row["question"], row["gold_sql"], row["pred_sql"])
                for row in csv.DictReader(handle)
            })
        db_sha = (manifest["inputs"].get("db") or {}).get("sha256")
        if db_sha:
            db_hashes.add(db_sha)
    context_text = None
    first = _manifest(workspace, arm["generate_runs"][0])
    context_record = first["inputs"].get("context")
    if context_record and context_record.get("copy"):
        context_text = (workspace / "runs" / first["run_id"]
                        / context_record["copy"]).read_text()
    on_holdout = any(runs[rid]["on_holdout"] for rid in arm["generate_runs"])
    return {"arm": arm, "score_runs": score_runs, "reports": reports,
            "predictions": predictions, "db_hashes": db_hashes,
            "context_text": context_text, "on_holdout": on_holdout,
            "retrieval": _retrieval_of(workspace, first, predictions)}


def _retrieval_of(workspace: pathlib.Path, manifest: dict,
                  predictions: list[dict]) -> dict | None:
    """What a retriever served the arm, or None when nothing was retrieved:
    the input file's hash (a leak run is matched by it), how many scored
    questions got text and how many got nothing. The counts come from the
    manifest's telemetry when generate recorded them, else from the file
    copy against the ids that were scored."""
    record = manifest.get("inputs", {}).get("context_per_question")
    if not record or not record.get("copy"):
        return None
    ids = list(predictions[0]) if predictions else []
    counts = (manifest.get("telemetry") or {}).get("retrieval")
    if not (isinstance(counts, dict) and {"served", "nothing"} <= set(counts)):
        mapping = json.loads((workspace / "runs" / manifest["run_id"] / record["copy"]).read_text())
        served = sum(1 for qid in ids if mapping.get(qid))
        counts = {"served": served, "nothing": len(ids) - served}
    return {"input_sha256": record["sha256"], "path": record["path"],
            "n_questions": len(ids), "served": counts["served"], "nothing": counts["nothing"]}


def _luck_text(p: float) -> str:
    """How often luck alone would do this, in 100 tries, as words. Below half a
    percent there is no honest integer: "0 in 100" says it cannot happen."""
    if p >= 0.995:
        return "about 100"
    if p < 0.005:
        return "fewer than 1"
    return str(round(p * 100))


def _identity_check(base: dict, cand: dict) -> str:
    """The sentence a paired test needs, or a refusal naming what differs."""
    differences = []
    b_arm, c_arm = base["arm"], cand["arm"]
    if b_arm["questions_sha256"] and c_arm["questions_sha256"] \
            and b_arm["questions_sha256"] != c_arm["questions_sha256"]:
        differences.append("questions file")
    if b_arm["model"] != c_arm["model"]:
        differences.append(f"model ({b_arm['model']} vs {c_arm['model']})")
    # an agent run through --backend command: the same program, the same
    # version, on both sides, or the difference is the agent and not the context
    for field, name in (("backend", "backend"), ("runner_version", "agent version")):
        if b_arm.get(field) != c_arm.get(field):
            differences.append(f"{name} ({b_arm.get(field)} vs {c_arm.get(field)})")
    if len(base["db_hashes"] | cand["db_hashes"]) > 1:
        differences.append("database")
    if differences:
        raise SystemExit("cannot compare: the arms differ in "
                         + ", ".join(differences)
                         + ", not only in context; the paired test would be invalid")
    who = "agent" if c_arm.get("backend") == "command" else "model"
    return ("the two arms differ only in the context given to the model: "
            f"{b_arm['label']} vs {c_arm['label']}; same questions, same {who}, "
            "same database")


def _newest(index: dict, command: str, predicate=lambda run, manifest: True,
            workspace: pathlib.Path | None = None):
    runs = [r for r in index["runs"] if r["command"] == command and r["finished"]]
    for run in sorted(runs, key=lambda r: r["started_at"] or "", reverse=True):
        manifest = _manifest(workspace, run["run_id"]) if workspace else {}
        if predicate(run, manifest):
            return run, manifest
    return None, None


def _ablation_from_dict(data: dict) -> AblationReport:
    """An ablate run's saved report (`--json` payload) back as the dataclass,
    so a reading saved by the CLI prints through the same summary, word and
    sentences as a fresh one."""
    return AblationReport(
        without_context=Proportion(**data["without_context"]),
        with_context=Proportion(**data["with_context"]),
        lift=data["lift"], mcnemar=McNemarResult(**data["mcnemar"]),
        n_paired=data["n_paired"], adjusted=data.get("adjusted", False),
        broke_ids=tuple(data.get("broke_ids") or ()), fixed_ids=tuple(data.get("fixed_ids") or ()),
        n_questions=data.get("n_questions"), n_reps=data.get("n_reps"),
        clustered=McNemarResult(**data["clustered"]) if data.get("clustered") else None,
        per_question=tuple(data.get("per_question") or ()),
        churn=data.get("churn"), reps_identical=data.get("reps_identical"))


def _ablate_reading(ablation: AblationReport) -> dict:
    """One ablation as the texts a page prints, so no caller rounds twice."""
    low, high = ablation.lift_interval()
    word, p_defended = verdict_word(ablation)
    test = ablation.defended
    return {"word": word, "p_text": fmt_p(p_defended),
            "without_text": f"{ablation.without_context.point:.1%}",
            "with_text": f"{ablation.with_context.point:.1%}",
            "lift_text": f"{ablation.lift:+.1%}".replace("%", " pts"),
            "lift_interval_text": f"[{low:+.1%}, {high:+.1%}]".replace("%", ""),
            "fixed": test.c, "broke": test.b,
            "n_questions": ablation.n_defended, "n_reps": ablation.n_reps or 1,
            "sentences": ablation.verdict_sentences(),
            "broke_ids": list(ablation.broke_ids), "fixed_ids": list(ablation.fixed_ids)}


def _values(value) -> list:
    """A manifest input hash, written as one value or as a list, always a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _report_hashes(workspace: pathlib.Path, score_runs: list[str]) -> set[str]:
    shas = set()
    for run_id in score_runs:
        record = (_manifest(workspace, run_id)["outputs"].get("save_report") or [{}])[0]
        if record.get("sha256"):
            shas.add(record["sha256"])
    return shas


def reserved_reading_run(workspace: pathlib.Path, index: dict) -> dict | None:
    """The `ablate` that read the two twins answered over holdout.csv against
    each other: the end of the loop. Matched by the report hashes the run was
    given, so a reading is the one over exactly those two arms; the command
    carries no `--ids`, because each twin holds reserved rows only."""
    baseline, candidate = reserved_pair(index)
    if not (baseline and candidate):
        return None
    without = _report_hashes(workspace, baseline[1]["score_runs"])
    with_context = _report_hashes(workspace, candidate[1]["score_runs"])
    if not (without and with_context):
        return None
    ablates = [r for r in index["runs"] if r["command"] == "ablate"
               and r["finished"] and r["exit_code"] == 0]
    for run in sorted(ablates, key=lambda r: r["started_at"] or "", reverse=True):
        given_without = set(_values(run["inputs"].get("without_report")))
        given_with = set(_values(run["inputs"].get("with_report")))
        if (given_without and given_with
                and given_without <= without and given_with <= with_context):
            return run
    return None


def holdout_readings(workspace: pathlib.Path, index: dict) -> list[dict]:
    """Every reading of the reserved questions, oldest first: the plain
    `ablate` over the two twins answered over holdout.csv, and any `ablate
    --ids` / `climb --ids` run over the split's reserved id list (reports that
    hold both halves). One run is one look, both arms included. An ablate run
    carries its report's numbers as text; a climb run the rate of each round."""
    readings = []
    looks = [r for r in index["runs"] if r["command"] in ("ablate", "climb")
             and r["on_holdout"] and r["finished"] and r["exit_code"] == 0]
    twin = reserved_reading_run(workspace, index)
    if twin and twin["run_id"] not in {r["run_id"] for r in looks}:
        looks.append(twin)
    for run in sorted(looks, key=lambda r: r["started_at"] or ""):
        manifest = _manifest(workspace, run["run_id"])
        entry = {"run_id": run["run_id"], "command": run["command"],
                 "started_at": run["started_at"], "argv": manifest.get("argv", [])}
        record = (manifest.get("outputs", {}).get("save_report") or [{}])[0]
        data = None
        if record.get("copy"):
            try:
                data = json.loads((workspace / "runs" / run["run_id"] / record["copy"]).read_text())
            except (OSError, ValueError):
                data = None
        if data and run["command"] == "ablate":
            entry.update(_ablate_reading(_ablation_from_dict(data)))
        elif data and run["command"] == "climb":
            entry["rounds"] = [{"label": r["label"], "rate_text": f"{r['rate']['point']:.1%}"}
                               for r in data.get("rounds", [])]
        readings.append(entry)
    return readings


# --- the slide sections: computed from what the workspace already holds ----

def _heading(text: str | None, fallback: str) -> str:
    """The "# heading" line of a rules file, else the fallback; the same
    rule the rounds ledger uses for a version's name."""
    for line in (text or "").splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def _failure_shapes(base_pooled: EvalReport, cand_pooled: EvalReport) -> dict:
    """Failures by shape in both arms, side by side. Counted per question,
    not per repetition: a question that failed in any run counts once, in
    the bucket of its first failing run, the way the per-question table
    names a bucket. Largest bucket first."""
    def per_question(pooled: EvalReport) -> Counter:
        bucket_of: dict[str, str] = {}
        for item in triage(pooled).classified:
            bucket_of.setdefault(item.id.split("#rep")[0], item.bucket)
        return Counter(bucket_of.values())
    without, with_ = per_question(base_pooled), per_question(cand_pooled)
    names = sorted(set(without) | set(with_), key=lambda b: (-(without[b] + with_[b]), b))
    return {"buckets": [{"bucket": b, "meaning": BUCKET_MEANINGS.get(b, ""),
                         "without": without.get(b, 0), "with": with_.get(b, 0)} for b in names],
            "n_failed": {"without": sum(without.values()), "with": sum(with_.values())}}


def _rounds_table(workspace: pathlib.Path, index: dict, base_pooled: EvalReport) -> list[dict]:
    """Every fully scored context arm against the pinned baseline, in the
    order the versions first appeared: the ledger a slide shows. The same
    arithmetic as the verdict card (ablate over the pooled reports); an arm
    that cannot be paired says why instead of showing a number. Not
    loop.api_rounds, which calls collect and would call this again."""
    # a twin answered over the reserved questions is not a round: it belongs
    # to the reading at the end, and pairing it with the baseline would
    # compare two different question sets
    versions = sorted((a for a in index["arms"]
                       if has_context(a) and fully_scored(a) and not only_holdout(a)),
                      key=lambda a: a["first_seen"])
    rows = []
    for n, arm in enumerate(versions, start=1):
        retrieved = arm.get("retrieved_sha256")
        row = {"n": n, "arm_id": arm["arm_id"], "label": arm["label"], "runs": None,
               "rule_lines": 0, "context": "no context file", "error": None}
        try:
            material = _arm_material(workspace, index, arm)
            row["label"] = _heading(material["context_text"],
                                    f"retrieved {retrieved[:12]}" if retrieved else f"rules v{n}")
            row["runs"] = len(material["reports"])
            row.update(_context_used(material, arm))
            reading = _ablate_reading(ablate(base_pooled, pool(material["reports"])))
        except (SystemExit, ValueError) as refusal:
            row["error"] = str(refusal)
            rows.append(row)
            continue
        for key in ("sentences", "broke_ids", "fixed_ids"):
            reading.pop(key)
        row.update(reading)
        rows.append(row)
    return rows


def _count_rule_lines(context_text: str | None) -> int:
    return sum(1 for line in (context_text or "").splitlines() if line.startswith("- "))


def _context_used(material: dict, arm: dict) -> dict:
    """What one round gave the model, in words and as a rule-line count.

    The rounds table showed a rules round with 8 lines beside a retrieval
    round, and the page said "the rules file (0 lines)" for both, because the
    only context it described was the candidate's.
    """
    lines = _count_rule_lines(material["context_text"])
    if lines:
        used = f"{lines} rule lines"
    elif material["retrieval"]:
        retrieval = material["retrieval"]
        used = (f"retrieved text for {retrieval['served']} of "
                f"{retrieval['n_questions']} questions")
    elif material["context_text"]:
        used = "a context file with no rule lines"
    elif arm.get("examples_sha256"):
        used = "worked examples only"
    else:
        used = "no context file"
    return {"rule_lines": lines, "context": used}


def _routing_accuracy(workspace: pathlib.Path, index: dict) -> list[dict]:
    """Table accuracy per routing arm, from the score-routing runs that saved
    a report. A run is matched to the arm whose generate run wrote the
    routing sidecar it scored (by hash, the index's lineage); a run over a
    file no arm produced is grouped by that file's name. Repetitions pool by
    adding their counts. Empty when no routing report exists."""
    arms = {a["arm_id"]: a for a in index["arms"]}
    groups: dict[str, dict] = {}
    runs = [r for r in index["runs"] if r["command"] == "score-routing"
            and r["finished"] and r["exit_code"] == 0]
    for run in sorted(runs, key=lambda r: r["started_at"] or ""):
        manifest = _manifest(workspace, run["run_id"])
        record = (manifest.get("outputs", {}).get("save_report") or [{}])[0]
        if not record.get("copy"):
            continue
        try:
            report = load_report(workspace / "runs" / run["run_id"] / record["copy"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        arm = arms.get(run["arm_id"]) if run.get("arm_id") else None
        source = (manifest.get("inputs", {}).get("predictions") or {}).get("path") or run["run_id"]
        key = run["arm_id"] or pathlib.Path(source).name
        group = groups.setdefault(key, {
            "arm_id": run.get("arm_id"), "label": arm["label"] if arm else pathlib.Path(source).name,
            "role": ("baseline" if run.get("arm_id") == index["pins"].get("baseline") else
                     "candidate" if run.get("arm_id") == index["pins"].get("candidate") else None),
            "runs": [], "k": 0, "n": 0})
        group["runs"].append(run["run_id"])
        group["k"] += report.execution.k
        group["n"] += report.execution.n
    for group in groups.values():
        rate = proportion(group["k"], group["n"])
        group.update(rate_text=f"{rate.point:.1%}", low_text=f"{rate.low:.1%}",
                     high_text=f"{rate.high:.1%}")
    return list(groups.values())


def _question_funnel(workspace: pathlib.Path, index: dict, gate: dict | None) -> dict:
    """Where the answer key came from, as counts: mine's candidates and how
    many were gate-clean, the review's kept and rejected-by-reason (read
    from the sheet the finishing run took in), the gate's flagged count,
    the split's sizes. None for a stage that did not run here."""
    def newest(command: str, predicate=lambda manifest: True):
        runs = [r for r in index["runs"] if r["command"] == command
                and r["finished"] and r["exit_code"] == 0]
        for run in sorted(runs, key=lambda r: r["started_at"] or "", reverse=True):
            manifest = _manifest(workspace, run["run_id"])
            if predicate(manifest):
                return run, manifest
        return None, None

    def rows_of(run_id: str, copy: str) -> list[dict]:
        with open(workspace / "runs" / run_id / copy, newline="") as handle:
            return list(csv.DictReader(handle))

    mined = None
    run, manifest = newest("mine", lambda m: bool((m.get("outputs", {}).get("out") or [{}])[0].get("copy")))
    if run:
        rows = rows_of(run["run_id"], manifest["outputs"]["out"][0]["copy"])
        mined = {"run_id": run["run_id"], "candidates": len(rows),
                 "clean": sum(1 for r in rows if (r.get("gate_status") or "ok") == "ok"),
                 "source": input_paths(manifest, "queries")}
    reviewed = None
    run, manifest = newest("review", lambda m: bool((m.get("inputs", {}).get("sheet") or {}).get("copy")))
    if run:
        rows = rows_of(run["run_id"], manifest["inputs"]["sheet"]["copy"])
        decisions = [(r.get("keep") or "").strip().lower() for r in rows]
        rejected = Counter((r.get("reason") or "(no reason given)").strip()
                           for r, keep in zip(rows, decisions) if keep in NO)
        reviewed = {"run_id": run["run_id"], "total": len(rows),
                    "kept": sum(1 for keep in decisions if keep in YES),
                    "rejected": dict(rejected.most_common())}
    split = None
    run, manifest = newest("split")
    if run:
        def count(flag: str) -> int:
            record = (manifest.get("outputs", {}).get(flag) or [{}])[0]
            if not record.get("copy"):
                return 0
            return len((workspace / "runs" / run["run_id"] / record["copy"]).read_text().split())
        split = {"run_id": run["run_id"], "curate": count("out_curate"), "holdout": count("out_holdout")}
    return {"mined": mined, "reviewed": reviewed,
            "gate": {"run_id": gate["run_id"], "n": gate["n"], "flagged": gate["flagged"]} if gate else None,
            "split": split}


def _spend(workspace: pathlib.Path, index: dict) -> dict:
    """Total spend over the recorded runs: model calls (telemetry
    `estimated_cost_usd`: generate, mine --write-questions) and warehouse
    scans (telemetry `usd`: every BigQuery command). The Usage screen shows
    the same figures per run."""
    model = warehouse = 0.0
    counted = 0
    for run in index["runs"]:
        telemetry = _manifest(workspace, run["run_id"]).get("telemetry") or {}
        model_usd = telemetry.get("estimated_cost_usd") or 0.0
        warehouse_usd = telemetry.get("usd") or 0.0
        if model_usd or warehouse_usd:
            counted += 1
        model += model_usd
        warehouse += warehouse_usd
    return {"model_usd": round(model, 4), "warehouse_usd": round(warehouse, 4),
            "total_usd": round(model + warehouse, 4), "runs_with_spend": counted,
            "text": f"${model + warehouse:.4f}"}


def _slide_keys(workspace: pathlib.Path, index: dict, ablation: AblationReport,
                base_pooled: EvalReport, cand_pooled: EvalReport, gate: dict | None) -> dict:
    """The keys a slide is built from, beside the verdict card's."""
    low, high = ablation.lift_interval()
    return {
        "verdict_sentences": ablation.verdict_sentences(),
        "lift_interval_text": f"[{low:+.1%}, {high:+.1%}]".replace("%", " pts"),
        "holdout_readings": holdout_readings(workspace, index),
        "failure_shapes": _failure_shapes(base_pooled, cand_pooled),
        "rounds": _rounds_table(workspace, index, base_pooled),
        "routing": _routing_accuracy(workspace, index),
        "funnel": _question_funnel(workspace, index, gate),
        "spend": _spend(workspace, index),
    }


def collect(workspace: str | pathlib.Path, baseline: str | None = None,
            candidate: str | None = None, include_rows: bool = True) -> dict:
    """Everything verdict.html shows, as one JSON-serializable dict.

    With include_rows off, each `scorer_saw` keeps the verdict and the
    shape note (row and column counts, no values) and its `rows` is None.
    """
    workspace = pathlib.Path(workspace)
    index = rebuild_index(workspace)
    pins = index["pins"]
    baseline = baseline or pins.get("baseline")
    candidate = candidate or pins.get("candidate")
    if not baseline or not candidate:
        raise SystemExit("no baseline/candidate: the index picks the version without rules "
                         "and the newest fully scored version with rules by itself, so score "
                         "both first; `contextlift pin baseline ARM` / `pin candidate ARM` "
                         "overrides that choice")
    arms = {a["arm_id"]: a for a in index["arms"]}
    for name, arm_id in (("baseline", baseline), ("candidate", candidate)):
        if arm_id not in arms:
            raise SystemExit(f"{name} arm {arm_id} is not in index.json")
    base = _arm_material(workspace, index, arms[baseline])
    cand = _arm_material(workspace, index, arms[candidate])
    if len(base["reports"]) != len(cand["reports"]):
        raise SystemExit(
            f"cannot compare: baseline has {len(base['reports'])} scored "
            f"predictions file(s), candidate {len(cand['reports'])}; the paired "
            "test lines runs up one to one, so score the same number per arm")
    identity = _identity_check(base, cand)

    try:
        ablation = ablate(pool(base["reports"]), pool(cand["reports"]))
    except ValueError as exc:
        raise SystemExit(f"cannot compare: {exc}") from exc
    word, p_defended = verdict_word(ablation)

    cand_pooled = pool(cand["reports"])
    buckets: dict[str, tuple[str, str]] = {}
    for item in triage(cand_pooled).classified:
        buckets.setdefault(item.id.split("#rep")[0], (item.bucket, item.evidence))

    def sql_by_observation(material: dict) -> dict[str, str]:
        return {f"{qid}#rep{i}": pred
                for i, preds in enumerate(material["predictions"])
                for qid, (_q, _g, pred) in preds.items()}

    attribution = None
    suspects: dict[str, list[str]] = {}
    credited: dict[str, list[tuple[str, int]]] = {}
    rules = parse_rules(cand["context_text"]) if cand["context_text"] else []
    if rules:
        attribution = attribute(ablation, sql_by_observation(base),
                                sql_by_observation(cand), rules)
        for impact in attribution.impacts:
            for qid, _n in impact.broke:
                suspects.setdefault(qid, []).append(impact.rule.id)
            for qid, n in impact.fixed:
                credited.setdefault(qid, []).append((impact.rule.id, n))

    previews = {}
    for report in cand["reports"]:
        for result in report.results:
            if result.rows_preview and result.id not in previews:
                previews[result.id] = {"verdict": result.verdict,
                                       "detail": result.detail,
                                       "rows": result.rows_preview if include_rows else None}

    questions = []
    for entry in ablation.per_question:
        qid = entry["id"]
        text, gold = "", ""
        for preds in base["predictions"] + cand["predictions"]:
            if qid in preds:
                text, gold, _ = preds[qid]
                break
        bucket, evidence = buckets.get(qid, (None, ""))
        questions.append({
            **entry,
            "question": text,
            "gold_sql": gold,
            "base_sql": [p.get(qid, ("", "", ""))[2] for p in base["predictions"]],
            "ctx_sql": [p.get(qid, ("", "", ""))[2] for p in cand["predictions"]],
            "bucket": bucket,
            "bucket_meaning": BUCKET_MEANINGS.get(bucket, "") if bucket else "",
            "evidence": evidence,
            "suspect_rules": suspects.get(qid, []),
            "credited_rules": [{"rule": r, "runs": n} for r, n in credited.get(qid, [])],
            "scorer_saw": previews.get(qid),
        })
    order = {"broke": 0, "unstable": 1, "fixed": 2, "wrong": 3, "right": 4}
    questions.sort(key=lambda q: (order[q["status"]], q["id"]))

    gate_run, gate_manifest = _newest(
        index, "gate", lambda run, m: bool(m.get("outputs", {}).get("save_report")),
        workspace)
    gate_summary = None
    if gate_run:
        copy = gate_manifest["outputs"]["save_report"][0]["copy"]
        findings = json.loads((workspace / "runs" / gate_run["run_id"] / copy).read_text())
        tally: dict[str, int] = {}
        for finding in findings:
            for defect in finding["defects"]:
                tally[defect] = tally.get(defect, 0) + 1
        gate_summary = {"run_id": gate_run["run_id"], "n": len(findings),
                        "flagged": sum(1 for f in findings if f["defects"]),
                        "defects": tally,
                        "flagged_ids": {f["gold_id"]: {"defects": f["defects"], "detail": f["detail"]}
                                        for f in findings if f["defects"]}}
    flagged_ids = (gate_summary or {}).get("flagged_ids", {})
    for entry in questions:
        finding = flagged_ids.get(entry["id"])
        entry["key_flagged"] = bool(finding)
        entry["key_detail"] = finding["detail"] if finding else ""

    # the leak run that read what this arm saw, by input hash: the rules file,
    # or the per-question file when a retriever chose the context
    if cand["retrieval"]:
        leak_flag, leak_sha = "context_per_question", cand["retrieval"]["input_sha256"]
    else:
        leak_flag, leak_sha = "context", arms[candidate]["context_sha256"]
    leak_run, _ = _newest(
        index, "leak",
        lambda run, m: (m.get("inputs", {}).get(leak_flag) or {}).get("sha256") == leak_sha,
        workspace) if leak_sha else (None, None)
    leak = {"status": "not checked", "run_id": None}
    if leak_run:
        leak = {"status": "clean" if leak_run["exit_code"] == 0 else "LEAKED",
                "run_id": leak_run["run_id"]}

    n_holdout = 0
    split_run, split_manifest = _newest(index, "split", workspace=workspace)
    if split_run:
        record = split_manifest.get("outputs", {}).get("out_holdout")
        if record and record[0].get("copy"):
            n_holdout = len((workspace / "runs" / split_run["run_id"]
                             / record[0]["copy"]).read_text().split())
    # who accepted the golds: the newest finished review, and the arms' models
    # so the sentence can say when reviewer and agent are one family. Under
    # --backend command the model option names nothing (see `who` in render).
    review_run = answer_key_review(index["runs"])
    answer_key = None
    if review_run:
        agent_models = [arm["model"] for arm in (arms[baseline], arms[candidate])
                        if arm.get("backend") != "command" and arm.get("model")]
        answer_key = {"run_id": review_run["run_id"], **review_run["summary"],
                      "caveat": answer_key_caveat(review_run["summary"], agent_models)}
    rule_lines = []
    id_by_line = {r.line_no: r.id for r in rules}
    for number, line in enumerate((cand["context_text"] or "").splitlines(), start=1):
        if line.startswith("- "):
            rule_lines.append({"line_no": number, "text": line,
                               "parsed": number in id_by_line,
                               "id": id_by_line.get(number)})

    return {
        "contextlift_version": __version__,
        "workspace": index["workspace"],
        "generated_at": index["generated_at"],
        "identity": identity,
        "n_holdout": n_holdout,
        "answer_key": answer_key,
        "rule_lines": rule_lines,
        "retrieval": cand["retrieval"],
        "p_text": {"defended": fmt_p(p_defended), "pooled": fmt_p(ablation.mcnemar.p_value),
                   "clustered": fmt_p(ablation.clustered.p_value) if ablation.clustered else None,
                   # derived from the printed p, so "63 in 100" can never sit
                   # next to "p = 0.62" (seen in the terminal vs verdict.html)
                   "luck_pct": round(float(fmt_p(p_defended)) * 100),
                   # the integer alone said "0 in 100" for every p below 0.005,
                   # which is an impossibility printed on the strongest results
                   "luck_text": _luck_text(float(fmt_p(p_defended)))},
        "baseline": {**arms[baseline], "score_runs": base["score_runs"],
                     "on_holdout": base["on_holdout"]},
        "candidate": {**arms[candidate], "score_runs": cand["score_runs"],
                      "on_holdout": cand["on_holdout"]},
        "verdict": {"word": word, "p_defended": p_defended,
                    "ungated": UNGATED_WORD},
        # every rate also as the text to print. verdict.html carries no
        # arithmetic of its own, so it must not multiply and round one:
        # Python rounds half-to-even and JavaScript half-up, and 13/16
        # printed 81.3% in the browser against 81.2% from the CLI
        "ablation": {**dataclasses.asdict(ablation),
                     "text": {
                         "without_context": f"{ablation.without_context.point:.1%}",
                         "with_context": f"{ablation.with_context.point:.1%}",
                         "without_low": f"{ablation.without_context.low:.1%}",
                         "without_high": f"{ablation.without_context.high:.1%}",
                         "with_low": f"{ablation.with_context.low:.1%}",
                         "with_high": f"{ablation.with_context.high:.1%}",
                         "lift": f"{ablation.lift:+.1%}".replace("%", " pts"),
                     }},
        "questions": questions,
        "rules": [dataclasses.asdict(r) for r in rules],
        "attribution": dataclasses.asdict(attribution) if attribution else None,
        "gate": gate_summary,
        "leak": leak,
        "holdout_looks": index["holdout_looks"],
        "warnings": index["warnings"],
        **_slide_keys(workspace, index, ablation, pool(base["reports"]), cand_pooled, gate_summary),
    }


# --- HTML -----------------------------------------------------------------
# Vocabulary for readers, chosen after two first-time users read the old
# file: "working questions" (curate), "reserved questions, sealed"
# (holdout), "answer-key check" (gate), "without context / with <file>"
# (arms). The technical word stays in the statistician fold.

_PAGE = string.Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>$headline</title>
<style>
  body{max-width:960px;margin:32px auto;padding:0 20px;background:#FAFAF8;color:#1A1A18;
       font:14.5px/1.55 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
  h1{font-size:24px;margin:0 0 10px;letter-spacing:-.01em} h2{font-size:18px;margin:34px 0 10px}
  .eyebrow{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#5F5F5A;margin-bottom:6px}
  .lead{font-size:15.5px;max-width:720px}
  .mono,td.mono,code,pre{font-family:ui-monospace,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums;font-size:12.5px}
  .dim{color:#5F5F5A} .green{color:#147A45} .red{color:#B93327} .amber{color:#9A6A12}
  .verdict{border:1px solid #E6E6E2;border-left:4px solid #9A6A12;border-radius:6px;padding:16px 20px;margin:16px 0;background:#fff}
  .verdict.v-green{border-left-color:#147A45} .verdict.v-red{border-left-color:#B93327} .verdict.v-grey{border-left-color:#B5B5B0}
  .word{font-size:30px;font-weight:700;margin-bottom:6px}
  .figs{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0 6px}
  .fig{flex:1;min-width:200px;border:1px solid #E6E6E2;border-radius:6px;padding:10px 12px;background:#fff}
  .fig .n{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
  .caveats{border:1px solid #E6E6E2;border-left:4px solid #9A6A12;border-radius:6px;padding:12px 18px;margin:12px 0;background:#fff}
  .caveats li{margin:4px 0}
  table{border-collapse:collapse;width:100%;font-size:13px;background:#fff;border:1px solid #E6E6E2;border-radius:6px}
  th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #E6E6E2;vertical-align:top} th{font-weight:600;color:#5F5F5A;font-size:12px}
  details{margin:6px 0 8px} summary{cursor:pointer;color:#2B63C7} pre{white-space:pre-wrap;margin:6px 0;
     background:#F3F3F0;padding:8px;border:1px solid #E6E6E2;border-radius:6px}
  .pill{display:inline-block;border-radius:999px;padding:1px 8px;font-size:11.5px;font-weight:600}
  .pill.broke{background:#FBEDEB;color:#B93327} .pill.fixed{background:#EAF6EF;color:#147A45}
  .pill.unstable{background:#FBF3E4;color:#9A6A12} .pill.wrong,.pill.right{background:#F3F3F0;color:#5F5F5A}
  .pill.key{background:#FBF3E4;color:#9A6A12;font-weight:500}
  footer{margin-top:40px;font-size:12.5px;color:#5F5F5A;max-width:760px}
</style></head><body>
<div class="eyebrow">$eyebrow</div>
<h1>$headline</h1>
<p class="lead">$lead</p>

<div class="verdict v-$word_class">
  <div class="word $word_class">$word</div>
  <div>$luck_line</div>
  $verdict_sentences
  <div class="figs">
    <div class="fig"><div class="dim">without context</div><div class="n">$base_rate%</div><div class="dim">plausible range $base_low&ndash;$base_high%</div></div>
    <div class="fig"><div class="dim">with $cand_label</div><div class="n">$ctx_rate%</div><div class="dim">plausible range $ctx_low&ndash;$ctx_high%</div></div>
    <div class="fig"><div class="dim">difference</div><div class="n">$lift</div><div class="dim">$range_note</div></div>
  </div>
  <details><summary>For the statistician: the test behind the word</summary>
    <div class="dim">$stats_line</div></details>
</div>

<div class="verdict v-$safety_class">
  <div class="word $safety_class">$safety_word</div>
  <div>$safety_line</div>
  <div class="dim">This is a count, not an estimate. The word above is a claim
  about questions the context was not written from, so it needs a test. This
  one says what happened to the questions in front of it, so there is nothing
  to estimate and no interval to print. A context can be unproven and safe at
  the same time.</div>
</div>

<h2>Before you repeat this number</h2>
<div class="caveats"><ul>$caveats</ul></div>

<h2>What changed, question by question</h2>
<div class="dim" style="margin-bottom:8px">$n_questions working questions, each asked $n_reps times in both setups. Regressions first. One mark per run: &#10003; right, &#10007; wrong.</div>
<table>
<tr><th>outcome</th><th>id</th><th>question</th><th>without</th><th>with</th><th>why</th><th>which rule did this?</th></tr>
$rows
</table>

$slide_sections

<h2>$rules_heading</h2>
$rules

<footer>
  <div><b>How to read this.</b> "Real gain" and "real loss" mean the difference would happen by luck less than 5 times
    in 100. "Not proven" means the set is too small to say; more questions would decide it, not more runs.
    Fixed and broke count questions whose answer changed the same way in most runs; unstable means it flipped
    between runs.</div>
  <div style="margin-top:8px"><b>For reproducing this.</b> $who &middot; built $generated_at with contextlift $version
    &middot; baseline runs $base_runs &middot; candidate runs $cand_runs &middot; setup ids $base_arm / $cand_arm
    &middot; database fingerprint $db_sha</div>
</footer>
</body></html>
""")


def _e(text) -> str:
    return html.escape("" if text is None else str(text))


def _glyphs(flags: list[bool]) -> str:
    return "".join("&#10003;" if f else "&#10007;" for f in flags)


def _sql_block(label: str, sqls: list[str]) -> str:
    parts = []
    for i, sql in enumerate(sqls):
        parts.append(f"<div class='dim'>{_e(label)}, run {i + 1}</div><pre>{_e(sql)}</pre>")
    return "".join(parts)


def _preview_table(preview: dict | None) -> str:
    if not preview:
        return ""
    if preview["rows"] is None:
        return (f"<div class='dim'>what the scorer saw: {_e(preview['verdict'])} &middot; "
                f"{_e(preview['detail'])}</div>"
                "<div class='dim'>row previews left out (bundle --no-rows)</div>")
    def table(rows: list[dict], total: int) -> str:
        if not rows:
            return "<div class='dim'>no rows</div>"
        cols = list(rows[0])
        head = "".join(f"<th>{_e(c)}</th>" for c in cols)
        body = "".join("<tr>" + "".join(f"<td class='mono'>{_e(r.get(c))}</td>"
                                        for c in cols) + "</tr>" for r in rows)
        more = f"<div class='dim'>{total} rows in total</div>" if total > len(rows) else ""
        return f"<table><tr>{head}</tr>{body}</table>{more}"
    return (f"<div class='dim'>what the scorer saw: {_e(preview['verdict'])} &middot; "
            f"{_e(preview['detail'])}</div>"
            f"<div style='display:flex;gap:16px'><div><b>correct rows</b>"
            f"{table(preview['rows']['gold'], preview['rows']['gold_total'])}</div>"
            f"<div><b>the model's rows</b>"
            f"{table(preview['rows']['pred'], preview['rows']['pred_total'])}</div></div>")


OUTCOME = {"broke": "Broke", "fixed": "Fixed", "unstable": "Unstable",
           "wrong": "Wrong both ways", "right": "Right both ways"}


def _row(q: dict) -> str:
    key = (" <span class='pill key'>answer key flagged</span>" if q.get("key_flagged") else "")
    why = _e(q["bucket_meaning"]) if q["bucket"] else ""
    if q.get("key_flagged"):
        why = (f"<span class='amber'>the answer key itself is wrong: {_e(q['key_detail'])}. "
               f"Both setups are graded against it, so this row says nothing.</span>")
    n_runs = len(q["ctx"]) or 1
    if q["suspect_rules"]:
        rule = "blamed: " + ", ".join(_e(s) for s in q["suspect_rules"])
    elif q.get("credited_rules"):
        rule = "credited: " + ", ".join(
            f"{_e(c['rule'])} in {c['runs']} of {n_runs} runs" for c in q["credited_rules"])
    elif q["status"] in ("broke", "fixed"):
        rule = "<span class='dim'>no rule is blamed or credited</span>"
    else:
        rule = ""
    detail = (
        f"<details><summary>the correct SQL and every attempt</summary>"
        f"<div class='dim'>correct SQL (the answer key)</div><pre>{_e(q['gold_sql'])}</pre>"
        f"{_sql_block('without context', q['base_sql'])}{_sql_block('with context', q['ctx_sql'])}"
        f"{_preview_table(q['scorer_saw'])}"
        + ("<div class='dim'>a blamed rule is a lead, not a verdict; confirm by running "
           "the same context minus that line</div>" if q["suspect_rules"] else "")
        + "</details>"
    )
    return (f"<tr><td><span class='pill {q['status']}'>{OUTCOME[q['status']]}</span></td>"
            f"<td class='mono'>{_e(q['id'])}</td><td>{_e(q['question'])}{key}{detail}</td>"
            f"<td class='mono'>{_glyphs(q['base'])}</td><td class='mono'>{_glyphs(q['ctx'])}</td>"
            f"<td>{why}</td><td class='mono'>{rule}</td></tr>")


def _retrieval_html(retrieval: dict) -> str:
    """The rules section of an arm whose context a retriever chose: there is
    no file of lines to credit or blame, so say what was served instead."""
    return (f"<div class='dim'>No rules file: a retriever chose the context per question, "
            f"from <code>{_e(retrieval['path'])}</code>. "
            f"<b>{retrieval['served']} of {retrieval['n_questions']}</b> questions were served "
            f"retrieved text; <b>{retrieval['nothing']}</b> retrieved nothing and were answered "
            "from the schema alone. No line can be credited or blamed; the unit of change is "
            "the retriever and its documents.</div>")


def _rules_html(verdict: dict) -> str:
    lines = verdict.get("rule_lines") or []
    if not lines and verdict.get("retrieval"):
        return _retrieval_html(verdict["retrieval"])
    if not lines:
        return ("<div class='dim'>the context has no rule lines "
                "(a rule line starts with: - \"phrase\" -&gt; TABLE.COLUMN)</div>")
    attribution = verdict["attribution"] or {}
    impacts = {i["rule"]["id"]: i for i in attribution.get("impacts", [])}
    rows = []
    for line in lines:
        impact = impacts.get(line["id"], {"broke": [], "fixed": []})
        broke = ", ".join(f"{_e(q)} in {n} run{'s' if n != 1 else ''}" for q, n in impact["broke"])
        fixed = ", ".join(f"{_e(q)} in {n} run{'s' if n != 1 else ''}" for q, n in impact["fixed"])
        note = ""
        if broke:
            note = f"<span class='red'>broke {broke}</span>"
        if fixed:
            note += (" &middot; " if note else "") + f"<span class='green'>fixed {fixed}</span>"
        if not line["parsed"]:
            note = "<span class='dim'>not in the rule format, so it cannot be credited or blamed</span>"
        rows.append(f"<tr><td class='mono'>{_e(line['id'] or '')}</td>"
                    f"<td class='mono'>{_e(line['text'])}</td><td>{note or '&mdash;'}</td></tr>")
    unattributed = ""
    if attribution.get("unattributed_broke") or attribution.get("unattributed_fixed"):
        broke = ", ".join(f"{_e(q)} in {n}" for q, n in attribution.get("unattributed_broke", []))
        fixed = ", ".join(f"{_e(q)} in {n}" for q, n in attribution.get("unattributed_fixed", []))
        unattributed = ("<div class='dim' style='margin-top:6px'>changes no line explains "
                        "(the SQL changed without using any rule's table or column): "
                        + (f"broke {broke}" if broke else "")
                        + (" &middot; " if broke and fixed else "")
                        + (f"fixed {fixed}" if fixed else "") + "</div>")
    return ("<div class='dim' style='margin-bottom:8px'>Each line maps a business phrase to a table and column. "
            "A line is credited when the model's SQL for a changed question uses that column.</div>"
            "<table><tr><th>line</th><th>rule</th><th>what it changed (question, runs)</th></tr>"
            + "".join(rows) + "</table>" + unattributed)


def _holdout_html(verdict: dict) -> str:
    """The reserved questions: sealed, read once (the number to report), or
    read more than once (a curate set now, in red)."""
    looks = verdict["holdout_looks"]
    n = verdict["n_holdout"]
    if looks == 0:
        return (f"<div class='dim'>Sealed: the <b>{n} reserved questions</b> have not been read. "
                "The reading is both versions answered over <code>holdout.csv</code> once, "
                "scored, and compared with one <code>ablate</code>, at the end.</div>")
    if looks == 1:
        label = ("<span class='green'><b>the reserved questions, read once: this is the number "
                 "to report</b></span>")
    else:
        times = "twice" if looks == 2 else f"{looks} times"
        label = f"<span class='red'><b>read {times}: a curate set now</b></span>"
    parts = [f"<div>{label}</div>"]
    readings = verdict.get("holdout_readings") or []
    if not readings:
        parts.append("<div class='dim'>Read by scoring an arm generated over the reserved rows; "
                     "the number is that arm's score report.</div>")
    pill = {"REAL GAIN": "fixed", "REAL LOSS": "broke", "NOT PROVEN": "unstable", "NO CHANGE": "right"}
    for reading in readings:
        if reading.get("word"):
            parts.append(
                f"<div style='margin-top:8px'><span class='pill {pill.get(reading['word'], 'right')}'>"
                f"{_e(reading['word'])}</span> without {_e(reading['without_text'])} &rarr; with "
                f"{_e(reading['with_text'])}, difference <b>{_e(reading['lift_text'])}</b> "
                f"{_e(reading['lift_interval_text'])}; fixed {reading['fixed']}, broke "
                f"{reading['broke']} of {reading['n_questions']} reserved questions, each asked "
                f"{reading['n_reps']} time(s); p = {_e(reading['p_text'])} "
                f"<span class='dim'>({_e(reading['run_id'])})</span></div>")
            for sentence in reading.get("sentences") or []:
                parts.append(f"<div class='dim'>{_e(sentence)}</div>")
        elif reading.get("rounds"):
            rounds = ", ".join(f"{_e(r['label'])} {_e(r['rate_text'])}" for r in reading["rounds"])
            parts.append(f"<div style='margin-top:8px'>climb over the reserved ids: {rounds} "
                         f"<span class='dim'>({_e(reading['run_id'])})</span></div>")
        else:
            parts.append(f"<div class='dim'>{_e(reading['command'])} run {_e(reading['run_id'])} "
                         "read the reserved ids (no report saved)</div>")
    return "".join(parts)


def _shapes_html(shapes: dict) -> str:
    if not shapes["buckets"]:
        return "<div class='dim'>no failures in either setup</div>"
    rows = "".join(
        f"<tr><td class='mono'>{_e(b['bucket'])}</td><td>{_e(b['meaning'])}</td>"
        f"<td class='mono'>{b['without']}</td><td class='mono'>{b['with']}</td></tr>"
        for b in shapes["buckets"])
    failed = shapes["n_failed"]
    return (f"<div class='dim' style='margin-bottom:8px'>Questions failing in at least one run: "
            f"{failed['without']} without context, {failed['with']} with. A question counts once, "
            "in the bucket of its first failing run.</div>"
            "<table><tr><th>shape</th><th>meaning</th><th>without</th><th>with</th></tr>"
            + rows + "</table>")


def _rounds_html(rounds: list[dict]) -> str:
    if not rounds:
        return "<div class='dim'>no scored version with context yet</div>"
    rows = []
    for r in rounds:
        if r["error"]:
            rows.append(f"<tr><td class='mono'>{r['n']}</td><td>{_e(r['label'])}</td>"
                        f"<td colspan='6' class='dim'>not comparable: {_e(r['error'])}</td></tr>")
            continue
        word_class = {"REAL GAIN": "green", "REAL LOSS": "red", "NOT PROVEN": "amber"}.get(r["word"], "dim")
        rows.append(
            f"<tr><td class='mono'>{r['n']}</td><td>{_e(r['label'])}</td>"
            f"<td class='dim'>{_e(r.get('context') or '')}</td><td class='mono'>{r['runs']}</td>"
            f"<td class='mono'>{_e(r['with_text'])}</td>"
            f"<td class='mono'>{_e(r['lift_text'])} {_e(r['lift_interval_text'])}</td>"
            f"<td class='mono'><span class='green'>fixed {r['fixed']}</span> / "
            f"<span class='red'>broke {r['broke']}</span></td>"
            f"<td class='mono'>{_e(r['p_text'])} <span class='{word_class}'>{_e(r['word'])}</span></td></tr>")
    return ("<div class='dim' style='margin-bottom:8px'>Every version with context against the same "
            "baseline, in the order the versions appeared. \"What it used\" is that round's own "
            "context, not the last one's. Fixed and broke count questions, by majority over the "
            "runs.</div>"
            "<table><tr><th>round</th><th>version</th><th>what it used</th><th>runs</th>"
            "<th>rate</th><th>lift [95%]</th>"
            "<th>questions</th><th>p, verdict</th></tr>" + "".join(rows) + "</table>")


def _routing_html(routing: list[dict]) -> str:
    rows = "".join(
        f"<tr><td>{_e(g['label'])}{' <span class=dim>(' + g['role'] + ')</span>' if g.get('role') else ''}</td>"
        f"<td class='mono'>{len(g['runs'])}</td><td class='mono'>{_e(g['rate_text'])}</td>"
        f"<td class='mono'>{g['k']} of {g['n']}</td>"
        f"<td class='dim'>{_e(g['low_text'])}&ndash;{_e(g['high_text'])}</td></tr>"
        for g in routing)
    return ("<div class='dim' style='margin-bottom:8px'>Did the right table get picked, before any "
            "SQL was written: table accuracy from the score-routing reports, one row per version, "
            "repetitions added together.</div>"
            "<table><tr><th>version</th><th>runs</th><th>table accuracy</th><th>right</th>"
            "<th>plausible range</th></tr>" + rows + "</table>")


def _funnel_html(funnel: dict) -> str:
    lines = []
    mined = funnel.get("mined")
    if mined:
        source = f" from <code>{_e(mined['source'])}</code>" if mined.get("source") else ""
        lines.append(f"<li>mined <b>{mined['candidates']}</b> candidate shapes{source}, "
                     f"{mined['clean']} gate-clean</li>")
    reviewed = funnel.get("reviewed")
    if reviewed:
        rejected = ", ".join(f"{_e(reason)} {n}" for reason, n in reviewed["rejected"].items()) or "none"
        lines.append(f"<li>review kept <b>{reviewed['kept']}</b> of {reviewed['total']}; "
                     f"rejected: {rejected}</li>")
    gate = funnel.get("gate")
    if gate:
        lines.append(f"<li>answer-key check: <b>{gate['n']}</b> golds, {gate['flagged']} flagged</li>")
    split = funnel.get("split")
    if split:
        lines.append(f"<li>split: <b>{split['curate']}</b> working, <b>{split['holdout']}</b> reserved</li>")
    if not lines:
        return "<div class='dim'>no mine, review, gate or split run in this workspace</div>"
    return "<ul>" + "".join(lines) + "</ul>"


def _spend_html(spend: dict) -> str:
    return (f"<div><b>{_e(spend['text'])}</b> over {spend['runs_with_spend']} run(s) that recorded "
            f"a cost: model calls ${spend['model_usd']:.4f}, warehouse scans ${spend['warehouse_usd']:.4f}. "
            "<span class='dim'>Summed from each run's telemetry; an agent run through --backend "
            "command carries its own cost.</span></div>")


def _slide_sections_html(verdict: dict) -> str:
    """The sections after the per-question table, each from one key
    `collect` computed; the routing section only when a routing report
    exists in the workspace."""
    parts = [
        "<h2>The reserved questions</h2>", _holdout_html(verdict),
        "<h2>Failures by shape, without &rarr; with context</h2>",
        _shapes_html(verdict.get("failure_shapes") or {"buckets": [], "n_failed": {}}),
        "<h2>Rounds</h2>", _rounds_html(verdict.get("rounds") or []),
    ]
    if verdict.get("routing"):
        parts += ["<h2>Routing accuracy</h2>", _routing_html(verdict["routing"])]
    parts += ["<h2>The question funnel</h2>", _funnel_html(verdict.get("funnel") or {}),
              "<h2>Spend</h2>", _spend_html(verdict.get("spend") or
                                             {"text": "$0.0000", "runs_with_spend": 0,
                                              "model_usd": 0.0, "warehouse_usd": 0.0})]
    return "\n".join(parts)


def _db_fingerprint(db: dict) -> str:
    """The footer's database identity: the file hash, or the dataset of a
    `bq://` spec, which has no file to hash."""
    if db.get("sha256"):
        return db["sha256"][:12]
    if db.get("kind") == "bigquery" and db.get("path"):
        try:
            dataset = parse_db_spec(db["path"]).dataset
        except ValueError:
            dataset = None
        return f"{dataset or db['path']} (BigQuery dataset)"
    return ""


def render(verdict: dict) -> str:
    ab = verdict["ablation"]
    counts = {"fixed": 0, "broke": 0, "unstable": 0, "unchanged": 0}
    for q in verdict["questions"]:
        counts["unchanged" if q["status"] in ("right", "wrong") else q["status"]] += 1
    word = verdict["verdict"]["word"]
    word_class = {"REAL GAIN": "green", "REAL LOSS": "red", "NOT PROVEN": "amber"}.get(word, "grey")
    # The safety word comes from the SAME tally the lead sentence prints, and
    # through `safety_word` so the page and the terminal cannot disagree.
    safety_name, safety_line = safety_word(
        counts["fixed"], counts["broke"], counts["unstable"],
        len(verdict["questions"]))
    safety_class = {"SAFE": "green", "MIXED": "amber",
                    "REGRESSION": "red"}[safety_name]
    p_text = verdict["p_text"]
    pct = p_text["luck_pct"]
    luck = p_text["luck_text"]
    # the counts in the luck sentence must be the ones the p was computed from:
    # the clustered test votes per question, the tally counts statuses,
    # and the two disagree whenever a question flips both ways between runs
    swing = ab["clustered"] or ab["mcnemar"]
    n_q = ab["n_questions"] or len(verdict["questions"])
    n_reps = ab["n_reps"] or 1
    cand, base = verdict["candidate"], verdict["baseline"]
    cand_label = cand["label"]
    headline = {
        "REAL GAIN": f"{cand_label} helps, and the gain is not luck.",
        "REAL LOSS": f"{cand_label} hurts, and the loss is not luck.",
        "NO CHANGE": f"{cand_label} changed nothing.",
    }.get(word, f"{cand_label} is not proven to help yet.")
    lead = (f"On the <b>{n_q} working questions</b>, each asked <b>{n_reps} times</b>, the model got "
            f"<b>{ab['without_context']['k']} of {ab['without_context']['n']}</b> answers right with no "
            f"context and <b>{ab['with_context']['k']} of {ab['with_context']['n']}</b> with "
            f"<code>{_e(cand_label)}</code>. That is <b>{ab['without_context']['point'] * 100:.0f}% "
            f"&rarr; {ab['with_context']['point'] * 100:.0f}%</b>. Question by question: "
            f"<span class='green'>{counts['fixed']} fixed</span>, <span class='red'>{counts['broke']} "
            f"broke</span>, {counts['unstable']} unstable, {counts['unchanged']} did not move.")
    if ab["mcnemar"]["b"] + ab["mcnemar"]["c"] == 0:
        luck_line = "The two setups never disagreed on any question."
    elif pct >= 100:
        luck_line = EVEN_SWING
    elif word == "NOT PROVEN":
        luck_line = (f"A swing of {swing['c']} fixed and {swing['b']} broke happens by luck "
                     f"about <b>{luck} times in 100</b>. The bar for \"proven\" is 5 in 100. "
                     f"<b>More questions would decide it; asking the same {n_q} again would not.</b>")
    else:
        luck_line = (f"A swing of {swing['c']} fixed and {swing['b']} broke happens by luck "
                     f"about <b>{luck} times in 100</b>; the bar for \"proven\" is 5 in 100.")
    if ab["clustered"]:
        c = ab["clustered"]
        stats_line = (f"McNemar's exact test, counting each question once by majority over its "
                      f"{n_reps} runs: fixed {c['c']}, broke {c['b']}, p = {p_text['clustered']}. "
                      f"Counting every run separately: fixed {ab['mcnemar']['c']}, broke "
                      f"{ab['mcnemar']['b']}, p = {p_text['pooled']}, which overstates the evidence "
                      f"because runs of one question are not independent. Wilson 95% intervals on the "
                      f"rates. The verdict word rests on p = {p_text['defended']}.")
    else:
        stats_line = (f"McNemar's exact test on {ab['n_paired']} paired answers: fixed "
                      f"{ab['mcnemar']['c']}, broke {ab['mcnemar']['b']}, p = {p_text['defended']}. "
                      f"One run per setup: more runs would tell unstable questions from real flips.")
    lo_b, hi_b = round(ab["without_context"]["low"] * 100), round(ab["without_context"]["high"] * 100)
    lo_c, hi_c = round(ab["with_context"]["low"] * 100), round(ab["with_context"]["high"] * 100)
    overlap = lo_c <= hi_b and lo_b <= hi_c
    range_note = ("the two ranges overlap, which is why this is not called" if overlap and word == "NOT PROVEN"
                  else "the two ranges do not overlap" if not overlap else "")

    caveats = []
    # First, because it bounds every other line on the page. The word here
    # comes from one arm pair with no gates and no working set beside it, so
    # a reader must not carry it away as a round's verdict.
    ungated = verdict["verdict"].get("ungated")
    if ungated:
        caveats.append(f"<li>{_e(ungated)}</li>")
    if cand["on_holdout"]:
        caveats.append(f"<li>Measured on the reserved questions, scored {verdict['holdout_looks']} time(s). "
                       f"{'This is the number to report.' if verdict['holdout_looks'] == 1 else 'A second look makes it a training score.'}</li>")
    else:
        caveats.append(f"<li>This is a <b>training score</b>: the rules were written from these same {n_q} "
                       f"questions. The <b>{verdict['n_holdout']} reserved questions</b> are sealed and "
                       f"scored {verdict['holdout_looks']} times; they are the honest test.</li>")
    gate = verdict["gate"]
    if gate:
        flagged_here = [q["id"] for q in verdict["questions"] if q.get("key_flagged")]
        if gate["flagged"]:
            here = (f" {', '.join(_e(i) for i in flagged_here)} scored here: both setups are marked wrong "
                    f"on a question whose answer key is itself wrong." if flagged_here else "")
            caveats.append(f"<li>The <b>answer-key check</b> flagged {gate['flagged']} of {gate['n']} "
                           f"correct answers.{here} Fix the key, then re-check (free).</li>")
        else:
            caveats.append(f"<li>The answer-key check passed all {gate['n']} correct answers.</li>")
    else:
        caveats.append("<li class='red'>The answer key was never checked in this workspace.</li>")
    if (verdict.get("answer_key") or {}).get("caveat"):
        caveats.append(f"<li class='amber'>{_e(verdict['answer_key']['caveat'])}</li>")
    leak = verdict["leak"]
    if leak["status"] == "clean":
        caveats.append("<li>Leak check on the context: <b>clean</b>, so the file does not contain the answers.</li>")
    elif leak["status"] == "LEAKED":
        caveats.append("<li class='red'>Leak check: the context <b>contains answers</b> of the scored questions; "
                       "the score measures copying.</li>")
    else:
        caveats.append("<li class='amber'>Leak check: <b>not run</b> on this context.</li>")
    # the customer's own agent answered, through --backend command: the model
    # option is argparse's default and names nothing; the runner version does,
    # and its spend never passed through contextlift
    if cand.get("backend") == "command":
        who = (f"agent: {cand.get('runner_version') or 'no --runner-version given'} "
               "(your own runner, via --backend command)")
        caveats.append("<li>Model spend: the agent's cost is its own; contextlift only counted "
                       "its calls.</li>")
    else:
        who = f"model {cand['model'] or 'unknown (imported predictions)'}"

        def spend(arm: dict) -> str:
            # None means nothing measured it, which is not the same as free
            return "not measured" if arm["cost_usd"] is None else f"${arm['cost_usd']:.4f}"

        caveats.append(f"<li>Model spend: without context {spend(base)} &middot; with context "
                       f"{spend(cand)} (0 when the predictions were imported).</li>")

    retrieved_only = bool(verdict.get("retrieval")) and not verdict["rule_lines"]

    return _PAGE.substitute(
        eyebrow=_e(f"{verdict['workspace']['name']} · {base['label']} vs {cand_label} · {who}"),
        rules_heading="The retrieved context" if retrieved_only else "The rules file",
        headline=_e(headline), lead=lead,
        word=_e(word), word_class=word_class, luck_line=luck_line,
        safety_word=_e(safety_name), safety_class=safety_class,
        safety_line=_e(safety_line),
        base_rate=f"{ab['without_context']['point'] * 100:.1f}",
        ctx_rate=f"{ab['with_context']['point'] * 100:.1f}",
        cand_label=_e(cand_label),
        lift=f"{ab['lift'] * 100:+.1f} pts", range_note=_e(range_note),
        base_low=lo_b, base_high=hi_b, ctx_low=lo_c, ctx_high=hi_c,
        stats_line=stats_line,
        caveats="".join(caveats),
        n_questions=n_q, n_reps=n_reps,
        rows="\n".join(_row(q) for q in verdict["questions"]),
        rules=_rules_html(verdict),
        # the same sentences the terminal prints after the ruling, under the word
        verdict_sentences="".join(
            f"<div class='dim' style='margin-top:6px'>{_e(sentence)}</div>"
            for sentence in ([f"difference {_e(verdict['lift_interval_text'])} at 95%"]
                             if verdict.get("lift_interval_text") else [])
            + list(verdict.get("verdict_sentences") or [])),
        slide_sections=_slide_sections_html(verdict),
        base_arm=_e(base["arm_id"]), base_runs=_e(", ".join(base["score_runs"])),
        cand_arm=_e(cand["arm_id"]), cand_runs=_e(", ".join(cand["score_runs"])),
        who=_e(who),
        db_sha=_e(_db_fingerprint(verdict["workspace"]["db"])),
        version=_e(verdict["contextlift_version"]),
        generated_at=_e(verdict["generated_at"]),
    )


def bundle_inventory(verdict: dict) -> dict:
    """What a verdict carries that a reader may not want to send on: counts
    of questions, SQL statements (the answer key's plus every attempt), row
    previews and the rows in them, rule lines, and the bq:// spec (None on
    SQLite). Computed from the dict that was written, so the printed
    inventory and the file cannot disagree."""
    questions = verdict["questions"]
    previews = [q["scorer_saw"] for q in questions
                if q.get("scorer_saw") and q["scorer_saw"].get("rows") is not None]
    db = (verdict.get("workspace") or {}).get("db") or {}
    rule_lines = verdict.get("rule_lines") or []
    retrieval = verdict.get("retrieval")
    return {
        "questions": len(questions),
        # what the candidate gave the model. "the rules file (0 lines)" was
        # printed for a retrieval arm that has no rules file at all.
        "context": (f"the rules file ({len(rule_lines)} lines)" if rule_lines
                    else f"the retrieved context for {retrieval['n_questions']} questions"
                    if retrieval else "no context file"),
        "rounds": [f"{r['n']} {r['label']} ({r.get('context') or 'no context file'})"
                   for r in (verdict.get("rounds") or [])],
        "sql_statements": sum(
            (1 if q["gold_sql"] else 0) + sum(1 for s in q["base_sql"] if s)
            + sum(1 for s in q["ctx_sql"] if s) for q in questions),
        "row_previews": len(previews),
        "rows": sum(len(p["rows"]["gold"]) + len(p["rows"]["pred"]) for p in previews),
        "rule_lines": len(verdict.get("rule_lines") or []),
        "bq_spec": db.get("path") if db.get("kind") == "bigquery" else None,
    }


def write_bundle(workspace: str | pathlib.Path, out: str | None = None,
                 baseline: str | None = None, candidate: str | None = None,
                 include_rows: bool = True) -> tuple[pathlib.Path, pathlib.Path]:
    workspace = pathlib.Path(workspace)
    verdict = collect(workspace, baseline, candidate, include_rows=include_rows)
    html_path = pathlib.Path(out) if out else workspace / "verdict.html"
    json_path = html_path.with_suffix(".json")
    json_path.write_text(json.dumps(verdict, indent=1, default=str))
    html_path.write_text(render(verdict))
    return html_path, json_path
