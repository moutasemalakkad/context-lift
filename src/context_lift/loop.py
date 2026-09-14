# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Where a workspace stands in the loop, and what to run next.

`api_next` reads index.json, manifests and saved reports and returns the one
next step: its state name, the exact argv, whether it costs money, whether it
reads the reserved questions, and the sentence for the progress rail. That is
what `contextlift next` prints and what `contextlift chat` proposes.

It computes nothing a caller could get wrong: a comparison is `bundle.collect`
verbatim, the same payload `verdict.html` is built from, and every rate arrives
as the text to print.

The launch rails live here too - `validate_launch`, `is_paid`,
`touches_holdout`, `start_job` - so anything that runs a command on a workspace
goes through the same checks: an argv that is not on the allowlist is refused, a
paid command needs a budget and a typed RUN, and a command that reads the
reserved questions needs the workspace name."""
from __future__ import annotations

import shlex
import csv
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser

from .ablate import climb, pool, safety_word
from .bundle import GATE_MEANINGS, _arm_material, collect, holdout_readings
from .contextmap import build_map, parse_schema
from .review import answer_key_caveat
from .triage import BUCKET_MEANINGS, COLUMN_COUNT, DID_NOT_RUN, ROW_COUNT, WRONG_VALUES, triage
from .workspace import (answer_key_review, fully_scored, has_context, input_paths, only_holdout,
                        rebuild_index, reserved_pair, sha256_of_file)



def _manifest(workspace: pathlib.Path, run_id: str) -> dict:
    return json.loads((workspace / "runs" / run_id / "manifest.json").read_text())


def _scored_files(workspace: pathlib.Path, index: dict,
                  score_runs: list[str]) -> list[tuple[str, str]]:
    """(saved report, predictions) per distinct predictions file, oldest
    first, both as paths inside the workspace. Scoring the same file twice is
    a duplicate observation, not a repetition; the rule bundle._arm_material
    uses."""
    runs = {r["run_id"]: r for r in index["runs"]}
    newest: dict[str, str] = {}
    for run_id in sorted(score_runs, key=lambda rid: runs[rid]["started_at"] or ""):
        newest[runs[run_id]["inputs"].get("predictions")] = run_id
    files = []
    for run_id in sorted(newest.values(), key=lambda rid: runs[rid]["started_at"] or ""):
        manifest = _manifest(workspace, run_id)
        report = (manifest["outputs"].get("save_report") or [{}])[0]
        predictions = manifest["inputs"].get("predictions") or {}
        if report.get("copy") and predictions.get("copy"):
            files.append((f"runs/{run_id}/{report['copy']}",
                          f"runs/{run_id}/{predictions['copy']}"))
    return files


def _report_paths(workspace: pathlib.Path, index: dict, score_runs: list[str]) -> list[str]:
    return [report for report, _ in _scored_files(workspace, index, score_runs)]


def _arm_context_path(workspace: pathlib.Path, arm: dict) -> str | None:
    """The context file the arm answered with, as the run kept it: the exact
    version, not whatever context/rules.md holds now."""
    for run_id in arm.get("generate_runs") or []:
        record = _manifest(workspace, run_id)["inputs"].get("context") or {}
        if record.get("copy"):
            return f"runs/{run_id}/{record['copy']}"
    return None


def _holdout_result(workspace: pathlib.Path, index: dict) -> dict | None:
    """The reserved-question score: the one number to report. The newest
    reading when there is one - the `ablate` over the two twins answered over
    holdout.csv, or an older `ablate --ids holdout_ids.txt` over arms that
    hold both halves - else pooled from the score runs on the reserved
    questions. Its own question set, so it never pools with the working
    questions; the newest arm scored on it wins."""
    from .harness import load_report
    readings = [r for r in holdout_readings(workspace, index) if r.get("word")]
    # by clock, not by run id: two runs of the same second differ only by the
    # command in the id, and "ablate" sorts before "score"
    runs_by_id = {r["run_id"]: r for r in index["runs"]}
    started = lambda run_id: runs_by_id[run_id]["started_at"] or run_id
    readings.sort(key=lambda r: r.get("started_at") or "")
    chosen = None
    for arm in index["arms"]:
        for group in arm.get("sets", []):
            if group["on_holdout"] and group["score_runs"]:
                if chosen is None or arm["first_seen"] > chosen[0]["first_seen"]:
                    chosen = (arm, group)
    if readings and (chosen is None
                     or (readings[-1].get("started_at") or "")
                     >= max(started(rid) for rid in chosen[1]["score_runs"])):
        reading = readings[-1]
        return {"arm_id": reading.get("arm_id") or index["pins"].get("candidate"),
                "label": "with context, the reserved questions",
                "kind": "ablate", "run_id": reading["run_id"], "word": reading["word"],
                "rate": None, "low": None, "high": None,
                "rate_text": reading["with_text"], "without_text": reading["without_text"],
                "lift_text": reading["lift_text"], "lift_interval_text": reading["lift_interval_text"],
                "fixed": reading["fixed"], "broke": reading["broke"], "p_text": reading["p_text"],
                "n_questions": reading["n_questions"], "runs": reading["n_reps"],
                "score_runs": [], "looks": index["holdout_looks"]}
    if chosen is None:
        return None
    arm, group = chosen
    reports = []
    for run_id in group["score_runs"]:
        manifest = _manifest(workspace, run_id)
        copy = manifest["outputs"]["save_report"][0]["copy"]
        reports.append(load_report(workspace / "runs" / run_id / copy))
    pooled = pool(reports)
    return {"arm_id": arm["arm_id"], "label": arm["label"],
            "rate": pooled.execution.point, "low": pooled.execution.low,
            "high": pooled.execution.high,
            # the text to print, so no caller rounds a rate a second way
            "rate_text": f"{pooled.execution.point:.1%}",
            "low_text": f"{pooled.execution.low:.1%}",
            "high_text": f"{pooled.execution.high:.1%}",
            "n_questions": len(reports[0].results), "runs": len(reports),
            "score_runs": group["score_runs"], "looks": index["holdout_looks"]}


def _zero_baseline_why(report) -> str:
    """What to fix when every repetition of the baseline scored 0, read from
    the largest failure bucket of its newest report. On one estate the
    baseline and two context arms all scored 0 of 18, and the printed hint
    ("usually a setup fault") did not apply: the golds were dashboard SQL
    and six answers hit the --max-bytes ceiling. Two arms were paid for
    before anyone read a pair side by side."""
    shapes = triage(report)
    n = report.n_scored
    closing = "0 correct in every repetition is not a baseline to climb from"
    if not shapes.counts:
        return closing
    bucket, count = shapes.ranked()[0]
    if bucket == DID_NOT_RUN:
        cause = (f"{count} of {n} did not run; if the cause is the --max-bytes ceiling, the "
                 "ceiling is a safety limit and not a score: re-score with a higher "
                 "--max-bytes, or write a rule for the partition filter the golds use")
    elif bucket in (ROW_COUNT, COLUMN_COUNT, WRONG_VALUES):
        cause = (f"{count} of {n} {bucket} ({BUCKET_MEANINGS[bucket]}): the answer key and the "
                 "agent disagree on the shape of an answer: read three pairs side by side "
                 "(the predictions file holds gold_sql beside pred_sql; `triage --show` names "
                 "the failing ids); either re-score both arms with --extra-columns tolerate, "
                 "or fix the golds (dashboard SQL with grouping columns the question never "
                 "asked for is not an answer)")
    else:
        cause = (f"{count} of {n} {bucket} ({BUCKET_MEANINGS.get(bucket, '')}): the agent's "
                 "SQL fails or returns nothing: check the dialect, the project and the dataset "
                 "before any context; a rule cannot fix a setup fault")
    return f"{cause}. {closing}"


def _newest_leak_status(index: dict, workspace: pathlib.Path,
                        context_sha: str | None, flag: str = "context") -> dict:
    """The newest leak run that read this context, by input hash. `flag` is
    the input it read it through: `context` for a rules file,
    `context_per_question` for what a retriever chose."""
    if not context_sha:
        return {"status": "no context", "run_id": None}
    leaks = [r for r in index["runs"] if r["command"] == "leak" and r["finished"]]
    for run in sorted(leaks, key=lambda r: r["started_at"] or "", reverse=True):
        recorded = (_manifest(workspace, run["run_id"])["inputs"].get(flag)
                    or {}).get("sha256")
        if recorded == context_sha:
            return {"status": "clean" if run["exit_code"] == 0 else "LEAKED",
                    "run_id": run["run_id"]}
    return {"status": "not checked", "run_id": None}


def _label(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _labels_path(workspace: pathlib.Path) -> pathlib.Path:
    return workspace / "context" / "labels.json"


def read_labels(workspace: pathlib.Path) -> dict[str, str]:
    """sha256 -> label, a small pointer file outside the hashed content, so
    a rename changes no version, number or manifest."""
    path = _labels_path(workspace)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {}
    return {k: str(v) for k, v in data.items() if isinstance(data, dict)}


def _context_writes(workspace: pathlib.Path, index: dict) -> list[dict]:
    """Every recorded `context-write`, oldest first: the bytes it wrote (by
    hash), its sequential number and note from version.json, the previous
    bytes' hash. A write without version.json predates the numbering."""
    writes = []
    runs = [r for r in index["runs"] if r["command"] == "context-write" and r["finished"]
            and r["exit_code"] == 0]
    for run in sorted(runs, key=lambda r: r["started_at"] or ""):
        manifest = _manifest(workspace, run["run_id"])
        record = (manifest.get("outputs", {}).get("to") or [{}])[0]
        if not record.get("sha256"):
            continue
        version_path = _copy_path(workspace, manifest, "outputs", "version_out")
        version = json.loads(version_path.read_text()) if version_path else {}
        previous_path = _copy_path(workspace, manifest, "outputs", "previous_out")
        text_path = workspace / "runs" / run["run_id"] / record["copy"] if record.get("copy") else None
        writes.append({
            "run_id": run["run_id"], "sha256": record["sha256"],
            "number": version.get("version"), "note": version.get("note") or manifest.get("options", {}).get("note") or "",
            "source": version.get("source") or manifest.get("options", {}).get("source") or "",
            "written_at": manifest.get("started_at"),
            "previous_sha256": hashlib.sha256(previous_path.read_bytes()).hexdigest() if previous_path else None,
            "text": text_path.read_text() if text_path and text_path.exists() else "",
        })
    return writes


