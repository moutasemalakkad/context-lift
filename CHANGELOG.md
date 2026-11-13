# Changelog

Notable changes, newest first. Format follows [Keep a Changelog][kac]; this
project uses [semantic versioning][semver].

**This file starts at the first public release and is not backfilled.** The
work before that release happened in a private repository, and a changelog
written backwards from commits nobody outside could read would be fiction —
invented entries, invented dates, invented reasons. What came before is in the
git history and in `docs/CASE_STUDY.md`, which records the measurements
themselves rather than a story about them.

## [Unreleased]

### Added

- Published to PyPI, so `uvx contextlift demo` runs the whole loop with
  nothing installed and no clone. `make check-wheel` builds the wheel and runs
  that demo from a clean virtual environment outside the checkout, which is
  what the release workflow checks before it publishes anything.
- `project.urls` and trove classifiers in `pyproject.toml`, so the PyPI page
  links back to the repo, the quickstart, the changelog and the issue tracker.

Nothing is tagged yet. These entries become `0.1.0` when `v0.1.0` is pushed.

[kac]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html
