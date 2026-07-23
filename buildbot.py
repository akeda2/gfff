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


CONFIG_FILENAME = "gfff.yaml"
USER_CONFIG_PATH = Path(".config/gfff/gfff.yaml")
DEV_REPO_CONFIG_PATH = Path("dev/gfff/gfff.yaml")
USER_SERVICE_PATH = Path(".config/systemd/user/gfff-buildbot.service")
REPO_SERVICE_FILE = "gfff-buildbot.service"


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


def parse_config_path_from_service(service_path: Path) -> Optional[Path]:
    if not service_path.is_file():
        return None

    try:
        lines = service_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for raw_line in lines:
        line = raw_line.strip()
        if not line.startswith("ExecStart="):
            continue

        command = line.split("=", 1)[1].strip()
        if not command:
            continue

        command = command.replace("%h", str(Path.home()))
        try:
            args = shlex.split(command)
        except ValueError:
            continue

        for idx, arg in enumerate(args):
            if arg == "--config" and idx + 1 < len(args):
                return Path(args[idx + 1]).expanduser().resolve()
            if arg.startswith("--config="):
                return Path(arg.split("=", 1)[1]).expanduser().resolve()

    return None


def infer_dev_fallback_config_path(explicit_path: Optional[Path] = None) -> Path:
    if explicit_path is not None:
        return explicit_path.resolve()

    home = Path.home()
    installed_service = (home / USER_SERVICE_PATH).resolve()
    service_cfg = parse_config_path_from_service(installed_service)
    if service_cfg is not None:
        return service_cfg

    repo_service = (Path(__file__).resolve().parent / REPO_SERVICE_FILE).resolve()
    repo_service_cfg = parse_config_path_from_service(repo_service)
    if repo_service_cfg is not None:
        return repo_service_cfg

    return (home / DEV_REPO_CONFIG_PATH).resolve()


def discover_default_config_paths(
    include_dev_fallback: bool = True,
    dev_fallback_config: Optional[Path] = None,
) -> List[Path]:
    cwd_config = (Path.cwd() / CONFIG_FILENAME).resolve()
    home = Path.home()
    user_config_dir = (home / USER_CONFIG_PATH.parent).resolve()
    user_primary_config = (home / USER_CONFIG_PATH).resolve()
    dev_config = infer_dev_fallback_config_path(explicit_path=dev_fallback_config)

    paths: List[Path] = []

    # Rule 1: current directory config, unless it is also the dev config path.
    if cwd_config.is_file() and (not include_dev_fallback or cwd_config != dev_config):
        paths.append(cwd_config)

    # Rule 2: local user configs.
    # gfff.yaml is loaded first, then any other *.yaml files in lexical order.
    user_candidates: List[Path] = []
    if user_primary_config.is_file():
        user_candidates.append(user_primary_config)

    if user_config_dir.is_dir():
        for candidate in sorted(user_config_dir.glob("*.yaml")):
            resolved = candidate.resolve()
            if resolved == user_primary_config:
                continue
            user_candidates.append(resolved)

    for user_config in user_candidates:
        if user_config == dev_config:
            # Keep dev fallback as the last source by design.
            continue
        if user_config not in paths:
            paths.append(user_config)

    # Rule 3: dev repo config (always considered last when present).
    if include_dev_fallback and dev_config.is_file() and dev_config not in paths:
        paths.append(dev_config)

    return paths


