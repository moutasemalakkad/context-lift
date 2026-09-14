# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Record every CLI run into a workspace directory, so a run has an identity.

Without a workspace, a command writes only the files named on its flags and
"which run is this" lives in the file names a person invents. With
`--workspace DIR`, `record_run` wraps the same command: it copies and hashes
every input file, tees stdout/stderr, runs the handler, hashes and copies
the outputs, and writes `runs/<run id>/manifest.json`. `rebuild_index`
scans those manifests into one `index.json` a viewer can read.

Lineage is derived by hash equality (a score run's predictions input hash
equals some generate run's output hash), never declared on the command
line. Exports: record_run, rebuild_index, read_pins, effective_pins,
fully_scored, has_context, input_paths, new_run_id, sha256_of_file, arm_id,
answer_key_review, workspace_error. Pure stdlib.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import hashlib
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import threading
import traceback
from collections import Counter

from . import __version__

SCHEMA_VERSION = 1

# Flags whose value is a path the command WRITES. Everything else that names
# an existing file is an input. The `_repK` expansion for `generate --reps`
# is handled in _expand_reps. `raw_out` is not a flag: draft-rules sets it to
# the model's reply it saved beside the draft, so the manifest carries it.
OUTPUT_FLAGS = (
    "out", "save_report", "clean_out", "flagged_out", "out_curate",
    "out_holdout", "out_curate_csv", "out_holdout_csv", "context_out",
    "routes_out", "routing_out",
    "candidates_out", "to", "previous_out", "diff_out", "version_out",
    "manifest_out", "checkpoint", "pages", "raw_out",
)

# Flags whose value is a DIRECTORY of inputs: every file in it is hashed and
# copied, so a push links to the draft-context or catalog-pull run that
# wrote the pages, and catalog-route to the pages it routed over.
DIRECTORY_INPUT_FLAGS = ("docs",)

# Under a workspace, an output the user did not name lands in the run dir.
# `required=True` flags (generate --out, curate-examples --out) are not
# listed: argparse already forces a value.
DEFAULT_OUTPUTS = {
    "score": {"save_report": "report.json", "checkpoint": "checkpoint.json"},
    "gate": {"save_report": "report.json", "clean_out": "golds_clean.csv",
             "flagged_out": "golds_flagged.csv"},
    "split": {"out_curate": "curate_ids.txt", "out_holdout": "holdout_ids.txt",
              "out_curate_csv": "curate.csv", "out_holdout_csv": "holdout.csv"},
    "ablate": {"save_report": "report.json"},
    "attribute": {"save_report": "report.json"},
    "climb": {"save_report": "report.json"},
    "triage": {"save_report": "report.json"},
    "import": {"out": "predictions.csv"},
    "context-write": {"previous_out": "previous.md", "diff_out": "diff.patch",
                      "version_out": "version.json"},
    "profile": {"out": "values.md"},
    "catalog-list": {"out": "catalog.txt"},
    "catalog-route": {"routes_out": "routes.json", "context_out": "context_per_question.json",
                      "routing_out": "routing.csv"},
    "catalog-push": {"manifest_out": "push_manifest.json", "previous_out": "previous"},
    "schema": {"out": "schema.txt"},
    # one flag, two outputs: `log --demand` writes a different file
    "log": {"out": lambda args: "demand.csv" if getattr(args, "demand", False)
            else "query_log.csv"},
}

# The database is hashed for identity but never copied (a warehouse dump can
# be gigabytes). A `bq://` value is not a file: the spec string is the
# identity and nothing is hashed. The gold cache and the checkpoint are
# working files a run reads and writes as it goes: hashed, never copied.
HASH_ONLY_FLAGS = ("db", "gold_cache", "checkpoint")

# Commands that execute the answer key. Under a workspace their unset
# --gold-cache is one shared file: a workspace has one database, and the
# file records which, so the "starts empty on a mismatch" rule still holds.
GOLD_CACHE_COMMANDS = ("score", "gate", "ablate", "climb", "triage", "attribute")

# argparse bookkeeping, not facts about the run.
SKIP_OPTION_KEYS = ("func", "command", "workspace")

# Commands that only read the workspace; recording them would be noise.
NOT_RECORDED = ("index", "next", "pin", "bundle", "chat")

# Commands whose predictions define an arm (a set of runs that differ only
# by context and examples).
ARM_COMMANDS = ("generate", "import")


def sha256_of_file(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def arm_id(context_sha256: str | None, examples_sha256: str | None,
           retrieved_sha256: str | None = None,
           runner_version: str | None = None) -> str:
    """Twelve hex chars naming what the model saw. None means the arm has no
    such input, so every baseline shares one id.

    `retrieved_sha256` is a hash of the context retrieved per question, when
    there was any. An arm is what the model saw, so two different retrievals
    must not collide as one arm - ablate would then compare them and report a
    null result that is an artifact of the naming.

    `runner_version` is the caller's own label for the agent under test, from
    `--runner-version`. It belongs here for the same reason: the runner IS
    part of what the model saw. Under `--backend command` the context never
    reaches the framework at all, so the context files are the ONLY thing
    naming an arm, and two runs of different agent versions merged into one.
    One estate had four no-context runs collapse into a single arm carrying
    whichever version created it; the other version was not recoverable from
    the index, and pinning could not tell the two apart.

    The key keeps its old shape when a part is absent, so every arm id
    already written into a manifest stays that same string. Only a run that
    passed `--runner-version` moves, which is exactly the broken case.
    """
    key = f"{context_sha256 or ''}:{examples_sha256 or ''}"
    if retrieved_sha256:
        key = f"{key}:{retrieved_sha256}"
    if runner_version:
        key = f"{key}:runner={runner_version}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def new_run_id(runs_dir: pathlib.Path, command: str,
               now: dt.datetime | None = None) -> str:
    """`YYYYMMDD-HHMMSS-<command>` in UTC, so `ls runs/` is chronological.
    A second run of the same command in the same second gets `-2`, `-3`."""
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{command}"
    candidate, n = base, 1
    while (runs_dir / candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _file_record(path: str, copy_dir: pathlib.Path | None,
                 used_names: set[str]) -> dict:
    """Hash one file and, unless copy_dir is None, copy it next to the
    manifest. Two inputs with the same basename get a numeric suffix."""
    source = pathlib.Path(path)
    record = {"path": path, "sha256": None, "bytes": None, "copy": None}
    if not source.is_file():
        return record
    record["sha256"] = sha256_of_file(source)
    record["bytes"] = source.stat().st_size
    if copy_dir is None:
        return record
    name = source.name
    stem, suffix = source.stem, source.suffix
    n = 1
    while name in used_names:
        n += 1
        name = f"{stem}_{n}{suffix}"
    used_names.add(name)
    copy_dir.mkdir(parents=True, exist_ok=True)
    target = copy_dir / name
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    record["copy"] = str(target.relative_to(copy_dir.parent))
    return record


def input_paths(manifest: dict, flag: str) -> str | None:
    """The path(s) a manifest recorded for one input flag, joined by commas:
    a repeated flag (`mine --queries A --queries B`) is recorded as a list,
    a single one as a dict."""
    record = manifest.get("inputs", {}).get(flag)
    records = record if isinstance(record, list) else [record or {}]
    return ", ".join(r.get("path") for r in records if r.get("path")) or None


def _paths_in(value) -> list[str]:
    """The file paths a flag value may name: a string, a list of strings, or
    climb's `LABEL=PATH` form."""
    if isinstance(value, str):
        candidate = value.partition("=")[2] if "=" in value else value
        return [candidate] if pathlib.Path(candidate).is_file() else []
    if isinstance(value, list):
        return [p for item in value for p in _paths_in(item)]
    return []


def _expand_reps(path: str) -> list[str]:
    """`generate --reps N` writes `<stem>_rep1.csv`.. and not `--out` itself;
    `draft-context --out DIR` writes a directory, recorded file by file."""
    if pathlib.Path(path).is_dir():
        return [str(f) for f in sorted(pathlib.Path(path).iterdir()) if f.is_file()]
    stem = str(pathlib.Path(path).with_suffix(""))
    suffix = pathlib.Path(path).suffix
    found = []
    if pathlib.Path(path).exists():
        found.append(path)
    else:
        k = 1
        while pathlib.Path(f"{stem}_rep{k}{suffix}").exists():
            found.append(f"{stem}_rep{k}{suffix}")
            k += 1
    # a command runner's routed tables ride beside each predictions file
    for name in list(found):
        sidecar = str(pathlib.Path(name).with_suffix("")) + "_routing.csv"
        if pathlib.Path(sidecar).exists():
            found.append(sidecar)
    return found


class _Tee:
    """Write to the terminal and to a file at the same time."""

    def __init__(self, terminal, handle):
        self.terminal, self.handle = terminal, handle

    def write(self, text: str) -> int:
        self.handle.write(text)
        return self.terminal.write(text)

    def flush(self) -> None:
        self.handle.flush()
        self.terminal.flush()

    def isatty(self) -> bool:
        return False


def _now_iso() -> str:
    # milliseconds: two runs in one second must still sort by start time
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def write_manifest(run_dir: pathlib.Path, manifest: dict) -> None:
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))


