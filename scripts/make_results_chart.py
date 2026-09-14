# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Draw the README's results chart from docs/assets/results.json.

    python scripts/make_results_chart.py        # or: make results-chart

Writes two files, light and dark, because a white chart looks broken in
GitHub's dark theme and GitHub picks between them with `prefers-color-scheme`.

Why a script and not a drawing: every figure this project ships has to be
produced by something a person can re-run and point at. A chart is figures.
`tests/test_results_chart.py` regenerates both files and fails if the
committed ones have gone stale, so the picture cannot drift from the data.

Plain SVG by hand, no plotting library. Four horizontal bars do not justify a
dependency, and hand-written SVG is diffable in review.
"""
from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "assets" / "results.json"

# geometry, in SVG user units (they are CSS pixels at width=100%)
PAD_L = 4          # keep glyphs off the left edge
LABEL_W = 300      # the left column of text
BAR_X = LABEL_W + 12
BAR_W = 330        # the plot area
ROW_H = 54
TOP = 34           # room for the axis above the first bar
BOT = 34           # room for the footnote under the last
BAR_H = 15
AXIS_MAX = 50.0    # points; must cover the widest INTERVAL, not the widest
                   # bar -- at 35 the +49.8 whisker was clipped and the
                   # chart read as if that interval stopped at 35
TICKS = (0, 10, 20, 30, 40, 50)

THEMES = {
    "light": dict(fg="#1f2328", dim="#59636e", grid="#d1d9e0", bg="none",
                  good="#1a7f37", weak="#9a6700", rule="#d1d9e0"),
    "dark": dict(fg="#f0f6fc", dim="#9198a1", grid="#3d444d", bg="none",
                 good="#3fb950", weak="#d29922", rule="#3d444d"),
}


def _x(points: float) -> float:
    """Points of accuracy lift to an x coordinate."""
    return BAR_X + BAR_W * min(points, AXIS_MAX) / AXIS_MAX


def _p(value: float) -> str:
    """A p-value a person reads, not a float repr. 0.004425 -> 0.0044."""
    if value < 0.0001:
        return "p<0.0001"
    return "p=" + f"{value:.4f}".rstrip("0")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def draw(results: list[dict], theme: str) -> str:
    """The whole chart as one SVG string."""
    c = THEMES[theme]
    height = TOP + ROW_H * len(results) + BOT
    width = BAR_X + BAR_W + 118       # room for the verdict word on the right
    font = ("-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, "
            "Arial, sans-serif")
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'font-family="{font}" role="img" '
        f'aria-label="Accuracy gain from written business context, '
        f'four measured runs.">',
    ]

    # the axis: faint gridlines behind everything, labelled in points
    for tick in TICKS:
        x = _x(tick)
        out.append(f'<line x1="{x:.1f}" y1="{TOP - 8}" x2="{x:.1f}" '
                   f'y2="{TOP + ROW_H * len(results) - 18}" '
                   f'stroke="{c["grid"]}" stroke-width="1"/>')
        out.append(f'<text x="{x:.1f}" y="{TOP - 14}" fill="{c["dim"]}" '
                   f'font-size="11" text-anchor="middle">+{tick}</text>')

    for i, row in enumerate(results):
        y = TOP + i * ROW_H
        mid = y + BAR_H / 2
        proven = row["verdict"] == "REAL GAIN"
        colour = c["good"] if proven else c["weak"]

        # left column: what the context was, and how many questions
        out.append(f'<text x="{PAD_L}" y="{mid + 1:.1f}" fill="{c["fg"]}" '
                   f'font-size="13">{_esc(row["label"])}</text>')
        by = "written by a person" if row["written_by"] == "people" \
            else "read by a machine"
        out.append(f'<text x="{PAD_L}" y="{mid + 17:.1f}" fill="{c["dim"]}" '
                   f'font-size="11">{row["questions"]} questions, '
                   f'{by}</text>')

        # the bar
        w = max(_x(row["lift_points"]) - BAR_X, 2.0)
        out.append(f'<rect x="{BAR_X}" y="{y:.1f}" width="{w:.1f}" '
                   f'height="{BAR_H}" rx="2" fill="{colour}"'
                   + ('' if proven else ' opacity="0.55"') + '/>')

        # the 95% interval, where the source recorded one
        interval = row["lift_ci_points"]
        if interval:
            lo, hi = (_x(v) for v in interval)
            out.append(f'<line x1="{lo:.1f}" y1="{mid:.1f}" x2="{hi:.1f}" '
                       f'y2="{mid:.1f}" stroke="{c["fg"]}" stroke-width="1.5" '
                       f'opacity="0.75"/>')
            for x in (lo, hi):
                out.append(f'<line x1="{x:.1f}" y1="{mid - 5:.1f}" '
                           f'x2="{x:.1f}" y2="{mid + 5:.1f}" '
                           f'stroke="{c["fg"]}" stroke-width="1.5" '
                           f'opacity="0.75"/>')

        # the number: inside the bar when the bar has room for it, and just
        # past the bar when it does not. Placing it after the WHISKER instead
        # ran it into the verdict column on the widest row.
        value = f'+{row["lift_points"]:.1f}'
        if w > 54:
            out.append(f'<text x="{BAR_X + w - 7:.1f}" y="{mid + 4:.1f}" '
                       f'fill="#ffffff" font-size="12" font-weight="700" '
                       f'text-anchor="end">{value}</text>')
        else:
            out.append(f'<text x="{BAR_X + w + 7:.1f}" y="{mid + 4:.1f}" '
                       f'fill="{c["fg"]}" font-size="12" font-weight="700">'
                       f'{value}</text>')
        out.append(f'<text x="{BAR_X + BAR_W + 8}" y="{mid + 4:.1f}" '
                   f'fill="{colour}" font-size="11" font-weight="600">'
                   f'{row["verdict"]}</text>')
        out.append(f'<text x="{BAR_X + BAR_W + 8}" y="{mid + 18:.1f}" '
                   f'fill="{c["dim"]}" font-size="10">'
                   f'{_p(row["mcnemar_p"])}</text>')

    foot = ("bars are the per-question accuracy gain in points; whiskers are "
            "the 95% paired interval, drawn where the source recorded one")
    out.append(f'<text x="{PAD_L}" y="{height - 10}" fill="{c["dim"]}" '
               f'font-size="10">{foot}</text>')
    out.append('</svg>')
    return "\n".join(out) + "\n"


def shown(results: list[dict]) -> list[dict]:
    """The rows the README draws. A row set `show_in_readme: false` stays in
    results.json and stays verified by the tests -- the finding is kept and
    checked, it is just argued in docs/CASE_STUDY.md instead of on the front
    page."""
    return [r for r in results if r.get("show_in_readme", True)]


def main() -> int:
    results = shown(json.loads(DATA.read_text())["results"])
    for theme in THEMES:
        path = DATA.parent / f"results-{theme}.svg"
        path.write_text(draw(results, theme))
        print(f"wrote {path.relative_to(ROOT)} "
              f"({path.stat().st_size} bytes, {len(results)} results)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
