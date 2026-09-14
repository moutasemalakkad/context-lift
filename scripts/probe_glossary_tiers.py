# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Can a Knowledge Catalog glossary term be filed under a team category?

    make probe-glossary-tiers PROJECT=your-project

M8 item 4 asks for "org glossary, team categories". That needs a capability
nobody here had confirmed, and the honest options were to ask the product team
or to try it. This is trying it.

The probe creates a glossary, one category inside it, one term whose parent is
that category and one term whose parent is the glossary itself, reads both
back to see what the service actually stored, and deletes all four. It is a
script rather than commands in a chat window so the answer can be re-checked
when the API changes, and so a claim in docs/ has something behind it.

Cost: four metadata objects, created and deleted. A few hundred bytes, inside
the Dataplex catalog free tier, so effectively nothing. It writes nothing
outside the glossary it creates, and deletes only what it created.

Needs an Application Default Credentials token:

    CLOUDSDK_CONFIG=$HOME/.gcloud/<config> gcloud auth application-default login

The other kind of token gcloud can print -- the first-party one -- is refused
by domain policy on a google.com account. The tell is
`401 ACCESS_TOKEN_TYPE_UNSUPPORTED` on EVERY googleapis.com call, BigQuery
included, which is how to tell a credential problem from a service problem.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://dataplex.googleapis.com/v1"
PROBE_ID = "contextlift-probe-glossary-tiers"


def plan(project: str, location: str, glossary_id: str) -> dict:
    """Every request this probe will make, as data.

    Split out so the request bodies can be checked without a network, and so
    a reader can see exactly what gets written before running it.
    """
    glossary = f"projects/{project}/locations/{location}/glossaries/{glossary_id}"
    category = f"{glossary}/categories/team-hardware-sales"
    return {
        "glossary": (f"{API}/projects/{project}/locations/{location}"
                     f"/glossaries?glossaryId={glossary_id}",
                     {"displayName": "contextlift probe",
                      "description": "Temporary. Created by "
                                     "scripts/probe_glossary_tiers.py to test "
                                     "whether a term can be filed under a "
                                     "category. Safe to delete."}),
        "category": (f"{API}/{glossary}/categories?categoryId=team-hardware-sales",
                     {"parent": glossary,
                      "displayName": "hardware sales",
                      "description": "One team's definitions."}),
        # The term that answers the question. Note the asymmetry the API
        # documents and this probe confirms: the URL parent must be the
        # GLOSSARY, while the body parent is what selects the category.
        "term_in_category": (f"{API}/{glossary}/terms?termId=attach-rate",
                             {"parent": category,
                              "displayName": "attach rate",
                              "description": "Accessories per handset."}),
        "term_in_glossary": (f"{API}/{glossary}/terms?termId=revenue",
                             {"parent": glossary,
                              "displayName": "revenue",
                              "description": "Sale price of items neither "
                                             "Cancelled nor Returned."}),
        "names": {"glossary": glossary, "category": category},
    }