# What a workspace leaves out of the user's own git repo: run folders and
# prediction files grow with every run, a database dump is binary, the index
# is rebuilt from the manifests. The rules file, the answer key, drafts and
# the thread stay in, so the loop's history travels with the repo. The gold
# cache is rows fetched from the database: rebuilt by the next run, not
# history.
# `contextlift.json` was added 2026-09-12, found by a test asserting this list
# and the exposure warning agree. It carries the workspace directory's
# NAME, which is a customer's name often enough to matter, and it was
# warned about while being left out of here -- so a FRESH workspace did not
# ignore it either. Safe to ignore: `_ensure_workspace` rewrites it when
# missing, and `share` is the supported way to hand a run to a colleague,
# not committing the workspace.
WORKSPACE_GITIGNORE = ("runs/\narms/\ncache/\n*.sqlite\n*.db\n"
                       ".chat_history\nindex.json\ncontextlift.json\n")


def _ensure_workspace(workspace: pathlib.Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    marker = workspace / "contextlift.json"
    if not marker.exists():
        marker.write_text(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "name": workspace.resolve().name,
            "created_at": _now_iso(),
        }, indent=1))
    # written once; a person may edit it afterwards and the edit is kept
    gitignore = workspace / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(WORKSPACE_GITIGNORE)


