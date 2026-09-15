# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Render the research archive's source-pinned results as readable light/dark SVGs.

Run `python scripts/make_results_chart.py` or `make results-chart`.
Labels sit above their intervals so neither overlaps the plotted evidence.
The existing tests check each source and regenerate both committed SVGs.
"""
from __future__ import annotations

import json
import pathlib
from html import escape

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "assets" / "results.json"
WIDTH = 440
PAD = 18
BAR_X = 30
BAR_W = WIDTH - 60
TOP = 90
ROW_H = 112
AXIS_MAX = 50.0
TICKS = (0, 10, 20, 30, 40, 50)
THEMES = {
    "light": dict(fg="#1f2328", dim="#59636e", grid="#d1d9e0",
                  bg="#ffffff", good="#0969da", weak="#9a6700"),
    "dark": dict(fg="#f0f6fc", dim="#b1bac4", grid="#3d444d",
                 bg="#0d1117", good="#58a6ff", weak="#e3b341"),
}


def _x(points: float) -> float:
    return BAR_X + BAR_W * points / AXIS_MAX


def _p(value: float) -> str:
    return "p<0.0001" if value < 0.0001 else f"p={value:.4f}".rstrip("0")


def draw(results: list[dict], theme: str) -> str:
    c = THEMES[theme]
    height = TOP + ROW_H * len(results) + 58
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{height}" viewBox="0 0 {WIDTH} {height}" role="img" '
        'aria-labelledby="title description" '
        'font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, '
        'Arial, sans-serif">',
        '<title id="title">Measured accuracy lift from business context</title>',
        f'<desc id="description">{len(results)} studies; each question counted '
        'once. Lines show recorded 95% paired intervals. A missing interval '
        'is labelled. Study conditions differ; these are not rankings.</desc>',
        f'<rect width="{WIDTH}" height="{height}" rx="12" fill="{c["bg"]}"/>',
    ]

    def text(x, y, value, size=14, colour=None, weight=400):
        out.append(f'<text x="{x}" y="{y}" fill="{colour or c["fg"]}" '
                   f'font-size="{size}" font-weight="{weight}">'
                   f'{escape(value)}</text>')

    text(PAD, 29, "What changed with context?", 22, weight=650)
    text(PAD, 54, "Accuracy lift in percentage points · per question", 14, c["dim"])

    for i, row in enumerate(results):
        y = TOP + i * ROW_H
        colour = c["good"] if row["verdict"] == "REAL GAIN" else c["weak"]
        text(PAD, y, row["label"], 16, weight=600)
        text(PAD, y + 23,
             f'{row["questions"]} questions · {row["lift_points"]:+.1f} points'
             f' · {row["verdict"]}', 14, colour)
        mid = y + 47
        out.append(f'<line x1="{BAR_X}" y1="{mid}" '
                   f'x2="{BAR_X + BAR_W}" y2="{mid}" '
                   f'stroke="{c["grid"]}" stroke-width="2"/>')
        for tick in TICKS:
            x = _x(tick)
            out.append(f'<line x1="{x:.1f}" y1="{mid - 4}" '
                       f'x2="{x:.1f}" y2="{mid + 4}" '
                       f'stroke="{c["grid"]}"/>')
        interval = row["lift_ci_points"]
        if interval:
            lo, hi = (_x(v) for v in interval)
            out.append(f'<line x1="{lo:.1f}" y1="{mid}" x2="{hi:.1f}" '
                       f'y2="{mid}" stroke="{colour}" stroke-width="3"/>')
            for x in (lo, hi):
                out.append(f'<line x1="{x:.1f}" y1="{mid - 6}" '
                           f'x2="{x:.1f}" y2="{mid + 6}" '
                           f'stroke="{colour}" stroke-width="2"/>')
            ci = f'95% interval [{interval[0]:+.1f}, {interval[1]:+.1f}]'
        else:
            ci = "Interval not recorded in source"
        out.append(f'<circle cx="{_x(row["lift_points"]):.1f}" cy="{mid}" '
                   f'r="5" fill="{colour}" stroke="{c["bg"]}" stroke-width="2"/>')
        text(PAD, y + 76, f'{ci} · {_p(row["mcnemar_p"])}', 14, c["dim"])

    axis_y = TOP + ROW_H * len(results) - 13
    for tick in TICKS:
        out.append(f'<text x="{_x(tick):.1f}" y="{axis_y}" '
                   f'text-anchor="middle" fill="{c["dim"]}" font-size="13">'
                   f'{tick:+d}</text>')
    text(PAD, height - 33, "Lines: 95% paired intervals. Dots: measured lift.", 14, c["dim"])
    text(PAD, height - 12, "Sources and study limits: docs/research/README.md", 13, c["dim"])
    out.append('</svg>')
    return "\n".join(out) + "\n"


def shown(results: list[dict]) -> list[dict]:
    """Keep variant-against-variant studies in the record, outside this chart."""
    return [r for r in results if r.get("show_in_readme", True)]


def main() -> int:
    results = shown(json.loads(DATA.read_text())["results"])
    for theme in THEMES:
        path = DATA.parent / f"results-{theme}.svg"
        path.write_text(draw(results, theme))
        print(f"wrote {path.relative_to(ROOT)} ({len(results)} results)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