def api_context(workspace: pathlib.Path) -> dict:
    """Every version of the rules file: one row per recorded write (its
    number and note), plus one "unrecorded" row per hash a run used that no
    write produced (a hand edit), plus the working copy on disk. Lineage
    is by hash: a restore writes old bytes under a new number, and the row
    says which earlier version it repeats."""
    index = rebuild_index(workspace)
    labels = read_labels(workspace)
    started = {r["run_id"]: r["started_at"] or "" for r in index["runs"]}
    arms_by_sha = {a["context_sha256"]: a for a in index["arms"] if a["context_sha256"]}
    # round numbers follow api_rounds' order: fully scored versions with
    # context, by first appearance (a position, not a statistic)
    scored = sorted((a for a in index["arms"] if has_context(a) and fully_scored(a)),
                    key=lambda a: a["first_seen"])
    round_by_sha = {a["context_sha256"]: n for n, a in enumerate(scored, start=1)
                    if a["context_sha256"]}

    def row(sha: str, text: str, path: str, first_run: str) -> dict:
        arm = arms_by_sha.get(sha)
        return {
            "sha256": sha, "short": sha[:8],
            "label": labels.get(sha) or _label(text, arm["label"] if arm else "rules"),
            "named": sha in labels,
            "path": path,
            "rules": sum(1 for line in text.splitlines() if line.startswith("- ")),
            "first_seen": first_run,            # the run that first carried these bytes
            "at": started.get(first_run, ""),
            "used_by": (arm["generate_runs"] + arm["score_runs"]) if arm else [],
            "arm_id": arm["arm_id"] if arm else None,
            "round": round_by_sha.get(sha),
            "leak": _newest_leak_status(index, workspace, sha),
            "text": text,
        }

    versions = []
    seen_writes: dict[str, int] = {}
    first_write_at: dict[str, str] = {}
    for write in _context_writes(workspace, index):
        entry = row(write["sha256"], write["text"], "context/rules.md", write["run_id"])
        entry.update(number=write["number"], note=write["note"], source=write["source"],
                     run_id=write["run_id"], previous_sha256=write["previous_sha256"],
                     same_as=seen_writes.get(write["sha256"]))
        if write["sha256"] not in seen_writes:
            seen_writes[write["sha256"]] = write["number"]
            first_write_at[write["sha256"]] = entry["at"]
        versions.append(entry)
    for sha, arm in arms_by_sha.items():
        # a hand edit some run used before any write recorded those bytes
        if sha in first_write_at and first_write_at[sha] <= started.get(arm["first_seen"], ""):
            continue
        first = _manifest(workspace, arm["generate_runs"][0])
        record = first["inputs"]["context"]
        text = (workspace / "runs" / first["run_id"] / record["copy"]).read_text() \
            if record.get("copy") else ""
        entry = row(sha, text, record["path"], arm["first_seen"])
        entry.update(number=None, note="", source="unrecorded", run_id=None,
                     previous_sha256=None, same_as=None)
        versions.append(entry)
    versions.sort(key=lambda v: v["at"])
    earlier: dict[str, str] = {}
    for entry in versions:                   # "same bytes as v2" / "as the unrecorded version"
        entry["same_as"] = earlier.get(entry["sha256"])
        earlier.setdefault(entry["sha256"], f"v{entry['number']}" if entry["number"] else "the unrecorded version")
    working = None
    working_path = workspace / "context" / "rules.md"
    if working_path.exists():
        text = working_path.read_text()
        sha = sha256_of_file(working_path)
        working = {"path": str(working_path.relative_to(workspace)),
                   "sha256": sha,
                   "rules": sum(1 for line in text.splitlines() if line.startswith("- ")),
                   "text": text,
                   "recorded_as": next((v["number"] for v in versions if v["sha256"] == sha and v["number"]), None)}
    return {"versions": versions, "working": working,
            "pins": index["pins"], "labels": labels}


def _version_label(workspace: pathlib.Path, arm: dict, fallback: str) -> str:
    """The "# heading" line of the rules file the arm used, else fallback."""
    first = _manifest(workspace, arm["generate_runs"][0])
    record = first["inputs"].get("context") or {}
    if not record.get("copy"):
        return fallback
    text = (workspace / "runs" / first["run_id"] / record["copy"]).read_text()
    return _label(text, fallback)


def api_rounds(workspace: pathlib.Path) -> dict:
    """The rounds ledger: the baseline, then one row per rules version that
    is fully scored, in the order the versions first appeared, each compared
    against the same baseline. Every number is `collect`'s (nothing here
    recomputes a statistic); the ladder comes from `ablate.climb` over the
    same pooled reports."""
    index = rebuild_index(workspace)
    arms = {a["arm_id"]: a for a in index["arms"]}
    baseline_id = index["pins"].get("baseline")
    if not baseline_id or baseline_id not in arms:
        return {"baseline": None, "rounds": [], "ladder_step": None}
    try:
        base_material = _arm_material(workspace, index, arms[baseline_id])
    except SystemExit as refusal:
        return {"baseline": None, "rounds": [], "ladder_step": None, "error": str(refusal)}
    base_pooled = pool(base_material["reports"])
    baseline = {"arm_id": baseline_id, "label": "without rules",
                "rate": base_pooled.execution.point, "runs": len(base_material["reports"])}

    # the twins answered over the reserved questions are the reading at the
    # end, not rounds of the ladder
    versions = sorted((a for a in index["arms"]
                       if has_context(a) and fully_scored(a) and not only_holdout(a)),
                      key=lambda a: a["first_seen"])
    rounds: list[dict] = []
    pooled_by_arm: dict[str, object] = {}
    for n, arm in enumerate(versions, start=1):
        material = _arm_material(workspace, index, arm)
        pooled_by_arm[arm["arm_id"]] = pool(material["reports"])
        # a retrieval arm has no rules file to take a heading from; its
        # version is the hash of what came back
        retrieved = arm.get("retrieved_sha256")
        fallback = f"retrieved {retrieved[:12]}" if retrieved else f"rules v{n}"
        entry = {"n": n, "arm_id": arm["arm_id"], "context_sha256": arm["context_sha256"],
                 "retrieved_sha256": retrieved,
                 "label": _version_label(workspace, arm, fallback),
                 "first_seen": arm["first_seen"], "runs": len(material["reports"]),
                 "rate": pooled_by_arm[arm["arm_id"]].execution.point,
                 "lift_vs_baseline": None, "fixed": None, "broke": None, "unstable": None,
                 "n_questions": None,
                 "word": None, "p_text": None, "lift_vs_previous": None, "ladder_rate": None,
                 "error": None}
        try:
            verdict = collect(workspace, baseline_id, arm["arm_id"])
        except SystemExit as refusal:
            entry["error"] = str(refusal)
            rounds.append(entry)
            continue
        counts = {"fixed": 0, "broke": 0, "unstable": 0}
        for question in verdict["questions"]:
            if question["status"] in counts:
                counts[question["status"]] += 1
        entry.update(counts)
        # The denominator the safety word needs: "broke 0" of what.
        entry["n_questions"] = len(verdict["questions"])
        entry["lift_vs_baseline"] = verdict["ablation"]["lift"]
        entry["word"] = verdict["verdict"]["word"]
        entry["p_text"] = verdict["p_text"]["defended"]
        rounds.append(entry)

    # the ladder only over the rounds that compare (same questions, same
    # number of runs); climb pairs each report with the one before it
    compared = [r for r in rounds if r["error"] is None]
    ladder_step = None
    if compared:
        try:
            report = climb([("baseline", base_pooled)]
                           + [(r["label"], pooled_by_arm[r["arm_id"]]) for r in compared])
        except ValueError:
            report = None
        if report is not None:
            ladder_step = report.ladder_step
            for entry, climbed in zip(compared, report.rounds[1:]):
                entry["ladder_rate"] = climbed.ladder_rate
                if entry is not compared[0]:
                    entry["lift_vs_previous"] = climbed.gain_vs_previous
    return {"baseline": baseline, "rounds": rounds, "ladder_step": ladder_step}


def _question_text(workspace: pathlib.Path, index: dict) -> dict[str, str]:
    """id -> question from every recorded CSV copy that has both columns
    (split outputs, generate inputs, scored predictions)."""
    known: dict[str, str] = {}
    for run in index["runs"]:
        run_dir = workspace / "runs" / run["run_id"]
        try:
            manifest = _manifest(workspace, run["run_id"])
        except (OSError, ValueError):
            continue
        records = list(manifest.get("inputs", {}).values()) \
            + [r for group in manifest.get("outputs", {}).values() for r in group]
        for record in records:
            for item in (record if isinstance(record, list) else [record]):
                copy = item.get("copy")
                if not copy or not copy.endswith(".csv"):
                    continue
                try:
                    with open(run_dir / copy, newline="") as handle:
                        reader = csv.DictReader(handle)
                        if not reader.fieldnames or "question" not in reader.fieldnames:
                            continue
                        for row in reader:
                            if row.get("id") and row.get("question"):
                                known.setdefault(row["id"], row["question"])
                except OSError:
                    continue
    return known