# `*` ignores everything in the directory holding this file, including the
# file itself. The pattern uv and pip already use for `.venv`, borrowed
# because it is self-protecting: nobody has to remember to add a rule.
_RUNS_GITIGNORE = """\
# Written by contextlift. Everything under runs/ is the customer's: their
# questions, their SQL, their table names, and a model's answers about their
# data. `contextlift --workspace .` is a documented way to run, so this directory
# can appear inside somebody's own repository, and a run recorded there would
# otherwise be one `git add -A` from being published.
*
"""


def protect_runs_from_git(runs_dir: pathlib.Path) -> bool:
    """Put a `.gitignore` in runs/ the first time. True if it wrote one.

    Only inside `runs/`, which this tool owns. NOT at the workspace root: the
    root can be a directory the user already had -- `--workspace .` is in the
    demo's own closing lines -- and writing `*` there would ignore their
    entire project.

    Never overwritten, so a person who edits it keeps their edit.
    """
    marker = runs_dir / ".gitignore"
    if marker.exists():
        return False
    try:
        marker.write_text(_RUNS_GITIGNORE)
        return True
    except OSError:
        # A read-only or exotic filesystem. This is a safety net, not a gate:
        # failing the run over it would be worse than the exposure it guards.
        return False


# Files this tool writes at the workspace ROOT, with what is in them. Checked
# as a list rather than by name, so a file added later is covered by default
# instead of by remembering.
_ROOT_FILES = {
    "index.json": "rebuilt from every runs/*/manifest.json, and a manifest "
                  "holds the full command line: project ids, dataset names, "
                  "file paths",
    "contextlift.json": "carries the workspace directory's NAME, which is a "
                     "customer's name often enough to matter",
}


def git_exposure_warning(workspace: pathlib.Path) -> list[str]:
    """Warn about root files git would commit. Empty when there are none.

    `runs/` protects itself with the `.gitignore` above. These cannot: they
    sit at the workspace root, and the root may be the user's own repository,
    where this tool has no business writing ignore rules. It names the lines
    to add instead.

    WHY THIS IS NEEDED AT ALL, given `_ensure_workspace` already writes a
    workspace `.gitignore` listing every one of these. That write is guarded
    by `if not gitignore.exists()`, and a real project already HAS a
    `.gitignore`. So the protection fails open in exactly the case the demo's
    own closing lines recommend:

        contextlift next --workspace .

    Measured 2026-09-12 in a fresh repository carrying an ordinary
    `__pycache__/` gitignore: `git status` listed `runs/`, `index.json` and
    `contextlift.json` as untracked, one `git add -A` from being published.

    Silent when git is absent, when the workspace is not in a work tree, or
    when everything is already ignored. A warning that fires every run on a
    correctly configured machine is one people learn to scroll past.
    """
    try:
        inside = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return []
        exposed = []
        for name in sorted(_ROOT_FILES):
            checked = subprocess.run(
                ["git", "-C", str(workspace), "check-ignore", "-q", name],
                capture_output=True, text=True, timeout=10)
            if checked.returncode != 0:      # 0 means ignored
                exposed.append(name)
    except (OSError, subprocess.SubprocessError):
        return []   # no git, or it misbehaved; this is advice, not a gate
    if not exposed:
        return []

    out = ["warning: this workspace is inside a git repository, and these are "
           "not ignored:"]
    for name in exposed:
        out.append(f"  {name} -- {_ROOT_FILES[name]}")
    out += [
        "",
        "  runs/ ignores itself. These cannot, because the workspace root may "
        "be your",
        "  repository and not this tool's to write rules in. Add to "
        ".gitignore:",
        "",
        *(f"    {name}" for name in exposed),
        "",
    ]
    return out


def workspace_error(workspace: pathlib.Path) -> str | None:
    """One line when the workspace directory cannot be made, else None.

    A mistyped `--workspace /private/tvfy/e2e/WS` used to raise
    PermissionError out of pathlib and print 40 lines of traceback before
    the command had done anything.
    """
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"cannot create workspace {workspace}: {(exc.strerror or str(exc)).lower()}"
    return None


def missing_file_message(exc: FileNotFoundError) -> str:
    """One line for a file that is not there: the path and, for a relative
    path, the usual cause. `next` prints paths joined to the workspace, but
    a path typed by hand still resolves from the shell's directory, and the
    traceback that used to follow said nothing this line does not.

    An absolute path gets no hint: it sent a reader looking for a working
    directory problem when the real cause was a directory that did not
    exist yet.
    """
    path = exc.filename or str(exc)
    if pathlib.Path(path).is_absolute():
        return f"contextlift: {path}: no such file"
    return (f"contextlift: {path}: no such file (relative paths resolve from the "
            "current directory, not from --workspace)")


