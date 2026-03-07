#!/usr/bin/env python3

"""Schedule git-aware build jobs from gfff.yaml through pueue."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List


def load_yaml_config(config_path: Path) -> List[Dict[str, Any]]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required. Install it with: pip install pyyaml"
        ) from exc

    raw = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, list):
        raise ValueError(f"Expected a list of jobs in {config_path}")

    jobs: List[Dict[str, Any]] = []
    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Job #{idx} must be a mapping")
        jobs.append(item)
    return jobs


def sanitize_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip())
    return cleaned.strip("-") or "job"


def normalize_jobs(jobs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for idx, job in enumerate(jobs, start=1):
        if not job.get("active", False):
            continue

        name = str(job.get("name", "")).strip() or f"job-{idx}"
        path = str(job.get("path", "")).strip()
        build = str(job.get("build", "")).strip()
        interval = job.get("interval", 0)

        if not path:
            raise ValueError(f"Job '{name}' is missing 'path'")
        if not build:
            raise ValueError(f"Job '{name}' is missing 'build'")

        try:
            interval_s = int(interval)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Job '{name}' has invalid 'interval': {interval}") from exc

        if interval_s <= 0:
            raise ValueError(f"Job '{name}' must have interval > 0")

        normalized.append(
            {
                "name": name,
                "slug": sanitize_name(name),
                "path": str(Path(path).expanduser()),
                "build": build,
                "interval": interval_s,
                "cleanup": str(job.get("cleanup", "")).strip(),
                "pre_build": str(job.get("pre-build", "")).strip(),
                "post_build": str(job.get("post-build", "")).strip(),
            }
        )

    return normalized


def ensure_pueue_group(group: str, dry_run: bool) -> None:
    cmd = ["pueue", "group", "add", group]
    if dry_run:
        print("DRY RUN:", " ".join(shlex.quote(p) for p in cmd))
        return

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return

    text = (result.stdout + result.stderr).lower()
    if "already" in text and "exist" in text:
        return
    raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def set_group_parallelism(group: str, dry_run: bool) -> None:
    cmd = ["pueue", "parallel", "-g", group, "1"]
    if dry_run:
        print("DRY RUN:", " ".join(shlex.quote(p) for p in cmd))
        return

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def generate_job_script(job: Dict[str, Any]) -> str:
    lines = [
        "set -e",
        f"cd {shlex.quote(job['path'])}",
        "git fetch",
        "local_head=$(git rev-parse HEAD)",
        "remote_head=$(git rev-parse @{u})",
        'if [ "$local_head" = "$remote_head" ]; then',
        f"  echo {shlex.quote('[' + job['name'] + '] up to date; skipping build')}",
        "  exit 0",
        "fi",
        "git pull --ff-only",
    ]

    if job["cleanup"]:
        lines.append(job["cleanup"])
    if job["pre_build"]:
        lines.append(job["pre_build"])
    lines.append(job["build"])
    if job["post_build"]:
        lines.append(job["post_build"])

    return "\n".join(lines)


def get_pending_groups() -> set[str]:
    result = subprocess.run(
        ["pueue", "status", "-j"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    data = json.loads(result.stdout)
    tasks = data.get("tasks", {})
    pending_states = {"Queued", "Running", "Paused", "Stashed", "Locked"}
    pending_groups: set[str] = set()

    for task in tasks.values():
        group = task.get("group")
        status = task.get("status")
        state = ""

        if isinstance(status, str):
            state = status
        elif isinstance(status, dict) and status:
            state = next(iter(status.keys()))

        if group and state in pending_states:
            pending_groups.add(str(group))

    return pending_groups


def queue_job(job: Dict[str, Any], group_prefix: str, dry_run: bool) -> None:
    group = f"{group_prefix}-{job['slug']}"
    ensure_pueue_group(group, dry_run=dry_run)
    set_group_parallelism(group, dry_run=dry_run)

    script = generate_job_script(job)
    cmd = ["pueue", "add", "-g", group, "bash", "-lc", script]
    if dry_run:
        print("DRY RUN:", " ".join(shlex.quote(p) for p in cmd))
        return

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    print(f"queued [{job['name']}] in group {group}: {result.stdout.strip()}")


def check_dependencies() -> None:
    if shutil.which("pueue") is None:
        raise RuntimeError("pueue is not installed or not in PATH")
    if shutil.which("git") is None:
        raise RuntimeError("git is not installed or not in PATH")


def run_loop(
    jobs: List[Dict[str, Any]],
    group_prefix: str,
    tick: int,
    dry_run: bool,
    run_once: bool,
) -> int:
    if not jobs:
        print("No active jobs found.")
        return 0

    now = time.time()
    next_runs = {job["name"]: now for job in jobs}

    while True:
        pending_groups: set[str] = set()
        if not dry_run:
            pending_groups = get_pending_groups()

        loop_now = time.time()
        for job in jobs:
            if loop_now < next_runs[job["name"]]:
                continue

            group = f"{group_prefix}-{job['slug']}"
            if group in pending_groups:
                print(f"skip [{job['name']}]: group {group} still has queued/running task")
            else:
                queue_job(job, group_prefix=group_prefix, dry_run=dry_run)

            next_runs[job["name"]] = loop_now + int(job["interval"])

        if run_once:
            return 0

        time.sleep(max(1, tick))


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run periodic pueue build tasks from gfff.yaml"
    )
    parser.add_argument(
        "--config",
        default="gfff.yaml",
        help="Path to gfff yaml config file (default: gfff.yaml)",
    )
    parser.add_argument(
        "--group-prefix",
        default="gfff",
        help="Prefix for pueue groups (default: gfff)",
    )
    parser.add_argument(
        "--tick",
        type=int,
        default=5,
        help="Scheduler polling interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Queue eligible jobs once and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print pueue commands without executing them",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        check_dependencies()
        config_path = Path(args.config).expanduser().resolve()
        jobs = normalize_jobs(load_yaml_config(config_path))
        return run_loop(
            jobs=jobs,
            group_prefix=args.group_prefix,
            tick=args.tick,
            dry_run=args.dry_run,
            run_once=args.once,
        )
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130
    except (
        RuntimeError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as exc:  # pragma: no cover
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