def _token() -> str:
    out = subprocess.run(
        ["gcloud", "auth", "application-default", "print-access-token"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(
            "no ADC token. Run:\n"
            "  CLOUDSDK_CONFIG=$HOME/.gcloud/<config> "
            "gcloud auth application-default login\n"
            f"gcloud said: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


def _call(method: str, url: str, token: str, project: str,
          body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "X-Goog-User-Project": project,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        payload = error.read()
        try:
            return error.code, json.loads(payload or b"{}")
        except json.JSONDecodeError:
            return error.code, {"error": {"message": payload.decode()[:300]}}


def _wait(name: str, token: str, project: str, tries: int = 30) -> None:
    """Creating a glossary is a long-running operation; a category and a term
    are not. Learned by running it, not from the reference."""
    for _ in range(tries):
        _, body = _call("GET", f"{API}/{name}", token, project)
        if body.get("done"):
            return
        time.sleep(2)
    raise SystemExit(f"operation never finished: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", required=True,
                        help="where the four temporary objects are created")
    parser.add_argument("--location", default="global")
    parser.add_argument("--glossary-id", default=PROBE_ID)
    parser.add_argument("--out", default=None, metavar="FILE.json",
                        help="write the evidence, so a claim can cite it")
    parser.add_argument("--keep", action="store_true",
                        help="leave the four objects behind for inspection")
    args = parser.parse_args()

    token = _token()
    steps = plan(args.project, args.location, args.glossary_id)
    names = steps["names"]
    # The evidence file is committed and this repository will be public, so
    # it records a placeholder. The project name adds nothing to the finding,
    # and the printed output still names it for whoever is watching the run.
    found: dict = {"project": "<project>", "location": args.location,
                   "steps": [], "verdict": None}

    def record(label: str, status: int, body: dict) -> dict:
        error = (body.get("error") or {}).get("message", "")
        found["steps"].append({"step": label, "status": status,
                               "error": error[:200] or None})
        print(f"  {label:22} HTTP {status}" + (f"  {error[:80]}" if error else ""))
        return body

    print(f"probe: can a glossary term live under a category? "
          f"({args.project}, {args.location})")
    try:
        url, body = steps["glossary"]
        created = record("create glossary", *_call("POST", url, token,
                                                   args.project, body))
        if "operations/" in (created.get("name") or ""):
            _wait(created["name"], token, args.project)

        for label in ("category", "term_in_category", "term_in_glossary"):
            url, body = steps[label]
            record(f"create {label}",
                   *_call("POST", url, token, args.project, body))

        # what the service actually stored, which is the only thing that counts
        _, listed = _call("GET", f"{API}/{names['glossary']}/terms",
                          token, args.project)
        terms = listed.get("terms") or []
        stored = {t["name"].rsplit("/", 1)[-1]:
                  ("category" if "/categories/" in (t.get("parent") or "")
                   else "glossary")
                  for t in terms}
        found["stored_parents"] = stored
        found["one_list_sees_both_tiers"] = len(terms) == 2
        print(f"  stored parents:        {stored}")
        print(f"  one glossary-level list returned {len(terms)} term(s): "
              + ("both tiers in one call" if len(terms) == 2
                 else "scoped, so each category needs its own read"))

        works = stored.get("attach-rate") == "category"
        found["verdict"] = ("TERM UNDER A CATEGORY WORKS" if works
                            else "IT DOES NOT WORK")
        print(f"\n  verdict: {found['verdict']}")
    finally:
        if not args.keep:
            print("  cleanup:")
            for suffix in ("/terms/attach-rate", "/terms/revenue",
                           "/categories/team-hardware-sales", ""):
                status, body = _call("DELETE", f"{API}/{names['glossary']}{suffix}",
                                     token, args.project)
                # Deleting the GLOSSARY is a long-running operation too, and
                # it answers 200 with an operation rather than a result. The
                # second run of this probe reported "STILL THERE" because it
                # checked before the operation finished; the first run only
                # passed because a sleep happened to be long enough.
                waited = ""
                if "operations/" in (body.get("name") or ""):
                    _wait(body["name"], token, args.project)
                    waited = "  (waited for the operation)"
                print(f"    delete {suffix or '(the glossary)':36} "
                      f"HTTP {status}{waited}")
            status, _ = _call("GET", f"{API}/{names['glossary']}",
                              token, args.project)
            found["cleaned_up"] = status == 404
            print(f"    glossary GET -> {status} "
                  + ("(gone)" if status == 404
                     else "(STILL THERE -- delete it by hand)"))

    if args.out:
        # make the directory rather than assume it: the first run of this
        # script failed on a missing docs/evidence/ after everything else
        # had already worked and been cleaned up
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w") as handle:
            json.dump(found, handle, indent=2)
            handle.write("\n")
        print(f"  wrote {args.out}")
    return 0 if (found.get("verdict") or "").startswith("TERM UNDER") else 1


if __name__ == "__main__":
    sys.exit(main())