def record_run(args: argparse.Namespace, argv: list[str]) -> int:
    """Run `args.func(args)` inside a recorded run directory.

    Returns the command's exit code. A SystemExit carrying a message is
    printed to stderr and becomes exit 1, the same as an unrecorded run
    would show the shell; a missing input file is one line and exit 2. Any
    other exception is written to stderr.txt with its traceback, the
    manifest records it, and it is re-raised.
    """
    workspace = pathlib.Path(args.workspace)
    unusable = workspace_error(workspace)
    if unusable:
        print(unusable, file=sys.stderr)
        return 2
    if args.command in GOLD_CACHE_COMMANDS and getattr(args, "gold_cache", None) is None:
        # set before the estimate too, so an estimate can say how many
        # answers the cache already holds
        args.gold_cache = str(workspace / "cache" / "golds.json")
    # an estimate makes no call and writes nothing: not a run, not an arm
    if args.command in NOT_RECORDED or getattr(args, "estimate_only", False) \
            or getattr(args, "dry_run", False):
        return args.func(args)
    _ensure_workspace(workspace)
    if args.command in GOLD_CACHE_COMMANDS:
        (workspace / "cache").mkdir(exist_ok=True)
    runs_dir = workspace / "runs"
    runs_dir.mkdir(exist_ok=True)
    protect_runs_from_git(runs_dir)
    for line in git_exposure_warning(workspace):
        print(line, file=sys.stderr)
    run_id = new_run_id(runs_dir, args.command)
    run_dir = runs_dir / run_id
    (run_dir / "outputs").mkdir(parents=True)

    for flag, name in DEFAULT_OUTPUTS.get(args.command, {}).items():
        if getattr(args, flag, None) is None:
            file_name = name(args) if callable(name) else name
            setattr(args, flag, str(run_dir / "outputs" / file_name))

    used_names: set[str] = set()
    inputs: dict = {}
    options: dict = {}
    for key, value in vars(args).items():
        if key in SKIP_OPTION_KEYS or key in OUTPUT_FLAGS:
            continue
        if key in HASH_ONLY_FLAGS and isinstance(value, str):
            if value.startswith("bq://"):
                inputs[key] = {"path": value, "sha256": None, "kind": "bigquery"}
            elif key == "db" or pathlib.Path(value).is_file():
                # a gold cache that does not exist yet is not an input;
                # the database is recorded even when the path is wrong
                inputs[key] = _file_record(value, None, used_names)
            continue
        if key in DIRECTORY_INPUT_FLAGS and isinstance(value, str) and pathlib.Path(value).is_dir():
            files = [str(f) for f in sorted(pathlib.Path(value).iterdir()) if f.is_file()]
            inputs[key] = [_file_record(f, run_dir / "inputs", used_names) for f in files]
            options[key] = value
            continue
        paths = _paths_in(value)
        if paths:
            records = [_file_record(p, run_dir / "inputs", used_names)
                       for p in paths]
            inputs[key] = records if isinstance(value, list) else records[0]
        else:
            options[key] = value

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "command": args.command,
        "argv": list(argv),
        "cwd": os.getcwd(),
        "contextlift_version": __version__,
        "python": platform.python_version(),
        "started_at": _now_iso(),
        "ended_at": None,
        "duration_seconds": None,
        "exit_code": None,
        "options": options,
        "inputs": inputs,
        "outputs": {},
    }
    write_manifest(run_dir, manifest)
    started = dt.datetime.now()

    code: int = 1
    error: BaseException | None = None
    with open(run_dir / "stdout.txt", "w") as out_handle, \
            open(run_dir / "stderr.txt", "w") as err_handle, \
            contextlib.redirect_stdout(_Tee(sys.stdout, out_handle)), \
            contextlib.redirect_stderr(_Tee(sys.stderr, err_handle)):
        try:
            code = int(args.func(args) or 0)
        except SystemExit as exc:
            if isinstance(exc.code, int) or exc.code is None:
                code = int(exc.code or 0)
            else:
                print(exc.code, file=sys.stderr)
                code = 1
        except FileNotFoundError as exc:
            print(missing_file_message(exc), file=sys.stderr)
            code = 2
        except Exception as exc:  # recorded, then re-raised below
            traceback.print_exc(file=sys.stderr)
            error = exc

    outputs: dict = {}
    for flag in OUTPUT_FLAGS:
        value = getattr(args, flag, None)
        if not isinstance(value, str):
            continue
        copy_dir = None if flag in HASH_ONLY_FLAGS else run_dir / "outputs"
        records = [_file_record(p, copy_dir, used_names) for p in _expand_reps(value)]
        if records:
            outputs[flag] = records

    manifest["outputs"] = outputs
    manifest["ended_at"] = _now_iso()
    manifest["duration_seconds"] = round(
        (dt.datetime.now() - started).total_seconds(), 1)
    manifest["exit_code"] = code if error is None else 1
    if args.command in ARM_COMMANDS:
        context = inputs.get("context") or {}
        examples = inputs.get("examples") or {}
        # what the model actually saw, when a retriever chose it per question;
        # the command computes it because only it knows what came back
        manifest["arm"] = _arm_record(context, examples,
                                      getattr(args, "retrieved_sha256", None),
                                      getattr(args, "runner_version", None))
    telemetry = getattr(args, "telemetry", None)
    if telemetry is not None:
        manifest["telemetry"] = telemetry
    write_manifest(run_dir, manifest)
    rebuild_index(workspace)
    print(f"recorded -> {run_dir}", file=sys.stderr)
    if error is not None:
        raise error
    return code