def api_answer_key(workspace: pathlib.Path) -> dict:
    """The newest gate run's findings joined to the newest split."""
    index = rebuild_index(workspace)
    gates = [r for r in index["runs"] if r["command"] == "gate" and r["finished"]
             and r["outputs"].get("save_report")]
    gate = max(gates, key=lambda r: r["started_at"] or "", default=None)
    rows: list[dict] = []
    gate_info = None
    if gate:
        manifest = _manifest(workspace, gate["run_id"])
        run_dir = workspace / "runs" / gate["run_id"]
        findings = {f["gold_id"]: f for f in json.loads(
            (run_dir / manifest["outputs"]["save_report"][0]["copy"]).read_text())}
        golds_record = manifest["inputs"]["golds"]
        with open(run_dir / golds_record["copy"], newline="") as handle:
            golds = list(csv.DictReader(handle))
        current = pathlib.Path(manifest["cwd"]) / golds_record["path"]
        stale = current.exists() and sha256_of_file(current) != golds_record["sha256"]
        gate_info = {"run_id": gate["run_id"], "path": golds_record["path"],
                     "sha256": golds_record["sha256"], "stale": stale,
                     "n": len(golds),
                     "flagged": sum(1 for f in findings.values() if f["defects"])}
        for row in golds:
            finding = findings.get(row["id"], {"defects": [], "detail": ""})
            rows.append({"id": row["id"], "question": row.get("question", ""),
                         "gold_sql": row.get("gold_sql", ""),
                         "defects": finding["defects"], "detail": finding["detail"],
                         "meaning": [GATE_MEANINGS.get(d, d) for d in finding["defects"]]})

    splits = [r for r in index["runs"] if r["command"] == "split" and r["finished"]]
    split = max(splits, key=lambda r: r["started_at"] or "", default=None)
    curate_ids: set[str] = set()
    holdout_ids: set[str] = set()
    split_info = None
    if split:
        manifest = _manifest(workspace, split["run_id"])
        run_dir = workspace / "runs" / split["run_id"]
        def ids(flag: str) -> set[str]:
            record = manifest["outputs"].get(flag)
            if not record:
                return set()
            return set((run_dir / record[0]["copy"]).read_text().split())
        curate_ids, holdout_ids = ids("out_curate"), ids("out_holdout")
        split_info = {"run_id": split["run_id"], "curate": len(curate_ids),
                      "holdout": len(holdout_ids),
                      "seed": manifest["options"].get("seed"),
                      "holdout_fraction": manifest["options"].get("holdout_fraction")}
    # gate may have audited a bare golds file (id, gold_sql); the question
    # text then comes from any other recorded file that carries it
    if any(not row["question"] for row in rows):
        known = _question_text(workspace, index)
        for row in rows:
            row["question"] = row["question"] or known.get(row["id"], "")

    sealed = index["holdout_looks"] == 0
    for row in rows:
        if row["defects"]:
            row["status"] = "flagged"
        elif row["id"] in holdout_ids:
            row["status"] = "holdout"
            if sealed:
                row["gold_sql"] = None      # sealed: not rendered until opened
        elif row["id"] in curate_ids:
            row["status"] = "curate"
        elif split:
            row["status"] = "unsplit"       # split ran, but not over this id
        else:
            row["status"] = "not split yet"
    return {"gate": gate_info, "split": split_info, "rows": rows,
            "holdout_looks": index["holdout_looks"]}


def _copy_path(workspace: pathlib.Path, manifest: dict, group: str, flag: str) -> pathlib.Path | None:
    """The recorded copy of one input or output, or None when the run kept none."""
    records = manifest.get(group, {}).get(flag)
    if isinstance(records, dict):
        records = [records]
    if not records or not records[0].get("copy"):
        return None
    return workspace / "runs" / manifest["run_id"] / records[0]["copy"]


def _csv_rows(path: pathlib.Path) -> list[dict]:
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


ALLOWED_COMMANDS = (
    "mine", "gate", "split", "schema", "triage", "leak", "ablate", "attribute",
    "climb", "curate-examples", "score", "generate", "import", "pin",
    "context-write", "bundle",
    # the retrieval path and the review between mine and gate: free, or
    # billed only through a `--db bq://` scan that is_paid already guards
    "catalog-list", "draft-context", "catalog-pull", "catalog-route", "score-routing",
    "routing-gate", "review", "draft-rules", "profile", "log",
)
OUTPUT_PATH_FLAGS = (
    "--out", "--save-report", "--clean-out", "--flagged-out", "--out-curate",
    "--out-holdout", "--out-curate-csv", "--out-holdout-csv", "--to",
)
QUESTION_FILE_FLAGS = ("--questions", "--predictions")


class Job:
    """One launch: one command, or several run in sequence under one log
    (a score per repetition). `run_id` is the last recorded run, `run_ids`
    every one; `exit_code` is the first non-zero code, else 0."""

    def __init__(self, job_id: str, argvs: list[list[str]]):
        self.id = job_id
        self.argvs = argvs
        self.argv = argvs[0]
        self.lines: list[str] = []
        self.done = False
        self.exit_code: int | None = None
        self.run_id: str | None = None
        self.run_ids: list[str] = []
        self.started = time.time()


JOBS: dict[str, Job] = {}


def _inside(workspace: pathlib.Path, path: str) -> bool:
    target = (workspace / path).resolve() if not pathlib.Path(path).is_absolute() \
        else pathlib.Path(path).resolve()
    root = workspace.resolve()
    return target == root or root in target.parents


def split_flags(args: list[str]) -> list[str]:
    """`--flag=value` written back as `--flag`, `value`.

    argparse accepts both spellings, and every check below looks for a flag and
    then reads the item after it. Without this, `--db=bq://p/d` scanned a
    warehouse with no budget and `--predictions=holdout.csv` opened the reserved
    questions with no confirmation, because neither flag was ever seen."""
    out: list[str] = []
    for arg in args:
        if isinstance(arg, str) and arg.startswith("--") and "=" in arg:
            flag, value = arg.split("=", 1)
            out += [flag, value]
        else:
            out.append(arg)
    return out


def validate_launch(workspace: pathlib.Path, command: str, args: list[str]) -> str | None:
    """The reason a launch is refused, or None."""
    args = split_flags(args)
    if command not in ALLOWED_COMMANDS:
        return f"{command} is not one of the commands that may be launched for you"
    if any(not isinstance(a, str) for a in args):
        return "arguments must be strings"
    if any(a.startswith("--workspace") for a in args):
        return "the workspace is fixed by the server"
    for i, arg in enumerate(args):
        if arg in OUTPUT_PATH_FLAGS:
            if i + 1 >= len(args) or not _inside(workspace, args[i + 1]):
                return f"{arg} must point inside the workspace"
    if any(a.startswith("<") and a.endswith(">") for a in args):
        return "fill in the <placeholders> first"
    return None


# on BigQuery, draft-context and log read INFORMATION_SCHEMA: a billed scan
SQL_COMMANDS = ("score", "gate", "triage", "ablate", "attribute", "climb", "mine",
                "schema", "profile", "draft-context", "log")


def db_of(args: list[str]) -> str | None:
    return args[args.index("--db") + 1] if "--db" in args and args.index("--db") + 1 < len(args) else None


def generate_flags(db: str | None) -> list[str]:
    """The flags a proposed `generate` needs on this workspace's database.

    A warehouse wants BigQuery SQL, not the SQLite default. The Vertex
    project comes from CONTEXTLIFT_PROJECT: the SDK reads GOOGLE_CLOUD_PROJECT,
    a different variable that is often unset, so a proposal without
    `--project` failed at the first call."""
    flags: list[str] = []
    if (db or "").startswith("bq://"):
        flags += ["--dialect", "BigQuery"]
    project = os.environ.get("CONTEXTLIFT_PROJECT")
    if project:
        flags += ["--project", project]
    return flags


def is_paid(command: str, args: list[str]) -> bool:
    """A command that spends money: a model call, or SQL on a warehouse
    (`--db bq://...`: every scan is billed). An estimate is free."""
    args = split_flags(args)
    if "--estimate-only" in args:
        return False
    if command == "generate":
        backend = args[args.index("--backend") + 1] if "--backend" in args else "gemini"
        return backend != "echo"
    if command == "score" and "--judge-gemini" in args:
        return True
    if command == "review":
        backend = args[args.index("--backend") + 1] if "--backend" in args else "gemini"
        return "--draft" in args and backend != "echo"
    # both call a model and both read as free until this line existed, so the
    # guided path and `chat --run` could launch one at a customer site with no
    # budget and no typed confirmation
    if command in ("draft-rules", "rewrite-questions"):
        backend = args[args.index("--backend") + 1] if "--backend" in args else "gemini"
        return backend != "echo"
    return command in SQL_COMMANDS and (db_of(args) or "").startswith("bq://")


def holdout_ids(workspace: pathlib.Path) -> set[str]:
    index = rebuild_index(workspace)
    splits = [r for r in index["runs"] if r["command"] == "split" and r["finished"]]
    split = max(splits, key=lambda r: r["started_at"] or "", default=None)
    if not split:
        return set()
    manifest = _manifest(workspace, split["run_id"])
    record = manifest["outputs"].get("out_holdout")
    if not record:
        return set()
    return set((workspace / "runs" / split["run_id"] / record[0]["copy"]).read_text().split())


def touches_holdout(workspace: pathlib.Path, command: str, args: list[str]) -> bool:
    """True when a command would read reserved questions: a generate/score
    over a file that holds their rows, or an ablate/climb whose `--ids` list
    names them (a reading of reports that hold both halves)."""
    if command not in ("generate", "score", "ablate", "climb"):
        return False
    args = split_flags(args)
    sealed = holdout_ids(workspace)
    if not sealed:
        return False
    flags = QUESTION_FILE_FLAGS if command in ("generate", "score") else ("--ids",)
    for flag in flags:
        if flag in args and args.index(flag) + 1 < len(args):
            path = args[args.index(flag) + 1]
            candidate = pathlib.Path(path) if pathlib.Path(path).is_absolute() else workspace / path
            if not candidate.is_file():
                continue
            if flag == "--ids":
                ids = {line.strip() for line in candidate.read_text().splitlines()
                       if line.strip() and not line.startswith("#")}
            else:
                with open(candidate, newline="") as handle:
                    ids = {row.get("id") for row in csv.DictReader(handle)}
            if ids & sealed:
                return True
    return False


