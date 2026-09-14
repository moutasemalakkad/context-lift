# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""What a run needs before it spends anything, and what a refusal means.

Two jobs, both answerable without a cloud call.

The first is a preflight: is there a credential, is the extra a command needs
installed, is the billing project on the forbidden list, is the workspace
writable. Every one of those failed on a real day one and cost a person an
hour of guessing.

The second is the map from a permission-denied message to the role that
grants it. A 403 names a permission (`dataplex.entries.update`) and never
the role, so an engineer at a customer site reads the message, guesses, and
asks for too much or too little. The map is written down here once, with the
scope each role is granted on, and marked where the repo has not verified it.

`doctor` reports what it checked AND what it could not: a green line for a
role nobody probed would be a lie. Roles are checked by calling the API and
reading the refusal, which is what `--explain` is for.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    """A role, where it is granted, and whether this repo has seen it work."""
    name: str
    scope: str
    verified: bool = True

    def line(self) -> str:
        mark = "" if self.verified else "  (not verified by a run here)"
        return f"{self.name} on the {self.scope}{mark}"


# One permission per line, as it appears in a refusal, and the role that
# carries it. Recorded from real refusals during a run, not from a table:
# 403s, plus the two surfaces phase 0 adds. `verified=False` marks a row the
# a row nobody has actually been refused on is marked "(verify)".
PERMISSION_ROLES: dict[str, Role] = {
    "bigquery.jobs.create": Role("roles/bigquery.jobUser", "billing project, the one in bq://"),
    "bigquery.jobs.listAll": Role("roles/bigquery.resourceViewer", "billing project", False),
    "bigquery.tables.getData": Role("roles/bigquery.dataViewer", "dataset being read"),
    "bigquery.tables.get": Role("roles/bigquery.metadataViewer", "dataset being read"),
    "bigquery.tables.list": Role("roles/bigquery.metadataViewer", "dataset being read"),
    "bigquery.datasets.get": Role("roles/bigquery.metadataViewer", "dataset being read"),
    # authorizing a view is a write on the dataset that holds the source data
    "bigquery.datasets.update": Role("roles/bigquery.dataOwner", "dataset holding the source tables", False),
    "bigquery.datasets.setIamPolicy": Role("roles/bigquery.dataOwner", "dataset holding the source tables", False),
    "dataplex.entries.get": Role("roles/dataplex.catalogViewer", "project being read"),
    "dataplex.entries.list": Role("roles/dataplex.catalogViewer", "project being read"),
    "dataplex.entries.update": Role("roles/dataplex.entryOwner", "entry group being written"),
    "dataplex.entryGroups.useOverviewAspect": Role("roles/dataplex.entryOwner", "entry group being written"),
    "dataplex.entryGroups.useQueriesAspect": Role("roles/dataplex.entryOwner", "entry group being written"),
    "dataplex.entryGroups.useDefinitionEntryLink": Role("roles/dataplex.entryOwner", "entry group being written"),
    "dataplex.entryLinks.create": Role("roles/dataplex.entryOwner", "entry group being written"),
    "dataplex.glossaries.create": Role("roles/dataplex.catalogEditor", "project holding the glossary"),
    "dataplex.glossaryTerms.create": Role("roles/dataplex.catalogEditor", "project holding the glossary"),
    "dataplex.glossaryTerms.use": Role("roles/dataplex.catalogEditor", "project holding the glossary"),
    "dataplex.aspectTypes.create": Role("roles/dataplex.aspectTypeOwner", "project holding the aspect type", False),
    "dataplex.aspectTypes.use": Role("roles/dataplex.aspectTypeUser", "project holding the aspect type", False),
    "aiplatform.endpoints.predict": Role("roles/aiplatform.user", "model project"),
    # the agent under test; no run here has exercised these yet
    "geminidataanalytics.dataAgents.create": Role(
        "roles/geminidataanalytics.dataAgentCreator", "project the agent lives in", False),
    "geminidataanalytics.dataAgents.use": Role(
        "roles/geminidataanalytics.dataAgentUser", "project the agent lives in", False),
}