def merge_jobs_from_configs(config_paths: List[Path]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen_names: set[str] = set()

    for config_path in config_paths:
        jobs = load_yaml_config(config_path)
        for idx, job in enumerate(jobs, start=1):
            raw_name = str(job.get("name", "")).strip()
            dedupe_key = raw_name if raw_name else f"__unnamed__:{config_path}:{idx}"
            if dedupe_key in seen_names:
                log_event(
                    "INFO",
                    f"skip duplicate job name '{raw_name}' from {config_path}",
                )
                continue
            seen_names.add(dedupe_key)
            merged.append(job)

    return merged


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


def parse_command_steps(value: Any, field: str, job_name: str) -> List[str]:
    if value is None:
        return []

    if isinstance(value, str):
        step = value.strip()
        return [step] if step else []

    if isinstance(value, list):
        steps: List[str] = []
        for idx, item in enumerate(value, start=1):
            if not isinstance(item, str):
                raise ValueError(
                    f"Job '{job_name}' has invalid '{field}' item at position {idx}: {item!r}"
                )
            step = item.strip()
            if step:
                steps.append(step)
        return steps

    raise ValueError(f"Job '{job_name}' has invalid '{field}': {value!r}")


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
        cleanup_steps = parse_command_steps(job.get("cleanup", ""), "cleanup", name)
        pre_build_steps = parse_command_steps(job.get("pre-build", ""), "pre-build", name)
        post_build_steps = parse_command_steps(job.get("post-build", ""), "post-build", name)

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
                "cleanup_steps": cleanup_steps,
                "pre_build_steps": pre_build_steps,
                "post_build_steps": post_build_steps,
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


def set_group_parallelism(group: str, parallelism: int, dry_run: bool) -> None:
    cmd = ["pueue", "parallel", "-g", group, str(parallelism)]
    if dry_run:
        print("DRY RUN:", " ".join(shlex.quote(p) for p in cmd))
        return

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def generate_build_script(job: Dict[str, Any]) -> str:
    lines = ["set -e", f"cd {shlex.quote(job['path'])}"]

    cleanup_steps = parse_command_steps(
        job.get("cleanup_steps", job.get("cleanup", "")),
        "cleanup",
        str(job.get("name", "job")),
    )
    pre_build_steps = parse_command_steps(
        job.get("pre_build_steps", job.get("pre_build", "")),
        "pre-build",
        str(job.get("name", "job")),
    )
    post_build_steps = parse_command_steps(
        job.get("post_build_steps", job.get("post_build", "")),
        "post-build",
        str(job.get("name", "job")),
    )

    lines.extend(cleanup_steps)
    lines.extend(pre_build_steps)
    if job.get("test"):
        lines.append(str(job["test"]))
    if job.get("build"):
        lines.append(str(job["build"]))
    lines.extend(post_build_steps)

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
    cpu_threads = max(1, os.cpu_count() or 1)
    ensure_pueue_group(shared_group, dry_run=dry_run)
    set_group_parallelism(shared_group, parallelism=cpu_threads, dry_run=dry_run)
    log_event("INFO", f"configured pueue group '{shared_group}' parallelism to {cpu_threads}")

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


def filter_jobs_by_name(jobs: List[Dict[str, Any]], job_name: str) -> List[Dict[str, Any]]:
    target = job_name.strip()
    if not target:
        raise ValueError("Job name filter cannot be empty")
    return [job for job in jobs if str(job.get("name", "")).strip() == target]


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run periodic pueue build tasks from gfff.yaml"
    )
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help="Path to a gfff yaml config file. When omitted, auto-discovery is used.",
    )
    parser.add_argument(
        "-g",
        "--group-prefix",
        default="gfff",
        help="Shared pueue group name for all builds (default: gfff)",
    )
    parser.add_argument(
        "-t",
        "--tick",
        type=int,
        default=5,
        help="Scheduler polling interval in seconds (default: 5)",
    )
    parser.add_argument(
        "-o",
        "--once",
        action="store_true",
        help="Queue eligible jobs once and exit",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Print pueue commands without executing them",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Queue runs even when git has no updates (useful with --once)",
    )
    parser.add_argument(
        "--no-dev-fallback",
        action="store_true",
        help="Do not include the development repo config in default config discovery",
    )
    parser.add_argument(
        "--dev-fallback-config",
        default=None,
        help=(
            "Path to development fallback config used by default config discovery "
            "(default: auto-detected from service ExecStart --config, then ~/dev/gfff/gfff.yaml)"
        ),
    )
    parser.add_argument(
        "job_name",
        nargs="?",
        help="Only run the job with this exact config name",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        check_dependencies()
        config_paths: List[Path]
        if args.config:
            config_paths = [Path(args.config).expanduser().resolve()]
        else:
            dev_fallback_config = (
                Path(args.dev_fallback_config).expanduser().resolve()
                if args.dev_fallback_config
                else None
            )
            config_paths = discover_default_config_paths(
                include_dev_fallback=not args.no_dev_fallback,
                dev_fallback_config=dev_fallback_config,
            )
            if not config_paths:
                raise RuntimeError(
                    "No config found. Searched: ./gfff.yaml, ~/.config/gfff/gfff.yaml"
                    + (
                        ""
                        if args.no_dev_fallback
                        else ", "
                        + str(
                            infer_dev_fallback_config_path(explicit_path=dev_fallback_config)
                        )
                    )
                )

        for config_path in config_paths:
            log_event("INFO", f"using config: {config_path}")

        jobs = normalize_jobs(merge_jobs_from_configs(config_paths))
        if args.job_name:
            jobs = filter_jobs_by_name(jobs, args.job_name)
            if not jobs:
                raise RuntimeError(
                    f"No active job matched name: {args.job_name}. "
                    "Config files were loaded in normal discovery order."
                )
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