def _subprocess_env() -> dict:
    """A launched command runs the same contextlift code as the server: the
    package's parent dir goes first on PYTHONPATH (relative entries would
    stop resolving from the workspace cwd)."""
    package_root = str(pathlib.Path(__file__).resolve().parent.parent)
    existing = os.environ.get("PYTHONPATH", "")
    return {**os.environ, "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": package_root + (os.pathsep + existing if existing else "")}


# commands whose CLI takes --budget and --estimate-only (generate: model
# spend; the rest: a BigQuery dry-run estimate, mine's measured once the
# shapes are ranked)
BUDGET_COMMANDS = ("generate", "score", "gate", "triage", "ablate", "attribute", "climb", "mine",
                   "review", "draft-rules", "rewrite-questions")


def cost_note(command: str, args: list[str]) -> str:
    """The line printed under a paid command: what it costs and how to
    price it, naming only flags the command has. Empty for a free command.
    `next` used to print one fixed tag; for `schema` it said to add --budget
    and price it with --estimate-only, and schema takes neither (it runs a
    few INFORMATION_SCHEMA queries per dataset, each billed at BigQuery's
    10 MB minimum), so argparse answered "unrecognized arguments"."""
    if not is_paid(command, args):
        return ""
    if command in BUDGET_COMMANDS:
        return "costs money: add --budget USD, and price it first with --estimate-only"
    if command == "log":
        return "costs money: price it first with --estimate-only"
    return ("costs money: a few metadata queries per dataset (BigQuery's 10 MB minimum "
            "each, cents); this command has no --estimate-only")


def authorize(workspace: pathlib.Path, commands: list[list[str]], budget: float | None,
              confirmed: bool, unseal: str | None) -> str | None:
    """The reason a set of commands may not start, or None.

    One place for the three rules, so every caller enforces the same thing: an
    argv that is not on the allowlist or points outside the workspace is
    refused, a command that spends money needs a budget and an explicit
    confirmation, and a command that reads the reserved questions needs the
    workspace folder name typed exactly. It used to be a method on the web
    server; the terminal asks the same questions and then calls this."""
    if not commands or not all(isinstance(c, list) and c for c in commands):
        return "no command given"
    for argv in commands:
        refusal = validate_launch(workspace, argv[0], argv[1:])
        if refusal:
            return refusal
    if any(is_paid(c[0], c[1:]) for c in commands):
        if budget is None:
            return "a paid command needs a budget (USD)"
        if not confirmed:
            return "a paid command needs an explicit confirmation"
    if any(touches_holdout(workspace, c[0], c[1:]) for c in commands):
        name = workspace.resolve().name
        if unseal != name:
            return (f"this reads the reserved questions: type the workspace name ({name}) "
                    "to open them")
    return None


def start_job(workspace: pathlib.Path, command: str, args: list[str],
              budget: float | None = None, then: list[list[str]] | None = None) -> Job:
    """Launch `command args`, then each argv in `then` after it finishes,
    all under one job. The caller validated every argv."""
    def full(command: str, args: list[str]) -> list[str]:
        argv = [sys.executable, "-m", "context_lift", "--workspace", str(workspace), command, *args]
        if budget is not None and command in BUDGET_COMMANDS and "--budget" not in args:
            argv += ["--budget", str(budget)]
        return argv

    job = Job(uuid.uuid4().hex[:10], [full(command, args)] + [full(c[0], c[1:]) for c in (then or [])])
    JOBS[job.id] = job

    def pump() -> None:
        for number, argv in enumerate(job.argvs):
            if number:
                job.lines.append(f"--- {number + 1} of {len(job.argvs)}: {shell_line(argv[argv.index('--workspace') + 2:])}")
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, bufsize=1, cwd=str(workspace),
                                       env=_subprocess_env())
            assert process.stdout is not None
            for line in process.stdout:
                job.lines.append(line.rstrip("\n"))
                # workspace.recorded prints this on stderr (merged here) when
                # the manifest is written; pin writes pins.json and never does
                if line.startswith("recorded -> "):
                    job.run_id = line.split("recorded -> ", 1)[1].strip().rstrip("/").rsplit("/", 1)[-1]
                    job.run_ids.append(job.run_id)
            code = process.wait()
            if job.exit_code in (None, 0):
                job.exit_code = code
        job.done = True

    threading.Thread(target=pump, daemon=True).start()
    return job


def _relative(workspace: pathlib.Path, path: str | None) -> str | None:
    if not path:
        return None
    candidate = pathlib.Path(path)
    try:
        return str(candidate.resolve().relative_to(workspace.resolve()))
    except ValueError:
        return path


def absolute_argv(workspace: pathlib.Path, argv: list[str]) -> list[str]:
    """The same argv with every path joined to the workspace, so the printed
    command runs from any directory. `next` used to print `--golds
    dataset/questions.csv`, which is valid only with the workspace as the
    current directory; one level up it ended in a FileNotFoundError
    traceback. A token is a path when it does not start with `-` or `<`, is
    not absolute, has no `://` (a bq:// spec), and either contains `/` or
    names something under the workspace. The command name is never a path,
    even when a folder of that name exists (`bundle` writes one)."""
    root = workspace.resolve()

    def joined(token: str) -> str:
        if token.startswith(("-", "<")) or "://" in token or pathlib.Path(token).is_absolute():
            return token
        if "/" in token or (root / token).exists():
            return str(root / token)
        return token
    return argv[:1] + [joined(token) for token in argv[1:]]


