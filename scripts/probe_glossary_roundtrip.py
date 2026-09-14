# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Does a term written by THIS code read back as the same term, live?

    python scripts/probe_glossary_roundtrip.py --project P [--keep]

`tests/test_glossary_round_trip.py` proves the round trip against a fake. A
fake agrees with whatever its author believed, so it cannot catch the two
things that actually cost time here: that the URL parent and the body parent
of a term differ when a category is involved, and that `list_glossary_terms`
returns the terms inside categories as well as the ones outside.

So this runs the real transport methods -- `upsert_term`, `read_terms`,
`delete_term` -- against the real catalog, and compares what came back with
what went in. It costs nothing: glossary metadata, no model calls, no query
bytes.

IT USES A THROWAWAY GLOSSARY AND DELETES IT, rather than writing into
whatever glossary the project already has. A probe that wrote into a real one
would be editing definitions somebody else depends on, and the damage would
be invisible until a round moved. Pass --keep to leave the probe glossary
behind when a delete failed and you want to look at it.

Deleting a glossary is a long-running operation; deleting a TERM is not. Both
are waited on correctly below, and the difference is not documented anywhere
this project could find.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

PROBE_GLOSSARY = "contextlift-roundtrip-probe"
CATEGORY = "team-hardware-sales"

# (term_id, display_name, description, category_id)
WRITTEN = [
    ("revenue", "revenue",
     "the sale price of items that are neither Cancelled nor Returned", ""),
    ("attach-rate", "attach rate", "accessories sold per device sold", CATEGORY),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", default="us")
    parser.add_argument("--keep", action="store_true",
                        help="do not delete the probe glossary at the end")
    parser.add_argument("--out", default="docs/evidence/glossary-roundtrip.json")
    args = parser.parse_args()

    from context_lift.catalog import transport_for
    from context_lift.export import LABELS

    transport = transport_for(args.project, args.location)
    found: dict = {"glossary": PROBE_GLOSSARY, "steps": [], "verdict": "",
                   "cleaned_up": False}

    def step(name: str, **rest) -> None:
        found["steps"].append({"step": name, **rest})
        print(f"  {name}: " + ", ".join(f"{k}={v}" for k, v in rest.items()))

    print(f"probe: does a term written by this code read back as the same term?")
    print(f"  project {args.project}, glossary {PROBE_GLOSSARY} (throwaway)")

    try:
        for term_id, display, description, category in WRITTEN:
            entry, state = transport.upsert_term(
                PROBE_GLOSSARY, term_id, display, description, dict(LABELS),
                category_id=category)
            step("upsert_term", term=term_id, category=category or "(glossary)",
                 state=state)

        back = transport.read_terms(PROBE_GLOSSARY)
        step("read_terms", count=len(back))

        # The comparison, which is the whole probe. Compared as sorted tuples
        # so a different list order is not a failure: the order a catalog
        # lists terms in is not something this project should depend on.
        wanted = sorted((t, d, desc, cat) for t, d, desc, cat in WRITTEN)
        got = sorted((r["term_id"], r["display_name"], r["description"],
                      r["category_id"]) for r in back
                     if r["term_id"] in {t for t, *_ in WRITTEN})
        found["written"] = wanted
        found["read_back"] = got
        found["identical"] = (wanted == got)
        step("compare", identical=found["identical"])
        if not found["identical"]:
            for a, b in zip(wanted, got):
                if a != b:
                    print(f"    WROTE {a}")
                    print(f"    READ  {b}")

        # Both tiers in one list is the fact that decides whether a caller has
        # to walk the categories. Checked here rather than assumed.
        tiers = {r["category_id"] for r in back}
        found["one_list_sees_both_tiers"] = ("" in tiers and CATEGORY in tiers)
        step("tiers_in_one_list", tiers=sorted(tiers),
             both=found["one_list_sees_both_tiers"])

        # And the delete, because pruning a stale term depends on it.
        state = transport.delete_term(PROBE_GLOSSARY, "attach-rate")
        after = transport.read_terms(PROBE_GLOSSARY)
        found["delete_removed_it"] = ("attach-rate" not in
                                      {r["term_id"] for r in after})
        step("delete_term", term="attach-rate", state=state,
             gone=found["delete_removed_it"])

        found["verdict"] = ("ROUND TRIP IS LOSSLESS" if
                            found["identical"] and found["one_list_sees_both_tiers"]
                            and found["delete_removed_it"]
                            else "ROUND TRIP IS NOT LOSSLESS")
    finally:
        if args.keep:
            print(f"  --keep: glossary {PROBE_GLOSSARY} left in place")
        else:
            found["cleaned_up"] = _delete_glossary(transport, args, found)

    print(f"\n  verdict: {found['verdict']}")
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # The project id never reaches a committed file. This repository may go
    # public, and every evidence file in docs/ goes with it.
    text = json.dumps(found, indent=2).replace(args.project, "<project>")
    out.write_text(text + "\n")
    print(f"  wrote {out}")
    return 0 if found["verdict"] == "ROUND TRIP IS LOSSLESS" else 1


def _delete_glossary(transport, args, found: dict) -> bool:
    """Remove the probe glossary, after emptying it.

    THE CHILDREN GO FIRST, and that is not optional. The first run of this
    probe deleted one term and then asked for the glossary, and the API
    answered `400 Glossary with children can't be deleted`. No reference page
    this project found says so. So every remaining term goes, then every
    category, then the glossary itself.

    Deleting the glossary is a long-running operation and is waited on;
    deleting a term or a category is immediate. That difference is also
    undocumented and was found the same way.

    Reported rather than raised: a probe that already has its answer should
    not lose it to a cleanup failure, and an object left behind in a shared
    project is something a person must be told about rather than discover.
    """
    from context_lift.catalog import GLOSSARY_LOCATION
    name = (f"projects/{args.project}/locations/{GLOSSARY_LOCATION}"
            f"/glossaries/{PROBE_GLOSSARY}")
    try:
        types = transport._types
        client = transport._glossary()
        for row in transport.read_terms(PROBE_GLOSSARY):
            transport.delete_term(PROBE_GLOSSARY, row["term_id"])
        for category in client.list_glossary_categories(
                request=types.ListGlossaryCategoriesRequest(parent=name)):
            client.delete_glossary_category(
                request=types.DeleteGlossaryCategoryRequest(name=category.name))
        client.delete_glossary(
            request=types.DeleteGlossaryRequest(name=name)).result()
        print(f"  deleted the probe glossary, its terms and its categories")
        return True
    except Exception as error:  # noqa: BLE001 - reported, never swallowed
        found["cleanup_error"] = f"{type(error).__name__}: {str(error)[:200]}"
        print(f"  COULD NOT DELETE {PROBE_GLOSSARY}: {found['cleanup_error']}",
              file=sys.stderr)
        print(f"  delete it by hand; nothing else depends on it",
              file=sys.stderr)
        return False


if __name__ == "__main__":
    raise SystemExit(main())