def _arm_record(context: dict, examples: dict,
                retrieved_sha256: str | None = None,
                runner_version: str | None = None) -> dict:
    context_sha = context.get("sha256")
    examples_sha = examples.get("sha256")
    parts = [pathlib.Path(r["path"]).name
             for r in (context, examples) if r.get("path")]
    if retrieved_sha256:
        # named by what came back, not by the retriever's file: the label
        # must change when the retrieval does
        parts.append(f"retrieved {retrieved_sha256[:12]}")
    label = " + ".join(parts) or "no context"
    if runner_version:
        # the person reading the verdict needs to see which agent answered,
        # not only that two arms differ
        label = f"{label} ({runner_version})"
    return {
        "arm_id": arm_id(context_sha, examples_sha, retrieved_sha256, runner_version),
        "context_sha256": context_sha,
        "examples_sha256": examples_sha,
        "retrieved_sha256": retrieved_sha256,
        "runner_version": runner_version,
        "label": label,
    }


def read_pins(workspace: str | os.PathLike) -> dict:
    """The explicit pins in pins.json, or None per slot. `contextlift pin`
    writes these; the index fills the empty slots itself (effective_pins)."""
    path = pathlib.Path(workspace) / "pins.json"
    pins = {"baseline": None, "candidate": None}
    if path.exists():
        pins.update(json.loads(path.read_text()))
    return pins


def _primary_set(sets: list[dict]) -> dict:
    """The question set an arm's top-level fields describe: the working set,
    never the holdout while a working set exists, and the most scored one
    when several working sets exist."""
    working = [group for group in sets if not group["on_holdout"]]
    return min(working or sets,
               key=lambda group: (-len(group["score_runs"]), group["generate_runs"][0]))


def fully_scored(arm: dict) -> bool:
    """Every prediction file the arm produced has a score run. An arm scored
    once out of three repetitions is not measured yet."""
    return bool(arm["score_runs"]) and len(arm["score_runs"]) >= len(arm["predictions"])


def reserved_set(arm: dict) -> dict | None:
    """The arm's runs over the reserved questions, or None. An arm keeps one
    group of runs per question set, and the reserved group is the arm answered
    over holdout.csv at the end of the loop."""
    for group in arm.get("sets") or []:
        if group["on_holdout"]:
            return group
    return None


def reserved_pair(index: dict) -> tuple:
    """The two versions answered over the reserved questions, each as (arm,
    its reserved runs) or None: the pinned baseline's twin, and the newest
    twin with context. A rules twin is the candidate arm answered a second
    time, so it is the same arm; a retrieval twin retrieves again, so it is an
    arm of its own."""
    arms = {a["arm_id"]: a for a in index["arms"]}
    baseline = arms.get(index["pins"].get("baseline") or "")
    baseline_set = reserved_set(baseline) if baseline else None
    with_context = [(arm, reserved_set(arm)) for arm in index["arms"]
                    if has_context(arm) and reserved_set(arm)]
    newest = max(with_context, key=lambda pair: pair[0]["first_seen"], default=None)
    return ((baseline, baseline_set) if baseline_set else None), newest


def only_holdout(arm: dict) -> bool:
    """Every run of this arm answered the reserved questions. That is the
    end-of-loop twin of a round arm, not a round of its own: it is never a
    pin, because comparing it with an arm answered over the working questions
    compares two different question sets."""
    groups = arm.get("sets") or []
    return bool(groups) and all(group["on_holdout"] for group in groups)


def has_context(arm: dict) -> bool:
    """Did the model see anything beyond the schema: a rules file, a worked
    example bank, or context a retriever chose per question. The one test
    every "with context" decision uses; a retrieval arm used to fail it
    because only the rules hash was checked, and was then taken as a
    baseline."""
    return bool(arm.get("context_sha256") or arm.get("examples_sha256")
                or arm.get("retrieved_sha256"))


def effective_pins(explicit: dict, arms: list[dict]) -> dict:
    """The pins a comparison uses: the explicit ones when pins.json names
    them, else picked from the arms. Baseline: the oldest arm with no
    context that has a score run. Candidate: the newest fully scored arm
    with context. So the loop never has a "pin" step; `contextlift pin` is an
    override for comparing two rules versions with each other. An arm
    answered over the reserved questions only is left out of both: it is a
    twin read at the end, not a round."""
    pins = {"baseline": explicit.get("baseline"), "candidate": explicit.get("candidate"),
            "explicit": {"baseline": bool(explicit.get("baseline")),
                         "candidate": bool(explicit.get("candidate"))}}
    arms = [a for a in arms if not only_holdout(a)]
    if not pins["baseline"]:
        no_context = [a for a in arms if not has_context(a) and a["score_runs"]]
        if no_context:
            pins["baseline"] = min(no_context, key=lambda a: a["first_seen"])["arm_id"]
    if not pins["candidate"]:
        measured = [a for a in arms if has_context(a) and fully_scored(a)]
        if measured:
            pins["candidate"] = max(measured, key=lambda a: a["first_seen"])["arm_id"]
    return pins


def write_pins(workspace: str | os.PathLike, pins: dict) -> None:
    (pathlib.Path(workspace) / "pins.json").write_text(json.dumps(pins, indent=1))


def _records(value) -> list[dict]:
    return value if isinstance(value, list) else [value]