# A permission looks like `service.resource.verb`, sometimes several words
# deep. Anchored on a dot-separated run of lowercase words so a table name in
# the same message is not read as a permission.
_PERMISSION = re.compile(r"\b([a-z][a-zA-Z]*(?:\.[a-zA-Z]+){2,})\b")


def permissions_in(message: str) -> list[str]:
    """Every permission-shaped token in a refusal, in the order they appear."""
    seen = []
    for token in _PERMISSION.findall(message or ""):
        if token in PERMISSION_ROLES and token not in seen:
            seen.append(token)
    return seen


def explain(message: str) -> list[str]:
    """Turn a refusal into the roles that would have prevented it.

    Returns the lines to print. A message naming no permission this table
    knows says so, and says where to add it, rather than guessing a role.
    """
    found = permissions_in(message)
    if not found:
        unknown = _PERMISSION.findall(message or "")
        if unknown:
            return [f"no role recorded for {unknown[0]}.",
                    "add it to PERMISSION_ROLES in doctor.py once a grant makes it work."]
        return ["no permission named in that message; paste the whole refusal."]
    lines = []
    for permission in found:
        role = PERMISSION_ROLES[permission]
        lines.append(f"{permission}: grant {role.line()}")
    return lines


@dataclass
class Result:
    """One preflight check: what it looked at, and what it found."""
    name: str
    ok: bool
    detail: str
    blocking: bool = True

    def line(self) -> str:
        mark = "ok  " if self.ok else ("FAIL" if self.blocking else "warn")
        return f"  [{mark}] {self.name}: {self.detail}"


EXTRAS = {
    "bigquery": ("google.cloud.bigquery", "any command that runs SQL against bq://"),
    "gemini": ("google.genai", "generate, draft-rules, the judge"),
    "catalog": ("google.cloud.dataplex_v1", "catalog-pull, catalog-push, catalog-route"),
    "ca": ("google.cloud.geminidataanalytics", "the ca_api runner and sentinel"),
    "lookml": ("lkml", "semantic --lookml"),
}


def extras_for_module(module: str) -> list[tuple[str, str]]:
    """Every extra that could explain a missing `module`, in name order.

    A LIST and not one answer, because four of the five extras import under
    `google`. Matching on the top-level package returned whichever of them
    came first in the dict, so a missing `google.cloud.dataplex_v1` was
    reported as the `[bigquery]` extra. That is worse than the traceback it
    replaced: a traceback sends nobody anywhere, and a confident wrong install
    command sends them somewhere wrong. Caught by this module's own test.

    An exact path match wins alone. Otherwise every extra under the same
    top-level package is returned and the caller says so.
    """
    exact = [(extra, purpose) for extra, (path, purpose) in EXTRAS.items()
             if path == module]
    if exact:
        return sorted(exact)
    head = module.split(".")[0]
    return sorted((extra, purpose) for extra, (path, purpose) in EXTRAS.items()
                  if path.split(".")[0] == head)