def discover(workspace: pathlib.Path) -> dict:
    """Files a fresh workspace holds by convention, so the first Next line
    is runnable: a questions CSV (id, question, gold_sql), a schema text,
    a SQLite database, a `catalog.txt` (catalog-list's table list), or
    `bigquery.txt`. Looks in the workspace root and dataset/.

    `bigquery.txt` is the `bq://` spec on line 1, then optional lines:
    `datasets: A, B` (the datasets the schema covers, each bare or
    `data-proj.name`) and `catalog: PROJECT LOCATION [ENTRY_GROUP]` (the
    Knowledge Catalog the tables are documented in). Blank lines and
    #-comments are skipped."""
    found = {"questions": None, "schema": None, "db": None, "datasets": None, "catalog": None,
             "catalog_project": None, "catalog_location": None, "catalog_group": None}
    places = [workspace, workspace / "dataset"]
    csvs, schemas, dbs, catalogs = [], [], [], []
    pointer = workspace / "bigquery.txt"
    if pointer.is_file():
        lines = [line.strip() for line in pointer.read_text().splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        if lines and lines[0].startswith("bq://"):
            found["db"] = lines[0]
        for line in lines[1:]:
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "datasets" and value:
                found["datasets"] = [d.strip() for d in value.split(",") if d.strip()]
            elif key == "catalog" and value:
                parts = value.split()
                found["catalog_project"] = parts[0]
                found["catalog_location"] = parts[1] if len(parts) > 1 else None
                found["catalog_group"] = parts[2] if len(parts) > 2 else None
    for place in places:
        if not place.is_dir():
            continue
        for path in sorted(place.iterdir()):
            if path.suffix == ".csv":
                try:
                    with open(path, newline="") as handle:
                        fields = csv.DictReader(handle).fieldnames or []
                except OSError:
                    continue
                if "id" in fields and "gold_sql" in fields:
                    csvs.append((0 if "question" in fields else 1, path))
            elif path.suffix in (".sqlite", ".db", ".sqlite3"):
                dbs.append(path)
            elif path.name == "catalog.txt":
                catalogs.append(path)
            elif path.suffix == ".txt" and "schema" in path.name.lower():
                schemas.append(path)
    if csvs:
        csvs.sort(key=lambda pair: (pair[0], "question" not in pair[1].name, pair[1].name))
        found["questions"] = str(csvs[0][1].relative_to(workspace))
    if schemas:
        found["schema"] = str(schemas[0].relative_to(workspace))
    if catalogs:
        found["catalog"] = str(catalogs[0].relative_to(workspace))
    if dbs and not found["db"]:
        found["db"] = str(dbs[0].relative_to(workspace))
    return found


def _answer_key_detail(workspace: pathlib.Path, index: dict, key: dict, found: dict) -> str | None:
    """Where the answer key came from, for step 0 of the strip: "mined from
    <log> (N candidates)" when a mine run wrote it, else "<file>, N
    questions, provided" when a questions file exists, else None."""
    mines = [r for r in index["runs"] if r["command"] == "mine" and r["finished"] and r["exit_code"] == 0]
    if mines:
        newest = max(mines, key=lambda r: r["started_at"] or "")
        manifest = _manifest(workspace, newest["run_id"])
        log = input_paths(manifest, "queries") or "a query log"
        out = _copy_path(workspace, manifest, "outputs", "out")
        n = len(_csv_rows(out)) if out else 0
        return f"mined from {log} ({n} candidates)"
    if key["gate"]:
        return f"{key['gate']['path']}, {key['gate']['n']} questions, provided"
    if found["questions"]:
        try:
            n = len(_csv_rows(workspace / found["questions"]))
        except OSError:
            return None
        return f"{found['questions']}, {n} questions, provided"
    return None


# The loop is a cycle, not a line. Five steps happen once per workspace, five
# repeat every round, so "step 10 of 10" starts lying the moment a second rules
# file is written. Each state names its phase and its place in that phase; the
# rail sentence is written here, in one place; callers only print it.
SETUP_STEPS = [("answer key", "find the questions"), ("gate", "check the answer key"),
               ("split", "reserve a share"), ("schema", "read the schema"),
               ("baseline", "answer without rules"), ("score baseline", "score without rules")]
ROUND_STEPS = [("rules", "write the rules"), ("leak", "leak check"),
               ("context arm", "answer with rules"), ("score context", "score with rules"),
               ("compare", "compare")]
STATE_PHASE = {
    "need_questions": ("setup", 0), "need_gate": ("setup", 1), "gate_stale": ("setup", 1),
    "need_split": ("setup", 2), "need_schema": ("setup", 3),
    "need_baseline": ("setup", 4), "score_baseline": ("setup", 5),
    "baseline_zero": ("round", 0), "need_triage": ("round", 0), "need_rules": ("round", 0),
    "leak_unchecked": ("round", 1), "leak_failed": ("round", 1),
    "need_context_arm": ("round", 2), "score_context": ("round", 3),
    # the reserved twins are scored at the compare step: the round is over,
    # the reading is what is left
    "score_holdout": ("round", 4),
    "verdict_holdout_ready": ("round", 4), "verdict_next_round": ("round", 4),
    "loop_closed": ("round", 4),
}
# Every state, derived from the phase map so the two cannot disagree. A
# test asserts STATES == frozenset(STATE_PHASE); deleting one half of a
# paired constant is how the halves drift.
STATES = frozenset(STATE_PHASE)
VERDICT_STATES = ("score_holdout", "verdict_holdout_ready", "verdict_next_round", "loop_closed")


def _loop_position(state: str, rounds_closed: int) -> dict:
    """Where the loop stands, as a caller prints it. `round` is the round being
    worked on, except at a verdict, where it is the round that just closed."""
    phase, index = STATE_PHASE[state]
    steps = SETUP_STEPS if phase == "setup" else ROUND_STEPS
    number = rounds_closed if state in VERDICT_STATES else rounds_closed + 1
    where = "setup" if phase == "setup" else f"round {number}"
    return {
        "phase": phase, "round": 0 if phase == "setup" else number,
        "rounds_closed": rounds_closed, "step_index": index, "step_count": len(steps),
        "setup_done": phase == "round",
        "rail": (f"{where} · closed" if state == "loop_closed"
                 else f"{where} · step {index + 1} of {len(steps)} · {steps[index][1]}"),
        "steps": [{"key": key, "label": label,
                   "state": "done" if i < index else ("now" if i == index else "todo")}
                  for i, (key, label) in enumerate(steps)],
    }


def _holdout_argv(workspace: pathlib.Path, index: dict, key: dict) -> tuple[list[str], str]:
    """The reading of the reserved questions, or an empty argv and the reason
    there is none.

    Rounds are answered over curate.csv, so no report of a round holds a
    reserved row. The reading is the two twins answered over holdout.csv,
    scored, and compared with a plain `ablate --without-report .. --with-report
    .. --show`: no `--ids`, because both files hold reserved rows only.
    `--ids` stays for reports imported from elsewhere that hold both halves.
    It must degrade, never raise: `api_next` is polled and calls this on every
    verdict."""
    if not key.get("split"):
        return [], "the questions have not been split yet"
    if not key["split"]["holdout"]:
        return [], ("the split reserved 0 questions, so there is nothing to read; split a "
                    "bigger answer key, or raise --holdout-fraction")
    pins = index["pins"]
    arms = {a["arm_id"]: a for a in index["arms"]}
    if not pins.get("baseline") or pins["baseline"] not in arms:
        return [], "there is no scored version without context (the baseline) yet"
    if not pins.get("candidate") or pins["candidate"] not in arms:
        return [], ("there is no fully scored version with context (the candidate) yet: "
                    "a rules or retrieval arm, every repetition scored")
    base, candidate = reserved_pair(index)
    if not base or not candidate:
        return [], ("the reserved questions have not been answered yet: `contextlift next` "
                    "proposes both versions over holdout.csv at the verdict")
    argv = ["ablate"]
    counted: dict[str, int] = {}
    for pair, flag in ((base, "--without-report"), (candidate, "--with-report")):
        paths = _report_paths(workspace, index, pair[1]["score_runs"])
        if not paths:
            return [], (f"{pair[0]['label']} answered the reserved questions but was not "
                        "scored; score every repetition first")
        counted[pair[0]["label"]] = len(paths)
        for path in paths:
            argv += [flag, path]
    # pooling needs the same number of repetitions on both sides. Without this
    # the command was printed anyway, raised at the one step the loop calls
    # one-way, blamed the question sets rather than the counts, and `next` then
    # printed the identical failing command again.
    sides = list(counted.values())
    if sides[0] != sides[1]:
        counts = ", ".join(f"{label} {n}" for label, n in counted.items())
        return argv, (f"the two versions bring a different number of scored repetitions "
                      f"({counts}), and a paired comparison needs the same number on both "
                      "sides. Score the missing repetitions, or run the ablate above by "
                      "hand with an equal number of --without-report and --with-report "
                      "files")
    argv += ["--show"]
    refusal = validate_launch(workspace, argv[0], argv[1:])
    if refusal is not None:
        return argv, refusal
    return argv, ""


def shell_line(argv: list[str]) -> str:
    """One argv joined into a line a person can paste into a shell.

    Every word is quoted when it needs to be. Joining on spaces looked fine
    for months because most flags are paths without spaces, and then the
    reserved-questions step printed

        generate ... --echo-response SELECT 1; --out arms/holdout_base.csv

    where the semicolon ended the shell line, dropped --out, --reps and
    --workers, and left the shell trying to run --out as a program. The step
    that prints it is the one the loop calls one-way, so a wrong command
    there costs a reserved reading.

    `--command` is the wider case: the person's own agent runner is a whole
    command line, spaces and all, in one argument.
    """
    return " ".join(shlex.quote(word) for word in argv)


def api_next(workspace: pathlib.Path) -> dict:
    """The one next command, from the files that exist. Placeholders in
    <angle brackets> mark what cannot be worked out from the files; the step is
    not runnable then, and `blockers` says what is missing.
    A score step that has several runs to score carries every argv in
    `commands`; `command` stays the first, for callers that run one."""
    index = rebuild_index(workspace)
    key = api_answer_key(workspace)
    context = api_context(workspace)
    found = discover(workspace)
    db = index["workspace"]["db"]["path"] or found["db"] or "<DB.sqlite>"
    blockers: list[str] = []
    runs = {r["run_id"]: r for r in index["runs"]}

    stages = ["answer key", "gate", "split", "baseline", "score baseline", "rules", "leak",
              "context arm", "score context", "compare"]
    answer_key = _answer_key_detail(workspace, index, key, found)

    # the arms of the loop itself. A twin answered over the reserved questions
    # is not a round and never a baseline: it is read once at the end, and
    # counting it would make the rail say "round 2" after a reading
    round_arms = [a for a in index["arms"] if not only_holdout(a)]
    rounds_closed = len([a for a in round_arms if has_context(a) and fully_scored(a)])

    def step(name: str, why: str, argv: list[str], state: str, stage: str | None = None,
             commands: list[list[str]] | None = None, label: str = "",
             also: list[dict] | None = None, blocker_code: str | None = None,
             goes_to: str | None = None, note: str = "", **extra) -> dict:
        current = stages.index(stage or name)
        strip = [{"name": s, "state": "done" if i < current else ("next" if i == current else "todo"),
                  **({"detail": answer_key} if s == "answer key" else {})}
                 for i, s in enumerate(stages)]
        commands = commands or ([argv] if argv else [])
        paid = any(is_paid(c[0], c[1:]) for c in commands)
        refusals = [validate_launch(workspace, c[0], c[1:]) for c in commands]
        runnable = bool(argv) and all(r is None for r in refusals)
        # the reason a command cannot run is a fact the caller must show; it used
        # to be reduced to a boolean here and the person met it as a traceback
        # on the cost sheet instead
        stopper = next((r for r in refusals if r), None)
        human = bool(extra.get("human"))
        reads_reserved = any(touches_holdout(workspace, c[0], c[1:]) for c in commands)
        # `argvs` stay relative: start_job runs them from the workspace. The
        # absolute copies are what a terminal prints, valid from anywhere.
        for other in also or []:
            other.setdefault("argvs_absolute", [absolute_argv(workspace, a) for a in other["argvs"]])
            other.setdefault("cost_note", next((cost_note(a[0], a[1:]) for a in other["argvs"]
                                                if cost_note(a[0], a[1:])), ""))
        return {"step": name, "why": why, "command": argv, "commands": commands,
                "stage_index": current, "answer_key": answer_key,
                "paid": paid, "runnable": runnable,
                "blockers": blockers, "strip": strip, "found": found,
                "n_runs": len(index["runs"]),
                "state": state,
                # a human step is one with nothing to run; `human` itself stays
                # byte-identical because chat.py serialises it
                "action": {"kind": "human" if (human and not commands)
                           else ("paid" if paid else "run"),
                           "label": label or name, "argvs": commands,
                           "argvs_absolute": [absolute_argv(workspace, c) for c in commands],
                           "goes_to": goes_to, "runnable": runnable or human, "paid": paid,
                           "cost_note": next((cost_note(c[0], c[1:]) for c in commands
                                              if cost_note(c[0], c[1:])), ""),
                           # computed, not assumed. It read False on every step,
                           # including the one that answers the reserved
                           # questions, so anything wrapping `next --json` in
                           # automation would be told no step reads them
                           "touches_holdout": reads_reserved,
                           "confirm": (["budget"] if paid else []) + (
                               ["name"] if reads_reserved else []),
                           "label_note": stopper or note},
                "also": also or [],
                "loop": _loop_position(state, rounds_closed),
                "blocker_code": blocker_code,
                **extra}

    if not key["gate"] and not found["questions"]:
        return step("answer key",
                    "no questions file yet. Mine candidates from a query log (a CSV with a "
                    "sql column; the question text is left for you to write), or write "
                    "dataset/questions.csv by hand with columns id, question, gold_sql",
                    ["mine", "--db", db, "--queries", "<query_log.csv>",
                     "--out", "dataset/candidates.csv", "--timeout", "60"],
                    state="need_questions", label="Copy the mine command", human=True)
    if not key["gate"]:
        return step("gate", "audit the answer key before trusting any score",
                    ["gate", "--db", db, "--golds", found["questions"] or "<questions.csv>",
                     "--timeout", "60"], state="need_gate", label="Check the answer key")
    if key["gate"]["stale"]:
        blockers.append("the answer key changed since gate ran")
        return step("gate", "the golds file on disk changed since the last audit",
                    ["gate", "--db", db, "--golds", key["gate"]["path"], "--timeout", "60"],
                    state="gate_stale", label="Check the answer key again",
                    blocker_code="answer_key_changed")
    gate_manifest = _manifest(workspace, key["gate"]["run_id"])
    clean_copy = gate_manifest["outputs"].get("clean_out", [{}])[0].get("copy")
    clean_path = f"runs/{key['gate']['run_id']}/{clean_copy}" if clean_copy else "<clean golds.csv>"
    if not key["split"]:
        # a cold run's gate flagged 112 of 205 and the loop went straight on to
        # split, so 55% of the key left without a word and the count only
        # reappeared on the verdict page, after every dollar was spent
        dropped = key["gate"]["flagged"]
        flagged_copy = gate_manifest["outputs"].get("flagged_out", [{}])[0].get("copy")
        flagged_path = (f"runs/{key['gate']['run_id']}/{flagged_copy}" if flagged_copy
                        else "the gate run's golds_flagged.csv")
        note = ""
        if dropped:
            note = (f"gate flagged {dropped} of {key['gate']['n']} golds and this split covers "
                    f"only the {key['gate']['n'] - dropped} that are clean. The flagged rows and the "
                    f"reason for each are in {flagged_path}; nothing puts them back. Read them "
                    "before you spend: a key that lost this many rows may not measure what you "
                    "meant to measure, and every rate below is computed over what is left")
        return step("split", "set questions aside now; curation must never see them",
                    ["split", "--golds", clean_path],
                    state="need_split", label="Reserve a share of the questions", note=note)
    split_manifest = _manifest(workspace, key["split"]["run_id"])
    def split_output(flag: str) -> str:
        record = split_manifest["outputs"].get(flag)
        return f"runs/{key['split']['run_id']}/{record[0]['copy']}" if record else f"<{flag}.csv>"
    curate_csv = split_output("out_curate_csv")
    holdout_csv = split_output("out_holdout_csv")
    curate_ids = split_output("out_curate")

    generates = [m for m in (_manifest(workspace, r["run_id"]) for r in index["runs"]
                              if r["command"] == "generate")]
    schema = next((m["inputs"]["schema"]["path"] for m in generates
                   if m.get("inputs", {}).get("schema")),
                  found["schema"] or "<schema.txt>")
    def schema_step(stage: str) -> dict | None:
        """The free step that has to happen before a model can be asked
        anything. Only offered where a generate is about to be proposed: a
        workspace whose predictions were imported never recorded a schema and
        is not waiting on one. It used to be a sentence telling the reader to
        go and run a command themselves, on a page whose whole claim is one
        card and one button."""
        if schema != "<schema.txt>":
            return None
        argv = ["schema", "--db", db, "--out", "dataset/schema.txt"]
        # the scope bigquery.txt and catalog.txt declare: the datasets line
        # carries the projects, the table list the tables; one read, one file
        if found["datasets"]:
            argv += ["--datasets", ",".join(found["datasets"])]
        if found["catalog"]:
            argv += ["--tables-file", found["catalog"]]
        return step("schema", "the model is shown the tables and columns before it is asked "
                    "anything; this reads them from the database and writes the text it sees",
                    argv,
                    # the ten-stage strip other callers read has no schema entry;
                    # it belongs to the step it is a precondition of
                    stage=stage, state="need_schema", label="Read the schema")

    def has_a_catalog_to_read() -> bool:
        """Whether a catalog arm is reachable at all from this workspace.

        Offered unconditionally, it printed five commands full of
        <CATALOG_PROJECT> placeholders as the longest block on the verdict
        screen of a SQLite workspace with no cloud project. It reads as
        not-ready rather than as not-configured.
        """
        # `db` is the RECORDED database, which is the SQLite file even on a
        # workspace whose bigquery.txt declares a warehouse. Both are read, so
        # a workspace that declares BigQuery still gets the offer with
        # placeholders (that is the prompt to configure it), and one that never
        # mentions a warehouse at all does not.
        return bool(found.get("catalog") or found.get("catalog_project")
                    or (db or "").startswith("bq://")
                    or (found.get("db") or "").startswith("bq://"))

    def catalog_arm() -> dict:
        """The retrieval arm as one offer, beside the rules arm: the table
        list, a draft page for every table in it, the catalog's own pages
        pulled beside the drafts, the routes graded against the list, then
        the arm. Placeholders where bigquery.txt has no `catalog:` line, so
        the offer says it is not ready instead of failing at the first
        command.

        Three directories, never one: `context/pages` holds the drafts a
        person reviews and pushes, `context/served` the bundle the catalog
        answers with, `context/docs_served` the pages that bundle holds and
        the arm routes over. The pull used to write into `context/pages`,
        the directory draft-context had just written and a person had just
        reviewed, and overwrote the review."""
        project = found["catalog_project"] or "<CATALOG_PROJECT>"
        location = found["catalog_location"] or "<LOCATION>"
        group = ["--entry-group", found["catalog_group"]] if found["catalog_group"] else []
        # no list yet: the offer writes one first, so the rest of the
        # sequence can name the file it is about to create
        catalog = found["catalog"] or "catalog.txt"
        datasets = ",".join(found["datasets"]) if found["datasets"] else "<DATASETS>"
        argvs = []
        if not found["catalog"]:
            argvs.append(["catalog-list", "--project", project, "--location", location, *group,
                          "--datasets", datasets, "--out", catalog])
        argvs += [
            ["draft-context", "--db", db, "--tables-file", catalog, "--out", "context/pages"],
            ["catalog-pull", "--project", project, "--location", location, *group,
             "--tables-file", catalog, "--out", "context/served",
             "--pages", "context/docs_served"],
            ["catalog-route", "--project", project, "--location", location, *group,
             "--questions", curate_csv, "--docs", "context/docs_served", "--catalog", catalog,
             "--routes-out", "context/catalog_routes.json",
             "--context-out", "context/catalog_per_question.json",
             "--routing-out", "context/catalog_routing.csv"],
            ["generate", "--questions", curate_csv, "--schema", schema,
             "--out", "arms/catalog.csv", "--reps", "3", "--workers", "8",
             "--context-per-question", "context/catalog_per_question.json", *generate_flags(db)],
        ]
        refusals = [validate_launch(workspace, argv[0], argv[1:]) for argv in argvs]
        stopper = next((r for r in refusals if r), None)
        paid = any(is_paid(argv[0], argv[1:]) for argv in argvs)
        notes = []
        if not found["catalog"]:
            # an unscoped list held 44 tables across three datasets on one
            # estate, and a push over that list would have written outside
            # the one dataset that was allowed
            notes.append("name the datasets the list may hold in --datasets: without them one "
                         "estate listed 44 tables across three datasets, and a push over that "
                         "list would have written outside the one dataset that was allowed")
        notes.append(stopper or ("the drafts, the catalog's own pages, the routes graded "
                                 "against the list, then the arm; compared like the rules arm"))
        return {"kind": "catalog_arm", "label": "build the catalog arm", "argvs": argvs,
                "confirm": ["budget"] if paid else [], "goes_to": None,
                "runnable": stopper is None, "paid": paid, "touches_holdout": False,
                "label_note": ". ".join(notes)}

    def reserved_twin(arm: dict | None) -> list[list[str]]:
        """The commands that answer the reserved questions the way this arm
        answered the working ones: the same runner, the same context, over
        holdout.csv. A rules arm is one generate; a retrieval arm routes
        again over the reserved questions, is leak-checked, and then
        answers."""
        if arm is None:
            return []
        options = None
        for run_id in arm.get("generate_runs") or []:
            manifest = _manifest(workspace, run_id)
            if manifest["command"] == "generate":
                options = manifest.get("options") or {}
                break
        # predictions made outside and imported: nothing here knows the
        # command that wrote them, and a guess would be a different version
        if options is None:
            return []
        backend = options.get("backend") or "gemini"
        runner = [] if backend == "gemini" else ["--backend", backend]
        if backend == "command":
            # the person's own agent: same runner, same version, no schema.
            # `--command` is stored under its argparse dest, and reading
            # options["command"] found nothing every time: workspace strips
            # that key as the subcommand name. So the reserved twin was built
            # with no runner at all and generate exited 2, at the last command
            # of the loop and straight after the workspace name was typed.
            if options.get("runner_command"):
                runner += ["--command", options["runner_command"]]
            if options.get("runner_version"):
                runner += ["--runner-version", options["runner_version"]]
        else:
            runner += ["--schema", schema, *generate_flags(db)]
            if backend == "gemini" and options.get("model"):
                runner += ["--model", options["model"]]
            # only the echo backend takes it; a gemini run carries the flag's
            # default in its manifest and copying that built a command with
            # both --model and --echo-response, which generate refuses
            if backend == "echo" and options.get("echo_response"):
                runner += ["--echo-response", options["echo_response"]]
        if not has_context(arm):
            return [["generate", "--questions", holdout_csv, *runner,
                     "--out", "arms/holdout_base.csv", "--reps", "3", "--workers", "8"]]
        if arm.get("retrieved_sha256"):
            project = found["catalog_project"] or "<CATALOG_PROJECT>"
            location = found["catalog_location"] or "<LOCATION>"
            group = ["--entry-group", found["catalog_group"]] if found["catalog_group"] else []
            catalog = found["catalog"] or "<catalog.txt>"
            name = f"arms/holdout_{arm['retrieved_sha256'][:8]}.csv"
            return [
                ["catalog-route", "--project", project, "--location", location, *group,
                 "--questions", holdout_csv, "--docs", "context/docs_served",
                 "--catalog", catalog, "--routes-out", "context/holdout_routes.json",
                 "--context-out", "context/holdout_per_question.json",
                 "--routing-out", "context/holdout_routing.csv"],
                ["leak", "--context-per-question", "context/holdout_per_question.json",
                 "--predictions", holdout_csv],
                ["generate", "--questions", holdout_csv, *runner,
                 "--context-per-question", "context/holdout_per_question.json",
                 "--out", name, "--reps", "3", "--workers", "8"],
            ]
        context_path = _arm_context_path(workspace, arm) or "<context/rules.md>"
        name = f"arms/holdout_{(arm['context_sha256'] or 'ctx')[:8]}.csv"
        return [["generate", "--questions", holdout_csv, *runner, "--context", context_path,
                 "--out", name, "--reps", "3", "--workers", "8"]]

    def reserved_offer(base_pair, candidate_pair) -> dict:
        """The one-way step at the end: both pinned versions answer the
        reserved questions once, are scored, and are read with one `ablate`.

        Rounds are answered over curate.csv, so no report of a round holds a
        reserved row and no forgotten `--ids` can leak one into triage,
        attribute, report.json or verdict.html. That is why the reserved
        questions get their own two arms instead of a slice of the round's."""
        reserved = key["split"]["holdout"] if key["split"] else 0
        if not reserved:
            working = key["split"]["curate"] if key["split"] else 0
            return {"kind": "holdout", "label": "There are no reserved questions to read",
                    "argvs": [], "confirm": [], "goes_to": None, "runnable": False,
                    "paid": False, "touches_holdout": True,
                    "label_note": f"the split held back 0 of {working + reserved} questions, so "
                                  "there is nothing to read: the answer key is too small to keep "
                                  "a share back. Mine more questions, or split again with a "
                                  "bigger --holdout-fraction before any curation"}
        arms_by_id = {a["arm_id"]: a for a in index["arms"]}
        candidate = arms_by_id.get(index["pins"].get("candidate") or "")
        baseline_arm = arms_by_id.get(index["pins"].get("baseline") or "")
        argvs: list[list[str]] = []
        blocked = ""
        for pair, arm, slot in ((base_pair, baseline_arm, "without context"),
                                (candidate_pair, candidate, "with context")):
            if pair:
                continue                       # this one has answered already
            if arm is None:
                blocked = blocked or f"there is no pinned version {slot} yet"
                continue
            twin = reserved_twin(arm)
            if not twin:
                blocked = blocked or (
                    f"{arm['label']} was imported, not generated, so the command that made it "
                    "is not recorded: answer the reserved questions with the same runner, "
                    "`import` the file and score it")
            argvs += twin
        note = ("one look: each version answers the reserved questions once, they are scored, "
                "and one ablate reads them. There is no undo.")
        if blocked:
            return {"kind": "holdout", "label": f"Read the {reserved} reserved questions once",
                    "argvs": [], "confirm": ["name"], "goes_to": None, "runnable": False,
                    "paid": False, "touches_holdout": True, "label_note": blocked}
        if not argvs:
            reading, refusal = _holdout_argv(workspace, index, key)
            argvs = [reading] if reading and not refusal else []
            note = refusal or ("one look: the two reserved arms, one ablate, no --ids. "
                               "There is no undo.")
        refusals = [validate_launch(workspace, argv[0], argv[1:]) for argv in argvs]
        stopper = next((r for r in refusals if r), None)
        paid = any(is_paid(argv[0], argv[1:]) for argv in argvs)
        runnable = bool(argvs) and stopper is None
        return {"kind": "holdout", "label": f"Read the {reserved} reserved questions once",
                "argvs": argvs, "confirm": ["name"] + (["budget"] if paid else []),
                "goes_to": None, "runnable": runnable, "paid": paid, "touches_holdout": True,
                "label_note": stopper or note}

    def training_offer() -> dict:
        """The reading of the round, over the working questions: the verdict
        and the rules behind it. Both used to be prose in the step's `why`, so
        the person built the paths by hand while the reserved command was
        printed in full."""
        arms_by_id = {a["arm_id"]: a for a in index["arms"]}
        baseline_arm = arms_by_id.get(index["pins"].get("baseline") or "")
        candidate = arms_by_id.get(index["pins"].get("candidate") or "")
        argvs: list[list[str]] = []
        if baseline_arm and candidate:
            without = _scored_files(workspace, index, baseline_arm["score_runs"])
            with_context = _scored_files(workspace, index, candidate["score_runs"])
            reading = ["ablate"]
            for flag, files in (("--without-report", without), ("--with-report", with_context)):
                for report, _ in files:
                    reading += [flag, report]
            reading += ["--ids", curate_ids, "--show"]
            argvs.append(reading)
            context_path = _arm_context_path(workspace, candidate)
            if context_path:
                # attribute needs the SQL, not only the verdicts: a rule is
                # credited against the query it changed
                blame = ["attribute"]
                for flag, files in (("--without-context", without), ("--with-context", with_context)):
                    for _, predictions in files:
                        blame += [flag, predictions]
                for flag, files in (("--without-report", without), ("--with-report", with_context)):
                    for report, _ in files:
                        blame += [flag, report]
                blame += ["--context", context_path]
                argvs.append(blame)
            # from round 2 on, one ablation cannot say whether the newest round
            # bought anything. A cold run's round 2 read +1.6 points while it
            # fixed 13 and BROKE 10, and only climb's ladder said the gain was
            # inside the noise floor. It was never offered.
            rounds = [a for a in index["arms"] if has_context(a) and fully_scored(a)]
            if len(rounds) > 1 and baseline_arm:
                ladder = ["climb", "--round", f"round 0: no context={without[0][0]}"]
                for n, arm in enumerate(sorted(rounds, key=lambda a: a["first_seen"]), start=1):
                    for report, _ in _scored_files(workspace, index, arm["score_runs"]):
                        ladder += ["--round", f"round {n}: {arm['label']}={report}"]
                ladder += ["--ids", curate_ids, "--show"]
                argvs.append(ladder)
        refusals = [validate_launch(workspace, argv[0], argv[1:]) for argv in argvs]
        stopper = next((r for r in refusals if r), None)
        return {"kind": "training", "label": "Read the round on the working questions",
                "argvs": argvs, "confirm": [], "goes_to": None,
                "runnable": bool(argvs) and stopper is None, "paid": False,
                "touches_holdout": False,
                "label_note": stopper or ("free, from the saved reports: the verdict over the "
                                          "working questions, which rule broke or fixed which "
                                          "question, and from round 2 on what each round bought "
                                          "against the noise floor")}
    baseline_arms = [a for a in round_arms if not has_context(a)]
    if not baseline_arms:
        blocked = schema_step("baseline")
        if blocked:
            return blocked
        return step("baseline", "measure accuracy with NO context first; every later "
                    "number is paired against this",
                    ["generate", "--questions", curate_csv, "--schema", schema,
                     "--out", "arms/base.csv", "--reps", "3", "--workers", "8",
                     *generate_flags(db)],
                    state="need_baseline", label="Answer the questions without rules")
    baseline = baseline_arms[0]

    def unscored(arm: dict) -> list[str]:
        """The prediction files of the arm no score run has read yet, in
        output order; every repetition must be scored before the arm counts
        as measured."""
        # Per content hash, the arm needs as many score runs as it has
        # output files with that hash: temperature-0 repetitions can be
        # byte-identical, and an import's copy is scored under another path.
        def absolute(manifest: dict, path: str) -> str:
            return str((pathlib.Path(manifest["cwd"]) / path).resolve())
        scored_by_hash: dict[str, int] = {}
        scored_paths: set[str] = set()
        for rid in arm["score_runs"]:
            manifest = _manifest(workspace, rid)
            record = manifest["inputs"].get("predictions") or {}
            if record.get("sha256"):
                scored_by_hash[record["sha256"]] = scored_by_hash.get(record["sha256"], 0) + 1
            if record.get("path"):
                scored_paths.add(absolute(manifest, record["path"]))
        outputs = []
        for run_id in arm["generate_runs"]:
            manifest = _manifest(workspace, run_id)
            for record in manifest.get("outputs", {}).get("out") or []:
                if record.get("sha256"):
                    outputs.append((record["sha256"], absolute(manifest, record["path"])))
        pending: list[str] = []
        for sha in dict.fromkeys(sha for sha, _ in outputs):
            group = [path for s, path in outputs if s == sha]
            needed = len(group) - scored_by_hash.get(sha, 0)
            if needed <= 0:
                continue
            fresh = [p for p in group if p not in scored_paths] or group[:1]
            chosen = fresh[:needed] + [fresh[0]] * max(0, needed - len(fresh))
            pending += [_relative(workspace, p) or p for p in chosen]
        return pending

    def score_step(arm: dict, pending: list[str], why: str, stage: str,
                   state: str | None = None, label: str | None = None) -> dict:
        """One proposal that scores every pending run of the arm in sequence."""
        commands = [["score", "--db", db, "--predictions", path, "--timeout", "60"] for path in pending]
        name = "score" if len(pending) == 1 else f"score the {len(pending)} runs"
        without = "without" if stage == "score baseline" else "with"
        return step(name, why, commands[0],
                    state=state or ("score_baseline" if stage == "score baseline" else "score_context"),
                    label=label or (f"Score the {len(pending)} "
                                    f"run{'' if len(pending) == 1 else 's'} {without} rules"),
                    stage=stage, commands=commands)

    pending = unscored(baseline)
    if pending:
        n_files = sum(len(_manifest(workspace, rid).get("outputs", {}).get("out") or [])
                      for rid in baseline["generate_runs"])
        return score_step(baseline, pending,
                          f"execute every baseline query and compare result sets "
                          f"({len(baseline['score_runs'])} of {n_files} runs scored)", "score baseline")
    # a baseline at 0 in every repetition is not a number to climb from:
    # say what to fix before any context arm is paid for
    rates = [runs[rid]["summary"]["execution_rate"] for rid in baseline["score_runs"]
             if runs[rid].get("summary")]
    context_scored = any(has_context(a) and a["score_runs"] for a in round_arms)
    if rates and not any(rates) and not context_scored:
        from .harness import load_report
        newest = max(baseline["score_runs"], key=lambda rid: runs[rid]["started_at"] or "")
        report = f"runs/{newest}/{runs[newest]['outputs']['save_report'][0]}"
        why = _zero_baseline_why(load_report(workspace / report))
        # the step used to repeat itself for ever: it proposed a triage and
        # then ignored that the triage had run, so a workspace with a zero
        # baseline never reached the rules step at all. Once the diagnosis
        # has run the loop goes on, and the warning rides along as a blocker
        # line under every later step.
        diagnosed = any(r["command"] == "triage" and r["finished"] and r["exit_code"] == 0
                        for r in index["runs"])
        if not diagnosed:
            return step("rules", why, ["triage", "--report", report, "--show"],
                        state="baseline_zero",
                        label="Diagnose the zero baseline before writing rules", human=True)
        blockers.append("the baseline scored 0 in every repetition: " + why)
    working = context["working"]
    # context a retriever chose per question: there is no rules file to
    # edit, so the arm itself is the round (newest first)
    retrieval_arms = sorted((a for a in round_arms if a.get("retrieved_sha256")),
                            key=lambda a: a["first_seen"], reverse=True)
    if not working and not retrieval_arms:
        score_run = runs[baseline["score_runs"][0]]
        report = f"runs/{score_run['run_id']}/{score_run['outputs']['save_report'][0]}"
        triaged = any(r["command"] == "triage" for r in index["runs"])
        if not triaged:
            return step("rules", "group the failures by shape first; the largest bucket is "
                        "where the first rules come from",
                        ["triage", "--report", report, "--show"],
                        state="need_triage", label="Group the failures by shape",
                        human=True, draft_offer=True)
        predictions = (_manifest(workspace, score_run["run_id"])["inputs"]
                       .get("predictions") or {}).get("path", "<predictions.csv>")
        return step("rules", "draft the rules the largest bucket asks for (one model call, "
                    "cents), gold beside prediction, each rule citing its questions; then "
                    "read the draft and install it with context-write",
                    ["draft-rules", "--report", report, "--predictions", predictions,
                     "--ids", curate_ids, "--out", "context/rules_draft.md"],
                    state="need_rules", label="Draft the rules", goes_to="#rules",
                    commands=[["draft-rules", "--report", report, "--predictions", predictions,
                               "--ids", curate_ids, "--out", "context/rules_draft.md"],
                              ["context-write", "--source-file", "context/rules_draft.md",
                               "--to", "context/rules.md",
                               # round 2 onward keeps what round 1 wrote;
                               # without --add the file is replaced and the
                               # climb only ever holds the newest bucket
                               *(["--add"] if working else [])]],
                    human=True, draft_offer=True)
    if working:
        leak = _newest_leak_status(index, workspace, working["sha256"])
        if leak["status"] == "not checked":
            return step("leak", "the context must not contain the answers of the set you score",
                        ["leak", "--context", working["path"], "--predictions", holdout_csv],
                        state="leak_unchecked", label="Check the rules for answers")
        if leak["status"] == "LEAKED":
            blockers.append("context/rules.md contains answers of the reserved questions; edit it")
            return step("leak", "context/rules.md contains answers of the reserved questions; remove them and re-check",
                        ["leak", "--context", working["path"], "--predictions", holdout_csv],
                        state="leak_failed", label="Check again after removing them",
                        blocker_code="context_leaks")
        context_arm = next((a for a in index["arms"] if a["context_sha256"] == working["sha256"]), None)
        if not context_arm:
            blocked = schema_step("context arm")
            if blocked:
                return blocked
            return step("context arm", "the version with rules: same questions, same model, the working rules file added",
                        ["generate", "--questions", curate_csv, "--schema", schema,
                         "--out", f"arms/ctx_{working['sha256'][:8]}.csv", "--reps", "3",
                         "--workers", "8", "--context", working["path"], *generate_flags(db)],
                        state="need_context_arm", label="Answer the questions with rules",
                        also=[] if retrieval_arms or not has_a_catalog_to_read()
                             else [catalog_arm()])
    else:
        context_arm = retrieval_arms[0]
        # the leak run reads the same per-question file generate did, by hash
        first = _manifest(workspace, context_arm["generate_runs"][0])
        record = first["inputs"].get("context_per_question") or {}
        retrieved_path = f"runs/{first['run_id']}/{record['copy']}" if record.get("copy") else "<context_per_question.json>"
        leak = _newest_leak_status(index, workspace, record.get("sha256"), flag="context_per_question")
        if leak["status"] == "not checked":
            return step("leak", "the retrieved context must not contain the answers of the set you score",
                        ["leak", "--context-per-question", retrieved_path, "--predictions", holdout_csv],
                        state="leak_unchecked", label="Check the retrieved context for answers")
        if leak["status"] == "LEAKED":
            blockers.append("the retrieved context contains answers of the reserved questions; "
                            "fix the source documents and retrieve again")
            return step("leak", "the retrieved context contains answers of the reserved questions; "
                        "fix the source documents, retrieve again, and re-check",
                        ["leak", "--context-per-question", retrieved_path, "--predictions", holdout_csv],
                        state="leak_failed", label="Check again after fixing the documents",
                        blocker_code="context_leaks")
    # every arm of the round is scored before anything is compared. A
    # retrieval arm built beside a rules file used to be skipped here: the
    # compare step read the rules reports and the catalog arm sat unscored,
    # with no step ever naming it
    for arm in {a["arm_id"]: a for a in [context_arm, *retrieval_arms] if a}.values():
        pending = unscored(arm)
        if pending:
            what = "the retrieved context" if arm.get("retrieved_sha256") else "rules"
            return score_step(arm, pending,
                              f"score the version with {what} the same way "
                              f"({len(arm['score_runs'])} runs scored so far)", "score context")
    # no pin step: the index pins the version without rules and the newest
    # fully scored version with rules by itself (workspace.effective_pins)
    # who accepted the golds: a verdict over a model-drafted answer key says
    # so, here as in ablate and on the verdict page, until a person re-reviews
    key_caveat = answer_key_caveat((answer_key_review(index["runs"]) or {}).get("summary"),
                                   [a["model"] for a in index["arms"]
                                    if a.get("backend") != "command" and a.get("model")])
    with_key_caveat = (lambda why: why + (f". Caveat: {key_caveat}" if key_caveat else ""))
    reading = _holdout_result(workspace, index)
    if reading and reading.get("kind") == "ablate":
        # the reserved questions have been read: the loop has an end. It used
        # to go on proposing "Write round N+1 rules" with the holdout entry
        # gone, so the reader could see the tool knew and still had no close-out
        return step("compare", with_key_caveat(
                        "the loop is closed; `contextlift bundle` writes the verdict page "
                        "and its sections; a new round is scored on the working questions only, "
                        "and a second reserved reading would be a second look"),
                    ["bundle"], state="loop_closed",
                    label=f"The reserved questions are read: {reading['rate_text']} with "
                          f"context vs {reading['without_text']} without, {reading['word']}",
                    also=[{"kind": "human", "label": f"Write round {rounds_closed + 1} rules",
                           "argvs": [], "confirm": [], "goes_to": "#rules", "runnable": True,
                           "paid": False, "touches_holdout": False,
                           "label_note": "edit context/rules.md, then run the leak check; "
                                         "the reserved reading stays at one look"}]
                         # From the second round on, the per-round page stops
                         # being enough: it says "5 fixed, 0 broke" and not
                         # whether THIS question has been broken once or every
                         # round. A verb nothing recommends is a verb nobody
                         # runs, which is the fault D62 found in `split`.
                         + ([{"kind": "machine",
                              "label": "See what each question has done across "
                                       f"all {rounds_closed} rounds",
                              "argvs": [["history", "--workspace", str(workspace),
                                         "--out", "history.csv"]],
                              "confirm": [], "goes_to": "#history", "runnable": True,
                              "paid": False, "touches_holdout": False,
                              "label_note": "free, no cloud call. A question broken in "
                                            "more than one round is a rule to fix; one "
                                            "broken once and right since is history"}]
                            if rounds_closed >= 2 else []))
    # the reserved twins have been answered but not scored yet: scoring them
    # is a step of its own, so the reading is never half done
    base_reserved, candidate_reserved = reserved_pair(index)
    for pair in (base_reserved, candidate_reserved):
        if not pair:
            continue
        pending = unscored(pair[1])
        if pending:
            return score_step(pair[0], pending,
                              "the reserved questions are answered; score every repetition, "
                              "then read both arms once with one ablate", "compare",
                              state="score_holdout",
                              label=f"Score the {len(pending)} reserved "
                                    f"run{'' if len(pending) == 1 else 's'} "
                                    f"({pair[0]['label']})")
    # the reserved questions are one-way, so they are never the primary step
    also = [training_offer(), reserved_offer(base_reserved, candidate_reserved)]
    if not retrieval_arms:
        if has_a_catalog_to_read():
            also.append(catalog_arm())
    unread = bool(key["split"] and key["split"]["holdout"]) and not index["holdout_looks"]
    return step("compare", with_key_caveat(
                    "the versions are measured; read the round on the working questions "
                    "(the two commands below), then edit context/rules.md for the next round. "
                    "The reserved questions are answered once at the end, by these same two "
                    "versions, and read with one ablate"), [],
                state="verdict_holdout_ready" if unread else "verdict_next_round",
                label=f"Write round {rounds_closed + 1} rules", goes_to="#rules",
                also=also, human=True, done=True)


# --- drafts -----------------------------------------------------------------

NUMBER = re.compile(r"\d+(?:\.\d+)?")


def fingerprint(workspace: pathlib.Path) -> str:
    """Cheap change detector: names and mtimes of run folders, plus the working
    rules file. The chat hashes it to notice work done in another terminal.
    Without the rules it would miss a file the person just saved in an editor.
    Measured on a 36-run workspace: 0.3 ms, against 15.8 ms to build the index."""
    digest = hashlib.sha256()
    for path in sorted((workspace / "runs").glob("*/manifest.json")):
        digest.update(f"{path.parent.name}:{path.stat().st_mtime_ns}".encode())
    working = (api_context(workspace).get("working") or {}).get("sha256") or ""
    digest.update(working.encode())
    return digest.hexdigest()[:16]