def run_that_wrote(workspace: str | os.PathLike, path: str | os.PathLike
                   ) -> tuple[pathlib.Path, dict] | None:
    """The run folder and manifest whose outputs include this file.

    Matched by hash, not by path: the file may have been renamed or copied out
    of the workspace since. When several runs wrote the same bytes the newest
    wins, the same rule `rebuild_index` uses for lineage.
    """
    workspace = pathlib.Path(workspace)
    try:
        sha = sha256_of_file(path)
    except OSError:
        return None
    found = None
    for manifest_path in sorted(workspace.glob("runs/*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            continue
        for records in manifest.get("outputs", {}).values():
            if any(r.get("sha256") == sha for r in _records(records)):
                found = (manifest_path.parent, manifest)
    return found


# The flags whose files are the arms of a reading: saved reports, or the
# predictions ablate scores itself.
READING_ARM_FLAGS = ("without_report", "with_report", "round",
                     "without_context", "with_context")


def _ids_in_lines(path: pathlib.Path) -> set[str]:
    """One id per line, as `split --out-holdout` writes it."""
    try:
        text = path.read_text()
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines()
            if line.strip() and not line.startswith("#")}


def _question_ids(path: pathlib.Path) -> set[str]:
    """The question ids one file names: a saved score report's results, or the
    `id` column of a questions/predictions CSV. Repetition suffixes are
    stripped, so the ids compare against the list `split` wrote."""
    try:
        with open(path) as handle:
            head = handle.read(2048)
    except OSError:
        return set()
    if head.lstrip().startswith("{"):
        try:
            results = json.loads(path.read_text()).get("results") or []
        except (OSError, ValueError):
            return set()
        return {str(row.get("id", "")).split("#rep")[0] for row in results}
    try:
        with open(path, newline="") as handle:
            return {row["id"] for row in csv.DictReader(handle) if row.get("id")}
    except (OSError, ValueError):
        return set()


def _answered_only_reserved(run_dir: pathlib.Path, manifest: dict,
                            reserved: set[str]) -> bool:
    """Every question this arm answered is a reserved one, so the file it read
    is the holdout whatever it is named.

    Not "any reserved id": arms may also be answered over the whole key and
    read with `--ids`, and the whole key is not the reserved set.
    """
    if not reserved:
        return False
    record = manifest.get("inputs", {}).get("questions")
    if not (isinstance(record, dict) and record.get("copy")):
        return False
    answered = _question_ids(run_dir / record["copy"])
    return bool(answered) and answered <= reserved


def _read_reserved(run_dir: pathlib.Path, manifest: dict, reserved: set[str]) -> bool:
    """Did this ablate or climb compare any reserved question?

    `--ids` decides when it is given: the reading is the arms' questions cut
    to that list. Without it the arms themselves say, so two arms answered
    over the reserved rows and compared with a plain `ablate` are the reading
    too - which is how the reserved half is unsealed.
    """
    if not reserved:
        return False
    inputs = manifest.get("inputs", {})
    record = inputs.get("ids")
    if isinstance(record, dict) and record.get("copy"):
        return bool(_ids_in_lines(run_dir / record["copy"]) & reserved)
    for flag in READING_ARM_FLAGS:
        for entry in _records(inputs.get(flag) or []):
            if entry.get("copy") and _question_ids(run_dir / entry["copy"]) & reserved:
                return True
    return False


def _score_summary(run_dir: pathlib.Path, manifest: dict) -> dict | None:
    """The library's own rate for a score run, read from its saved report."""
    report = manifest.get("outputs", {}).get("save_report")
    if not report or not report[0].get("copy"):
        return None
    from .harness import load_report  # local: keeps this module import-light
    try:
        loaded = load_report(run_dir / report[0]["copy"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {
        "n_scored": loaded.n_scored,
        "execution_rate": loaded.execution.point,
        "adjusted_rate": loaded.adjusted.point,
        "judged": any(r.judged for r in loaded.results),
    }


def _review_summary(manifest: dict) -> dict | None:
    """Who accepted each gold, for a review run that wrote golds (a sheet
    in, a golds file out): the counts the finishing command recorded in its
    telemetry. None for a prepare or a draft run."""
    telemetry = manifest.get("telemetry") or {}
    if "model_reviewed" not in telemetry:
        return None
    if not manifest.get("inputs", {}).get("sheet") or not manifest.get("outputs", {}).get("out"):
        return None
    return {key: telemetry.get(key)
            for key in ("model_reviewed", "person_reviewed", "reviewer_model")}


def answer_key_review(runs: list[dict]) -> dict | None:
    """The newest finished review run that wrote golds, as its index entry;
    its `summary` says who accepted each gold. None when no review finished
    here (golds provided by hand carry no such record)."""
    finished = [r for r in runs if r["command"] == "review" and r["finished"]
                and r["exit_code"] == 0 and r.get("summary")]
    return max(finished, key=lambda r: r["started_at"] or "", default=None)


def rebuild_index(workspace: str | os.PathLike) -> dict:
    """Scan every runs/*/manifest.json into index.json.

    Rebuilt from scratch each time rather than appended: run folders get
    deleted, copied in from another machine, or left half-written by a
    killed process, and the manifests are the truth. Written atomically so two
    concurrent commands cannot leave a torn file.
    """
    workspace = pathlib.Path(workspace)
    runs_dir = workspace / "runs"
    manifests: list[tuple[pathlib.Path, dict]] = []
    for manifest_path in sorted(runs_dir.glob("*/manifest.json")):
        try:
            manifests.append((manifest_path.parent,
                              json.loads(manifest_path.read_text())))
        except (OSError, ValueError):
            continue

    # output hash -> the runs that wrote it, oldest first; the basis of every
    # lineage link. Byte-identical outputs (temperature-0 repetitions) share
    # a hash, so a consumer links to the newest producer that started
    # before it did.
    producers: dict[str, list[tuple[str, str]]] = {}
    for _, manifest in manifests:
        for records in manifest.get("outputs", {}).values():
            for record in _records(records):
                if record.get("sha256"):
                    producers.setdefault(record["sha256"], []).append(
                        (manifest.get("started_at") or "", manifest["run_id"]))
    for entries in producers.values():
        entries.sort()

    def producer_for(sha: str, consumer: dict) -> str | None:
        candidates = [rid for started, rid in producers.get(sha, [])
                      if rid != consumer["run_id"]]
        before = [rid for started, rid in producers.get(sha, [])
                  if rid != consumer["run_id"]
                  and started <= (consumer.get("started_at") or "")]
        return (before or candidates or [None])[-1]

    holdout_hashes = {
        record["sha256"]
        for _, m in manifests if m["command"] == "split"
        for record in _records(m.get("outputs", {}).get("out_holdout_csv", []))
        if record.get("sha256")
    }
    # The reserved question ids themselves, from both halves of what split
    # wrote. A run is a look at the reserved half when the ids it read are
    # in here, whatever the file was named or how it was copied - a hash
    # match on the ids file missed a plain `ablate` over two arms answered
    # over holdout.csv, which is the reading, not a training one.
    reserved_ids: set[str] = set()
    for run_dir, m in manifests:
        if m["command"] != "split":
            continue
        for record in _records(m.get("outputs", {}).get("out_holdout", [])):
            if record.get("copy"):
                reserved_ids |= _ids_in_lines(run_dir / record["copy"])
        for record in _records(m.get("outputs", {}).get("out_holdout_csv", [])):
            if record.get("copy"):
                reserved_ids |= _question_ids(run_dir / record["copy"])

    by_id: dict[str, dict] = {}
    runs: list[dict] = []
    db_hashes: Counter = Counter()
    for run_dir, manifest in manifests:
        inputs = manifest.get("inputs", {})
        produced_by = {}
        input_hashes = {}
        for flag, records in inputs.items():
            shas = [r.get("sha256") for r in _records(records)]
            input_hashes[flag] = shas if isinstance(records, list) else shas[0]
            for sha in shas:
                # a run never produces its own input (gate with nothing
                # flagged, import's byte copy): producer_for skips itself
                source = producer_for(sha, manifest) if sha else None
                if source:
                    produced_by[flag] = source
        db_record = inputs.get("db") or {}
        # identity: the file hash, or the bq:// spec (no file to hash)
        db_identity = db_record.get("sha256") or (
            db_record.get("path") if db_record.get("kind") == "bigquery" else None)
        if db_identity:
            db_hashes[db_identity] += 1
        entry = {
            "run_id": manifest["run_id"],
            "command": manifest["command"],
            "started_at": manifest.get("started_at"),
            "duration_seconds": manifest.get("duration_seconds"),
            "exit_code": manifest.get("exit_code"),
            "finished": manifest.get("ended_at") is not None,
            "arm_id": (manifest.get("arm") or {}).get("arm_id"),
            # on the run as well as the arm: an arm that merged two runner
            # versions before this was recorded cannot otherwise be split
            "runner_version": (manifest.get("options") or {}).get("runner_version"),
            "questions_sha256": input_hashes.get("questions"),
            "on_holdout": None,
            "produced_by": produced_by,
            "inputs": input_hashes,
            "outputs": {flag: [r.get("copy") for r in _records(records)]
                        for flag, records in manifest.get("outputs", {}).items()},
            "summary": (_score_summary(run_dir, manifest) if manifest["command"] == "score"
                        else _review_summary(manifest) if manifest["command"] == "review"
                        else None),
        }
        if manifest["command"] in ARM_COMMANDS:
            entry["on_holdout"] = (entry["questions_sha256"] in holdout_hashes
                                   or _answered_only_reserved(run_dir, manifest, reserved_ids))
        elif manifest["command"] in ("ablate", "climb"):
            entry["on_holdout"] = _read_reserved(run_dir, manifest, reserved_ids)
        by_id[entry["run_id"]] = entry
        runs.append(entry)

    # A run inherits arm and holdout status from the run that produced its
    # predictions (score, triage) - by hash, so renamed files still link.
    for entry in runs:
        parent_id = entry["produced_by"].get("predictions")
        parent = by_id.get(parent_id)
        if parent and entry["arm_id"] is None:
            entry["arm_id"] = parent["arm_id"]
            entry["questions_sha256"] = parent["questions_sha256"]
            entry["on_holdout"] = parent["on_holdout"]

    arms: dict[str, dict] = {}
    # (arm, questions) -> its runs. The same rules answered on a different
    # question set (the holdout) is the same arm but not the same measurement;
    # pooling 6 questions with 18 raises in ablate.pool, so they never meet.
    question_sets: dict[tuple[str, str | None], dict] = {}
    for run_dir, manifest in manifests:
        if manifest["command"] not in ARM_COMMANDS or "arm" not in manifest:
            continue
        # a run that failed wrote no predictions; registering it as an arm gives
        # the loop a baseline with nothing in it, and api_next then indexes an
        # empty score_runs list
        if manifest.get("exit_code") not in (0, None):
            continue
        arm = manifest["arm"]
        run = by_id[manifest["run_id"]]
        options = manifest.get("options", {})
        entry = arms.setdefault(arm["arm_id"], {
            "arm_id": arm["arm_id"],
            "label": arm["label"],
            "context_sha256": arm["context_sha256"],
            "examples_sha256": arm["examples_sha256"],
            # older manifests were written before retrieval existed
            "retrieved_sha256": arm.get("retrieved_sha256"),
            "first_seen": manifest["run_id"],
            "generate_runs": [],
            "predictions": [],
            "score_runs": [],
            "questions_sha256": run["questions_sha256"],
            "sets": [],
            "model": options.get("model"),
            # who answered: gemini, echo, or the person's own agent run through
            # `--backend command`, whose version is what the verdict must name
            "backend": options.get("backend"),
            "runner_version": options.get("runner_version"),
            # None, not 0.0: under --backend command the runner's spend never
            # reaches the framework, and a reader has to be able to tell
            # "this was free" from "nothing measured it"
            "cost_usd": None,
        })
        key = (arm["arm_id"], run["questions_sha256"])
        group = question_sets.get(key)
        if group is None:
            group = {"questions_sha256": run["questions_sha256"],
                     "on_holdout": bool(run["on_holdout"]),
                     "generate_runs": [], "predictions": [], "score_runs": []}
            question_sets[key] = group
            entry["sets"].append(group)
        group["generate_runs"].append(manifest["run_id"])
        for records in manifest.get("outputs", {}).values():
            group["predictions"] += [r["sha256"] for r in _records(records)
                                     if r.get("sha256")]
        cost = (manifest.get("telemetry") or {}).get("estimated_cost_usd")
        if cost:
            entry["cost_usd"] = round((entry["cost_usd"] or 0.0) + cost, 4)
    for entry in runs:
        if entry["command"] == "score" and entry["arm_id"] in arms:
            group = question_sets.get((entry["arm_id"], entry["questions_sha256"]))
            if group is not None:
                group["score_runs"].append(entry["run_id"])
    for entry in arms.values():
        primary = _primary_set(entry["sets"])
        entry["questions_sha256"] = primary["questions_sha256"]
        entry["generate_runs"] = primary["generate_runs"]
        entry["predictions"] = primary["predictions"]
        entry["score_runs"] = primary["score_runs"]

    # A look is one decision, not one repetition. A reading is one
    # `ablate`/`climb` over the reserved questions, both arms included; a
    # refused run (exit 2) read nothing and is not one. The scoring that fed
    # a reading is part of that same look, so an arm answered over
    # holdout.csv, scored three times and then compared is one look and not
    # four.
    readings = [entry for entry in runs
                if entry["command"] in ("ablate", "climb") and entry["on_holdout"]
                and entry["finished"] and entry["exit_code"] == 0]
    read_already: set[str] = set()
    for entry in readings:
        for flag in READING_ARM_FLAGS:
            value = entry["inputs"].get(flag)
            for sha in (value if isinstance(value, list) else [value]):
                read_already |= {rid for _started, rid in producers.get(sha or "", [])}
    unread_sets = []
    for arm in arms.values():
        for group in arm["sets"]:
            # a score refused by --budget executed nothing and read nothing
            ran = [run_id for run_id in group["score_runs"]
                   if by_id.get(run_id, {}).get("exit_code") == 0]
            if group["on_holdout"] and ran and not set(ran) & read_already:
                unread_sets.append(group)

    warnings = []
    if len(db_hashes) > 1:
        warnings.append(f"runs used {len(db_hashes)} different databases")
    db_path, db_kind = None, None
    if db_hashes:
        top = db_hashes.most_common(1)[0][0]
        for _, manifest in manifests:
            record = manifest.get("inputs", {}).get("db")
            if record and (record.get("sha256") or record.get("path")) == top:
                db_path = record["path"]
                db_kind = record.get("kind") or "sqlite"
                break

    index = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "workspace": {
            "name": workspace.resolve().name,
            "db": {"path": db_path,
                   "sha256": (db_hashes.most_common(1)[0][0]
                              if db_hashes and db_kind != "bigquery" else None),
                   "kind": db_kind},
        },
        "pins": effective_pins(read_pins(workspace), list(arms.values())),
        "arms": list(arms.values()),
        "runs": runs,
        "holdout_looks": len(readings) + len(unread_sets),
        "warnings": warnings,
    }
    target = workspace / "index.json"
    # `next` is the first command a new user runs, and it rebuilds the index
    # before anything has created the workspace, so make it rather than crash
    # with a FileNotFoundError on the temp file.
    workspace.mkdir(parents=True, exist_ok=True)
    # unique per call: two threads rebuilding at once (the viewer fetches
    # two endpoints in parallel) must not race on one temp name
    temp = workspace / f"index.json.{os.getpid()}.{threading.get_ident()}.tmp"
    temp.write_text(json.dumps(index, indent=1))
    os.replace(temp, target)
    return index