def missing_extra_message(error: ModuleNotFoundError) -> str:
    """What to type next, for a command that reached for an absent extra.

    The core install is stdlib-only on purpose, so a command that never
    touches BigQuery never installs its client. The cost of that choice is
    that reaching for an uninstalled path has to fail in WORDS. Before this,
    `contextlift semantic --lookml` on a fresh `uv tool install` printed
    `ModuleNotFoundError: No module named 'lkml'` under six frames of
    traceback: a crash by appearance, and `lkml` is not a name anyone can act
    on, since the extra carrying it is spelled `lookml`. EXTRAS already held
    the mapping and nothing read it on the failure path.
    """
    module = getattr(error, "name", "") or ""
    found = extras_for_module(module)

    if not found:
        # Not an extra at all, so this is a real bug rather than a setup
        # problem. Saying otherwise would send someone to install a package
        # that does not exist.
        return (f"missing module {module!r}, which is not one of this tool's "
                f"optional extras ({', '.join(sorted(EXTRAS))}). That is a bug "
                "in contextlift rather than something to install.")

    if len(found) == 1:
        extra, purpose = found[0]
        head = [f"this command needs the [{extra}] extra, which is not "
                f"installed (no module {module!r}).",
                f"  it is used by: {purpose}"]
        installs = [f"  uv tool install 'contextlift[{extra}]'    "
                    "# if you installed the tool",
                    f"  uv pip install -e '.[{extra}]'         "
                    "# if you are in the clone"]
    else:
        # Ambiguous on purpose rather than guessed. Naming all of them costs
        # the reader one line; naming the wrong one costs them an afternoon.
        names = ", ".join(f"[{e}]" for e, _ in found)
        head = [f"module {module!r} is missing. It belongs to more than one of "
                f"this tool's extras, so which one you need depends on the "
                f"command: {names}.",
                *(f"  [{e}] is used by: {p}" for e, p in found)]
        every = ",".join(e for e, _ in found)
        installs = [f"  uv tool install 'contextlift[{every}]'    "
                    "# all of them, if unsure",
                    f"  uv pip install -e '.[{every}]'         "
                    "# if you are in the clone"]

    return "\n".join([*head, "", *installs, "",
                       "  contextlift doctor                       "
                       "# every extra, and what needs it", "",
                       "The core install is stdlib-only on purpose, so a run "
                       "that never touches BigQuery never installs its client."])


def preflight(
    project: str | None = None,
    workspace: str | pathlib.Path | None = None,
    needs: tuple[str, ...] = (),
    *,
    environ: dict | None = None,
    can_import=None,
    have_credentials=None,
) -> list[Result]:
    """Everything checkable before a call: credentials, extras, guards, disk.

    The three side effects are injected so the whole function is testable
    with no cloud and no filesystem surprises: `can_import(module)` says
    whether an extra is installed, `have_credentials()` says whether
    Application Default Credentials resolve.
    """
    environ = os.environ if environ is None else environ
    if can_import is None:
        can_import = _can_import
    if have_credentials is None:
        have_credentials = _have_credentials

    results: list[Result] = []

    forbidden = [p.strip() for p in environ.get("CONTEXTLIFT_FORBIDDEN_PROJECTS", "").split(",")
                 if p.strip()]
    if not forbidden:
        results.append(Result(
            "forbidden projects", True,
            "CONTEXTLIFT_FORBIDDEN_PROJECTS is unset, so no project is refused. Set it to the "
            "prefixes of any project a run must never bill.", blocking=False))
    elif project and any(project.startswith(prefix) for prefix in forbidden):
        hit = next(p for p in forbidden if project.startswith(p))
        results.append(Result("forbidden projects", False,
                              f"{project} matches {hit!r}; nothing here may bill it"))
    else:
        results.append(Result("forbidden projects", True,
                              f"{len(forbidden)} prefix(es) set; {project or 'no project given'} is allowed"))

    for extra in needs:
        module, why = EXTRAS[extra]
        installed = can_import(module)
        results.append(Result(
            f"[{extra}] extra", installed,
            f"{module} imports; {why}" if installed
            else f"{module} is missing. Run: uv pip install -e '.[{extra}]'  ({why})"))

    if needs:
        account = have_credentials()
        forbidden = [f for f in forbidden_accounts(environ)
                     if account and f in account.lower()]
        if not account:
            results.append(Result(
                "credentials", False,
                "no Application Default Credentials. Run: "
                "gcloud auth application-default login"))
        elif forbidden:
            results.append(Result(
                "credentials", False,
                f"the credentials belong to {account}, which matches "
                f"CONTEXTLIFT_FORBIDDEN_ACCOUNTS ({', '.join(forbidden)}). Switch "
                "with `gcloud auth application-default login` before anything "
                "is billed"))
        else:
            results.append(Result(
                "credentials", True,
                f"Application Default Credentials resolve, as {account}"))

    if workspace is not None:
        path = pathlib.Path(workspace)
        writable = path.is_dir() and os.access(path, os.W_OK)
        results.append(Result(
            "workspace", writable,
            f"{path} is writable" if writable
            else f"{path} is not a writable directory; every run record goes there"))

    return results


