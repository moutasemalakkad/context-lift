"""Run every statement in a .sql file through the bq CLI, one job each.

The point is the job history, not the rows: `contextlift log` reads
INFORMATION_SCHEMA.JOBS_BY_PROJECT, so each statement must run as its own
job under the billing project. Statements are split on a semicolon at the
end of a line; comment-only lines are dropped. Stdlib only.

    python3 examples/thelook/run_queries.py --project PROJECT examples/thelook/queries.sql
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys


def statements(text: str) -> list[str]:
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in re.split(r";\s*\n", body) if s.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sql_file")
    parser.add_argument("--project", required=True, help="billing project the jobs run under")
    parser.add_argument("--label", default="tool:contextlift-seed", help="job label key:value")
    args = parser.parse_args()
    with open(args.sql_file) as handle:
        todo = statements(handle.read())
    failed = 0
    for i, sql in enumerate(todo, 1):
        first_line = sql.splitlines()[0][:70]
        result = subprocess.run(
            ["bq", "query", f"--project_id={args.project}", "--nouse_legacy_sql",
             "--format=none", f"--label={args.label}", "--max_rows=0", sql],
            capture_output=True, text=True)
        status = "ok" if result.returncode == 0 else "FAILED"
        print(f"{i:2d}/{len(todo)} {status}  {first_line}", flush=True)
        if result.returncode != 0:
            failed += 1
            print(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "", file=sys.stderr)
    print(f"{len(todo) - failed} of {len(todo)} statements ran; they appear in the query log within minutes")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
