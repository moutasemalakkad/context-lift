# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Draw the README workflow in wide and stacked layouts, without dependencies.

The picture element selects the stacked version on narrow screens. Keep the
steps as text here so the two versions always describe the same workflow.
"""
from pathlib import Path

STEPS = ("Prepare questions", "Establish baseline", "Add context",
         "Compare answers", "Inspect failures")
ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"


def draw(stacked: bool) -> str:
    width, height = (360, 386) if stacked else (860, 100)
    box_w, box_h = (300, 58) if stacked else (156, 78)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        'role="img" aria-labelledby="title" '
        'font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Arial, sans-serif">',
        '<title id="title">Prepare questions, establish baseline, add context, '
        'compare answers, then inspect failures.</title>',
    ]
    for i, label in enumerate(STEPS):
        x, y = (30, 8 + i * 78) if stacked else (5 + i * 172, 10)
        out.append(f'<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" '
                   'rx="8" fill="#f6f8fa" stroke="#8c959f"/>')
        if stacked:
            out.append(f'<circle cx="{x + 28}" cy="{y + 29}" r="15" fill="#0969da"/>')
            out.append(f'<text x="{x + 28}" y="{y + 35}" text-anchor="middle" '
                       f'font-size="16" fill="#fff">{i + 1}</text>')
            out.append(f'<text x="{x + 54}" y="{y + 35}" font-size="17" '
                       f'fill="#1f2328">{label}</text>')
        else:
            out.append(f'<text x="{x + box_w / 2}" y="{y + 29}" text-anchor="middle" '
                       f'font-size="17" fill="#0969da" font-weight="600">0{i + 1}</text>')
            out.append(f'<text x="{x + box_w / 2}" y="{y + 55}" text-anchor="middle" '
                       f'font-size="15" fill="#1f2328">{label}</text>')
        if i < len(STEPS) - 1:
            if stacked:
                line = f'M180 {y + box_h + 3} v13 m-4 -4 l4 4 4 -4'
            else:
                line = f'M{x + box_w + 3} 49 h10 m-4 -4 l4 4 -4 4'
            out.append(f'<path d="{line}" fill="none" stroke="#6e7781" '
                       'stroke-width="1.5" stroke-linecap="round"/>')
    return '\n'.join(out + ['</svg>', ''])


if __name__ == '__main__':
    for stacked, label in ((False, 'wide'), (True, 'stacked')):
        path = ASSETS / f'workflow-{label}.svg'
        path.write_text(draw(stacked))
        print(f'wrote {path.relative_to(ASSETS.parent.parent)}')
