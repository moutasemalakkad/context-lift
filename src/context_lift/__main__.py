# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Run the CLI as a module: `python -m context_lift score|gate ...`.

Same entry point as the installed `contextlift` console script, but works
without the script being on PATH (used by `make example`).
"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
