# Copilot instructions for `gfff`

## Build, test, and lint commands

- **Install/update buildbot + user service (main operational build/install path):**
  - `bash install-buildbot.sh`
- **Manual dev install (editable, from README):**
  - `python3 -m venv ~/.local/share/gfff-buildbot/.venv`
  - `~/.local/share/gfff-buildbot/.venv/bin/python -m pip install --upgrade pip setuptools wheel`
  - `~/.local/share/gfff-buildbot/.venv/bin/python -m pip install --upgrade -e .`
- **Run all tests:**
  - `python3 -m unittest discover -s tests -q`
- **Run a single test:**
  - `python3 -m unittest tests.test_buildbot.NormalizeJobsTests.test_allows_build_only_job -q`
- **Lint:**
  - No dedicated lint command is configured in this repository (`pyproject.toml` has no lint tool config).

## High-level architecture

- The project is a single-module CLI app (`buildbot.py`) exposed as `gfff-buildbot` and `gb` via `pyproject.toml`.
- Runtime flow in `main()`:
  1. Parse CLI flags (`--check`, `--import`, scheduler/once flags).
  2. Resolve config files (explicit `--config` or discovery/merge path).
  3. Normalize job entries and enforce job schema.
  4. Enter `run_loop()` for scheduling + queueing through `pueue`.
- Config discovery/merge is opinionated:
  - `discover_default_config_paths()` loads `~/.config/gfff/*.yaml` first (`gfff.yaml` first, then lexical order), then current-dir `global.yaml` (legacy fallback: `gfff.yaml`), with dev fallback last.
  - `merge_jobs_from_configs()` keeps the first job by `name` and logs duplicates from later files as skipped.
- Scheduler behavior (`run_loop()`):
  - Uses one shared pueue group and sets group parallelism to `os.cpu_count()`.
  - Tracks next-run timestamps for interval and daily (`at`) jobs.
  - Reloads merged config files periodically (`--reload-config-seconds`) and recomputes schedule only when job schedule fields change.
  - Applies fast retry after runtime errors (`--error-retry-seconds`, `--at-error-retry-seconds`).
- Git-gated queueing (`prepare_repo_for_build()`):
  - Default path: `git fetch` + compare local head vs configured `git-remote-ref` + pull only when remote is strictly ahead.
  - Daily scheduled jobs (`run-mode: scheduled` + `at`) are time-driven and skip git update checks.
  - `--force` bypasses update-detection gating but still runs fetch/pull attempts.
- Build command execution:
  - `generate_build_script()` assembles a `set -e` shell script in strict order:
    `cleanup` -> `pre-build` -> `test` -> `build` -> `post-build`.
  - `queue_job()` submits that script to pueue with sanitized shell env (`BASH_ENV`/`ENV` unset).

## Key codebase conventions

- Job config format is a **YAML list of mappings**, not a keyed object.
- Active jobs must define **at least one** of `test` or `build`, and **exactly one** of `interval` or `at`.
- `test`, `build`, `cleanup`, `pre-build`, `post-build` accept either a single string or a list of strings; code normalizes all into `*_steps`.
- Daily `at` values should be `HH:MM` and quoted in YAML (`at: "05:00"`).
- `run-mode` is strictly one of `normal`, `manual`, `scheduled`; `--once` normally skips `scheduled` jobs unless a specific `job_name` filter is provided.
- `disable-when-run` mutates the source YAML by flipping `active: true` to `active: false` within that job block before queueing.
- Job names are treated as stable identifiers across merged config files; keep them unique across all loaded YAML files.