def _can_import(module: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _have_credentials() -> str | None:
    """The account the credentials belong to, or None when there are none.

    The name, not just yes or no. A machine can hold two accounts at once, a
    personal one and a customer's, and `gcloud config set account` is one
    command away from the wrong one. A report that says only "credentials
    resolve" cannot tell anybody which of the two is about to be billed.
    """
    try:
        import google.auth
        credentials, _project = google.auth.default()
    except Exception:
        return None

    # A service account carries its own address.
    for attribute in ("service_account_email", "signer_email"):
        value = getattr(credentials, attribute, None)
        if value:
            return str(value)

    # A user credential from gcloud does not carry it: `credentials.account`
    # is empty, and so is the `account` field of the credentials file gcloud
    # writes. The active configuration is the only place that names it. Never
    # fall back to the quota project, which is a project id, and naming one as
    # an account is worse than saying nothing.
    for path in _adc_paths():
        try:
            data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except Exception:
            continue
        for key in ("account", "client_email"):
            if data.get(key):
                return str(data[key])
    return _gcloud_active_account() or (
        "resolved, but nothing on this machine names the account")


def _adc_paths() -> list[str]:
    """Where the Application Default Credentials file may be."""
    explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    paths = [explicit] if explicit else []
    paths.append(str(pathlib.Path.home() / ".config" / "gcloud"
                     / "application_default_credentials.json"))
    return paths


def _gcloud_active_account() -> str | None:
    """The account of the gcloud configuration that is currently active.

    Read from the files rather than by running gcloud, so a preflight makes no
    subprocess and no network call.

    The configuration NAME is not the account. One machine had five
    configurations, an active one called `acme` holding a personal account and
    a `default` holding a customer's, so trusting either the name or the
    default would have reported the wrong one.
    """
    root = pathlib.Path.home() / ".config" / "gcloud"
    try:
        name = (root / "active_config").read_text(encoding="utf-8").strip()
    except Exception:
        name = "default"
    try:
        text = (root / "configurations" / f"config_{name}").read_text(encoding="utf-8")
    except Exception:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() == "account" and value.strip():
            return value.strip()
    return None


def forbidden_accounts(environ=None) -> list[str]:
    """The account substrings CONTEXTLIFT_FORBIDDEN_ACCOUNTS names, comma-separated.

    The same shape as CONTEXTLIFT_FORBIDDEN_PROJECTS, and for the same reason:
    configuration rather than source, because a guard that hardcodes a
    customer's domain publishes it to everyone who reads the repository.
    """
    environ = os.environ if environ is None else environ
    return [a.strip().lower() for a in
            environ.get("CONTEXTLIFT_FORBIDDEN_ACCOUNTS", "").split(",") if a.strip()]


def format_report(results: list[Result]) -> tuple[str, int]:
    """The printed report and the exit code: 1 when a blocking check failed."""
    lines = ["doctor: what a run needs before it spends anything", ""]
    lines += [result.line() for result in results]
    failed = [r for r in results if not r.ok and r.blocking]
    warned = [r for r in results if not r.ok and not r.blocking]
    lines.append("")
    if failed:
        lines.append(f"{len(failed)} blocking problem(s); fix them before a paid run.")
    else:
        lines.append("no blocking problem found.")
    if warned:
        lines.append(f"{len(warned)} thing(s) worth setting, listed as warn above.")
    lines.append("")
    lines.append("Roles are not probed here: a green line for a role nobody called would be a")
    lines.append("guess. Run the command, and pass any refusal to `contextlift doctor --explain`.")
    return "\n".join(lines), (1 if failed else 0)
