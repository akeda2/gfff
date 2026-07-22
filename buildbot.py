#!/usr/bin/env python3

"""Schedule git-aware build jobs from gfff.yaml through pueue."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def log_event(level: str, message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} {level} {message}")


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


def parse_bool(value: Any, field: str, job_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"Job '{job_name}' has invalid '{field}': {value}")


def parse_at_time(value: Any, job_name: str) -> str:
    at = str(value).strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError(
            f"Job '{job_name}' has invalid 'at': {value}. Expected HH:MM (24-hour)."
        )
    return at


def next_daily_at_timestamp(at_time: str, from_ts: float) -> float:
    hour, minute = map(int, at_time.split(":"))
    now = dt.datetime.fromtimestamp(from_ts)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= from_ts:
        candidate += dt.timedelta(days=1)
    return candidate.timestamp()


def normalize_jobs(jobs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for idx, job in enumerate(jobs, start=1):
        if not job.get("active", False):
            continue

        name = str(job.get("name", "")).strip() or f"job-{idx}"
        path = str(job.get("path", "")).strip()
        build = str(job.get("build", "")).strip()
        test = str(job.get("test", "")).strip()
        interval = job.get("interval")
        at = str(job.get("at", "")).strip()

        if not path:
            raise ValueError(f"Job '{name}' is missing 'path'")
        if not build and not test:
            raise ValueError(f"Job '{name}' must define at least one of 'build' or 'test'")

        has_interval = interval not in (None, "")
        has_at = bool(at)
        if has_interval and has_at:
            raise ValueError(f"Job '{name}' must set only one of 'interval' or 'at'")
        if not has_interval and not has_at:
            raise ValueError(f"Job '{name}' must set one of 'interval' or 'at'")

        interval_s: Optional[int] = None
        at_s: str = ""

        if has_interval:
            try:
                interval_s = int(interval)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Job '{name}' has invalid 'interval': {interval}") from exc

            if interval_s <= 0:
                raise ValueError(f"Job '{name}' must have interval > 0")
        else:
            at_s = parse_at_time(at, name)

        git_strict = parse_bool(job.get("git-strict", True), "git-strict", name)
        git_pull = str(job.get("git-pull", "git pull --ff-only")).strip()
        git_remote_ref = str(job.get("git-remote-ref", "@{u}")).strip()

        if not git_pull:
            raise ValueError(f"Job '{name}' has invalid 'git-pull': {git_pull}")
        if not git_remote_ref:
            raise ValueError(
                f"Job '{name}' has invalid 'git-remote-ref': {git_remote_ref}"
            )

        normalized.append(
            {
                "name": name,
                "slug": sanitize_name(name),
                "path": str(Path(path).expanduser()),
                "build": build,
                "test": test,
                "interval": interval_s,
                "at": at_s,
                "cleanup": str(job.get("cleanup", "")).strip(),
                "pre_build": str(job.get("pre-build", "")).strip(),
                "post_build": str(job.get("post-build", "")).strip(),
                "manual_install_cmd": str(job.get("manual-install-cmd", "")).strip(),
                "git_strict": git_strict,
                "git_pull": git_pull,
                "git_remote_ref": git_remote_ref,
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


def generate_build_script(job: Dict[str, Any]) -> str:
    lines = ["set -e", f"cd {shlex.quote(job['path'])}"]

    if job["cleanup"]:
        lines.append(job["cleanup"])
    if job["pre_build"]:
        lines.append(job["pre_build"])
    if job.get("test"):
        lines.append(str(job["test"]))
    if job.get("build"):
        lines.append(str(job["build"]))
    if job["post_build"]:
        lines.append(job["post_build"])

    return "\n".join(lines)


def run_repo_command(job: Dict[str, Any], cmd: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("BASH_ENV", None)
    env.pop("ENV", None)

    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-lc", cmd],
        cwd=job["path"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def prepare_repo_for_build(job: Dict[str, Any], dry_run: bool, force_run: bool = False) -> bool:
    label = "[" + job["name"] + "]"
    strict = bool(job.get("git_strict", True))
    git_pull = str(job.get("git_pull", "git pull --ff-only"))
    git_remote_ref = str(job.get("git_remote_ref", "@{u}"))

    repo_path = Path(str(job["path"]))
    if not repo_path.is_dir():
        log_event("ERROR", f"skip {label}: path does not exist: {repo_path}")
        return False

    if force_run:
        log_event("INFO", f"force {label}: skipping git update checks")
        return True

    if dry_run:
        print(f"DRY RUN: ({job['path']}) git fetch")
        print(
            "DRY RUN:",
            f"({job['path']}) git rev-parse --abbrev-ref --symbolic-full-name {git_remote_ref}",
        )
        print("DRY RUN:", f"({job['path']}) git rev-parse HEAD")
        print("DRY RUN:", f"({job['path']}) git rev-parse {git_remote_ref}")
        print("DRY RUN:", f"({job['path']}) {git_pull}")
        return True

    fetch_result = run_repo_command(job, "git fetch")
    if fetch_result.returncode != 0:
        msg = fetch_result.stderr.strip() or fetch_result.stdout.strip() or "git fetch failed"
        if strict:
            raise RuntimeError(f"{label} {msg}")
        log_event("ERROR", f"skip {label}: git fetch failed ({msg})")
        return False

    remote_ref_check = run_repo_command(
        job,
        "git rev-parse --abbrev-ref --symbolic-full-name "
        + shlex.quote(git_remote_ref)
        + " >/dev/null 2>&1",
    )
    if remote_ref_check.returncode != 0:
        log_event(
            "ERROR",
            f"skip {label}: cannot resolve git-remote-ref {git_remote_ref}",
        )
        return False

    local_head_result = run_repo_command(job, "git rev-parse HEAD")
    if local_head_result.returncode != 0:
        msg = local_head_result.stderr.strip() or local_head_result.stdout.strip() or "git rev-parse HEAD failed"
        if strict:
            raise RuntimeError(f"{label} {msg}")
        log_event("ERROR", f"skip {label}: failed to read local HEAD ({msg})")
        return False

    remote_head_result = run_repo_command(job, "git rev-parse " + shlex.quote(git_remote_ref))
    if remote_head_result.returncode != 0:
        msg = remote_head_result.stderr.strip() or remote_head_result.stdout.strip() or "git rev-parse remote ref failed"
        if strict:
            raise RuntimeError(f"{label} {msg}")
        log_event(
            "ERROR",
            f"skip {label}: failed to read remote ref {git_remote_ref} ({msg})",
        )
        return False

    local_head = local_head_result.stdout.strip()
    remote_head = remote_head_result.stdout.strip()
    if local_head == remote_head:
        log_event(
            "INFO",
            f"skip {label}: no git updates found for {git_remote_ref} (HEAD {local_head[:12]})",
        )
        return False

    # Only pull/build when the configured remote ref is strictly ahead of local HEAD.
    # This avoids false positives when local is ahead or the branches have diverged.
    remote_ahead_check = run_repo_command(
        job,
        "git merge-base --is-ancestor "
        + shlex.quote(local_head)
        + " "
        + shlex.quote(git_remote_ref),
    )
    if remote_ahead_check.returncode != 0:
        local_ahead_check = run_repo_command(
            job,
            "git merge-base --is-ancestor "
            + shlex.quote(git_remote_ref)
            + " "
            + shlex.quote(local_head),
        )
        if local_ahead_check.returncode == 0:
            log_event(
                "INFO",
                f"skip {label}: local HEAD is ahead of {git_remote_ref}; no pull-triggered build",
            )
        else:
            log_event(
                "ERROR",
                f"skip {label}: local HEAD and {git_remote_ref} have diverged",
            )
        return False

    pull_result = run_repo_command(job, git_pull)
    if pull_result.returncode != 0:
        msg = pull_result.stderr.strip() or pull_result.stdout.strip() or "git pull failed"
        if strict:
            raise RuntimeError(f"{label} {msg}")
        log_event("ERROR", f"skip {label}: git pull failed ({msg})")
        return False

    local_head_after_result = run_repo_command(job, "git rev-parse HEAD")
    if local_head_after_result.returncode != 0:
        msg = (
            local_head_after_result.stderr.strip()
            or local_head_after_result.stdout.strip()
            or "git rev-parse HEAD after pull failed"
        )
        if strict:
            raise RuntimeError(f"{label} {msg}")
        log_event("ERROR", f"skip {label}: failed to read local HEAD after pull ({msg})")
        return False

    local_head_after = local_head_after_result.stdout.strip()
    if local_head_after == local_head:
        log_event(
            "INFO",
            f"skip {label}: git pull completed but HEAD did not advance",
        )
        return False

    log_event(
        "INFO",
        f"{label} updates found and pulled successfully ({local_head[:12]} -> {local_head_after[:12]})",
    )
    return True


def get_pueue_status() -> Dict[str, Any]:
    result = subprocess.run(
        ["pueue", "status", "-j"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    return json.loads(result.stdout)


def extract_task_state(task: Dict[str, Any]) -> str:
    status = task.get("status")
    if isinstance(status, str):
        return status
    if isinstance(status, dict) and status:
        return str(next(iter(status.keys())))
    return "Unknown"


def normalize_task_result(result: Any) -> Optional[str]:
    if result is None:
        return None
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if "Success" in result:
            return "Success"
        if "Failed" in result:
            return f"Failed({result['Failed']})"
        if len(result) == 1:
            key, value = next(iter(result.items()))
            if value in (None, ""):
                return str(key)
            return f"{key}({value})"
        return json.dumps(result, sort_keys=True)
    if isinstance(result, (list, tuple)):
        return json.dumps(result)
    return str(result)


def extract_done_result(task: Dict[str, Any]) -> Optional[str]:
    status = task.get("status")
    if isinstance(status, dict) and status:
        state, state_data = next(iter(status.items()))
        if state == "Done" and isinstance(state_data, dict):
            result = normalize_task_result(state_data.get("result"))
            if result is not None:
                return result

    return normalize_task_result(task.get("result"))


def queue_job(job: Dict[str, Any], group: str, dry_run: bool) -> Optional[int]:
    script = generate_build_script(job)
    cmd = [
        "pueue",
        "add",
        "-g",
        group,
        "env",
        "-u",
        "BASH_ENV",
        "-u",
        "ENV",
        "bash",
        "--noprofile",
        "--norc",
        "-lc",
        script,
    ]
    if dry_run:
        print("DRY RUN:", " ".join(shlex.quote(p) for p in cmd))
        return None

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    output = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
    task_id: Optional[int] = None

    match = re.search(r"\(id\s+(\d+)\)", output)
    if match:
        task_id = int(match.group(1))

    if task_id is not None:
        log_event(
            "INFO",
            f"queued [{job['name']}] in group {group} as task {task_id}: {output}",
        )
    else:
        log_event("INFO", f"queued [{job['name']}] in group {group}: {output}")

    return task_id


def log_finished_task_outcomes(
    status_data: Dict[str, Any], tracked_tasks: Dict[int, Dict[str, str]]
) -> None:
    tasks = status_data.get("tasks", {})
    pending_states = {"Queued", "Running", "Paused", "Stashed", "Locked"}

    for task_id, task_meta in list(tracked_tasks.items()):
        task = tasks.get(str(task_id)) or tasks.get(task_id)
        if not isinstance(task, dict):
            continue

        state = extract_task_state(task)
        if state in pending_states:
            continue

        job_name = task_meta["job_name"]
        done_result = extract_done_result(task)
        if state == "Done" and done_result == "Success":
            log_event("INFO", f"outcome [{job_name}]: task {task_id} completed successfully")
            manual_install_cmd = task_meta.get("manual_install_cmd", "").strip()
            if manual_install_cmd:
                log_event(
                    "ACTION",
                    f"manual step required for [{job_name}]: run '{manual_install_cmd}'",
                )
        elif state == "Done":
            log_event(
                "ERROR",
                f"outcome [{job_name}]: task {task_id} completed with non-success result {done_result!r}. Check 'pueue log {task_id}'",
            )
        else:
            log_event(
                "ERROR",
                f"outcome [{job_name}]: task {task_id} finished with state {state}. Check 'pueue log {task_id}'",
            )

        tracked_tasks.pop(task_id, None)


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
    force_run: bool,
) -> int:
    if not jobs:
        print("No active jobs found.")
        return 0

    shared_group = group_prefix
    ensure_pueue_group(shared_group, dry_run=dry_run)

    now = time.time()
    next_runs: Dict[str, float] = {}
    for job in jobs:
        if run_once and force_run:
            next_runs[job["slug"]] = now
        elif job.get("at"):
            next_runs[job["slug"]] = next_daily_at_timestamp(str(job["at"]), now)
        else:
            next_runs[job["slug"]] = now
    tracked_tasks: Dict[int, Dict[str, str]] = {}

    while True:
        if not dry_run:
            status_data = get_pueue_status()
            log_finished_task_outcomes(status_data, tracked_tasks)

        loop_now = time.time()
        for job in jobs:
            if loop_now < next_runs[job["slug"]]:
                continue

            try:
                if prepare_repo_for_build(job, dry_run=dry_run, force_run=force_run):
                    task_id = queue_job(job, group=shared_group, dry_run=dry_run)
                    if task_id is not None:
                        tracked_tasks[task_id] = {
                            "job_name": str(job["name"]),
                            "manual_install_cmd": str(job.get("manual_install_cmd", "")),
                        }
                    if job.get("at"):
                        next_runs[job["slug"]] = next_daily_at_timestamp(
                            str(job["at"]), loop_now + 1
                        )
                    else:
                        next_runs[job["slug"]] = loop_now + int(job["interval"])
                    # In continuous mode we queue at most one build per pass.
                    if not run_once:
                        break

                if job.get("at"):
                    next_runs[job["slug"]] = next_daily_at_timestamp(
                        str(job["at"]), loop_now + 1
                    )
                else:
                    next_runs[job["slug"]] = loop_now + int(job["interval"])
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                label = "[" + str(job.get("name", job.get("slug", "job"))) + "]"
                log_event("ERROR", f"skip {label}: scheduler recovered from job error ({exc})")
                if job.get("at"):
                    next_runs[job["slug"]] = next_daily_at_timestamp(
                        str(job["at"]), loop_now + 1
                    )
                else:
                    next_runs[job["slug"]] = loop_now + int(job["interval"])

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
        help="Shared pueue group name for all builds (default: gfff)",
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Queue runs even when git has no updates (useful with --once)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
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
            force_run=args.force,
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
    raise SystemExit(main())
