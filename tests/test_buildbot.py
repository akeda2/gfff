import io
import datetime as dt
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import buildbot
from buildbot import (
    CONFIG_FILENAME,
    check_dependencies,
    disable_job_in_source_config,
    discover_default_config_paths,
    extract_done_result,
    extract_task_state,
    filter_jobs_by_name,
    generate_build_script,
    load_yaml_config,
    log_finished_task_outcomes,
    merge_jobs_from_configs,
    normalize_jobs,
    next_run_for_daily_job,
    normalize_task_result,
    next_daily_at_timestamp,
    is_job_mode_eligible,
    parse_run_mode,
    parse_config_path_from_service,
    parse_bool,
    parse_command_steps,
    parse_at_time,
    parse_args,
    prepare_repo_for_build,
    queue_job,
    resolve_executable,
    run_loop,
    sanitize_name,
)


def cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["cmd"], returncode=returncode, stdout=stdout, stderr=stderr)


class NormalizeJobsTests(unittest.TestCase):
    def test_allows_build_only_job(self) -> None:
        jobs = normalize_jobs(
            [
                {
                    "name": "build-only",
                    "active": True,
                    "path": "~/repo",
                    "build": "make",
                    "interval": 60,
                }
            ]
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["build_steps"], ["make"])
        self.assertEqual(jobs[0]["test_steps"], [])

    def test_allows_test_only_job(self) -> None:
        jobs = normalize_jobs(
            [
                {
                    "name": "test-only",
                    "active": True,
                    "path": "~/repo",
                    "test": "pytest -q",
                    "interval": 60,
                }
            ]
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["build_steps"], [])
        self.assertEqual(jobs[0]["test_steps"], ["pytest -q"])

    def test_allows_list_test_and_build_commands(self) -> None:
        jobs = normalize_jobs(
            [
                {
                    "name": "multi-test-build",
                    "active": True,
                    "path": "~/repo",
                    "test": ["echo t1", "echo t2"],
                    "build": ["echo b1", "echo b2"],
                    "interval": 60,
                }
            ]
        )

        self.assertEqual(jobs[0]["test_steps"], ["echo t1", "echo t2"])
        self.assertEqual(jobs[0]["build_steps"], ["echo b1", "echo b2"])

    def test_rejects_job_without_test_and_build(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            normalize_jobs(
                [
                    {
                        "name": "invalid",
                        "active": True,
                        "path": "~/repo",
                        "interval": 60,
                    }
                ]
            )

        self.assertIn("at least one of 'build' or 'test'", str(ctx.exception))

    def test_rejects_invalid_interval(self) -> None:
        with self.assertRaises(ValueError):
            normalize_jobs(
                [
                    {
                        "name": "invalid",
                        "active": True,
                        "path": "~/repo",
                        "build": "make",
                        "interval": "oops",
                    }
                ]
            )

    def test_allows_at_without_interval(self) -> None:
        jobs = normalize_jobs(
            [
                {
                    "name": "daily",
                    "active": True,
                    "path": "~/repo",
                    "test": "pytest -q",
                    "at": "05:00",
                }
            ]
        )
        self.assertEqual(jobs[0]["at"], "05:00")
        self.assertIsNone(jobs[0]["interval"])

    def test_rejects_both_interval_and_at(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            normalize_jobs(
                [
                    {
                        "name": "invalid",
                        "active": True,
                        "path": "~/repo",
                        "build": "make",
                        "interval": 60,
                        "at": "05:00",
                    }
                ]
            )
        self.assertIn("only one of 'interval' or 'at'", str(ctx.exception))

    def test_rejects_missing_interval_and_at(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            normalize_jobs(
                [
                    {
                        "name": "invalid",
                        "active": True,
                        "path": "~/repo",
                        "build": "make",
                    }
                ]
            )
        self.assertIn("must set one of 'interval' or 'at'", str(ctx.exception))

    def test_skips_inactive_jobs(self) -> None:
        jobs = normalize_jobs(
            [
                {"name": "off", "active": False, "path": "~/repo", "build": "make", "interval": 60},
                {"name": "on", "active": True, "path": "~/repo", "build": "make", "interval": 60},
            ]
        )
        self.assertEqual([job["name"] for job in jobs], ["on"])

    def test_sets_defaults_for_git_options(self) -> None:
        jobs = normalize_jobs(
            [
                {
                    "name": "defaults",
                    "active": True,
                    "path": "~/repo",
                    "build": "make",
                    "interval": 60,
                }
            ]
        )
        self.assertEqual(jobs[0]["git_pull"], "git pull --ff-only")
        self.assertEqual(jobs[0]["git_remote_ref"], "@{u}")
        self.assertTrue(jobs[0]["git_strict"])

    def test_supports_pre_and_post_build_as_lists(self) -> None:
        jobs = normalize_jobs(
            [
                {
                    "name": "multi-steps",
                    "active": True,
                    "path": "~/repo",
                    "build": "make",
                    "interval": 60,
                    "pre-build": ["echo prep1", "echo prep2"],
                    "post-build": ["echo post1", "echo post2"],
                }
            ]
        )

        self.assertEqual(jobs[0]["pre_build_steps"], ["echo prep1", "echo prep2"])
        self.assertEqual(jobs[0]["post_build_steps"], ["echo post1", "echo post2"])

    def test_defaults_run_mode_to_normal(self) -> None:
        jobs = normalize_jobs(
            [
                {
                    "name": "defaults",
                    "active": True,
                    "path": "~/repo",
                    "build": "make",
                    "interval": 60,
                }
            ]
        )

        self.assertEqual(jobs[0]["run_mode"], "normal")

    def test_accepts_manual_and_scheduled_run_modes(self) -> None:
        jobs = normalize_jobs(
            [
                {
                    "name": "manual-only",
                    "active": True,
                    "path": "~/repo",
                    "build": "make",
                    "interval": 60,
                    "run-mode": "manual",
                },
                {
                    "name": "scheduled-only",
                    "active": True,
                    "path": "~/repo",
                    "build": "make",
                    "at": "05:00",
                    "run-mode": "scheduled",
                },
            ]
        )

        self.assertEqual(jobs[0]["run_mode"], "manual")
        self.assertEqual(jobs[1]["run_mode"], "scheduled")

    def test_rejects_invalid_run_mode(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            normalize_jobs(
                [
                    {
                        "name": "invalid",
                        "active": True,
                        "path": "~/repo",
                        "build": "make",
                        "interval": 60,
                        "run-mode": "nightly",
                    }
                ]
            )

        self.assertIn("invalid 'run-mode'", str(ctx.exception))

    def test_disable_when_run_defaults_to_false(self) -> None:
        jobs = normalize_jobs(
            [
                {
                    "name": "defaults",
                    "active": True,
                    "path": "~/repo",
                    "build": "make",
                    "interval": 60,
                }
            ]
        )

        self.assertFalse(jobs[0]["disable_when_run"])

    def test_disable_when_run_accepts_true(self) -> None:
        jobs = normalize_jobs(
            [
                {
                    "name": "one-shot",
                    "active": True,
                    "path": "~/repo",
                    "test": "pytest -q",
                    "interval": 60,
                    "disable-when-run": True,
                }
            ]
        )

        self.assertTrue(jobs[0]["disable_when_run"])


class GenerateBuildScriptTests(unittest.TestCase):
    def test_runs_test_before_build(self) -> None:
        job = {
            "path": "~/repo",
            "cleanup": "echo cleanup",
            "pre_build": "echo pre",
            "test": "pytest -q",
            "build": "make",
            "post_build": "echo post",
        }

        script = generate_build_script(job)
        lines = script.splitlines()

        self.assertEqual(lines[0], "set -e")
        self.assertEqual(lines[1], "cd '~/repo'")
        self.assertEqual(lines[2:], [
            "echo cleanup",
            "echo pre",
            "pytest -q",
            "make",
            "echo post",
        ])

    def test_supports_test_only_script(self) -> None:
        job = {
            "path": "~/repo",
            "cleanup": "",
            "pre_build": "",
            "test": "pytest -q",
            "build": "",
            "post_build": "",
        }

        script = generate_build_script(job)
        self.assertEqual(script.splitlines(), ["set -e", "cd '~/repo'", "pytest -q"])

    def test_supports_multiple_pre_and_post_build_steps(self) -> None:
        job = {
            "path": "~/repo",
            "cleanup_steps": ["echo cleanup1", "echo cleanup2"],
            "pre_build_steps": ["echo pre1", "echo pre2"],
            "test_steps": ["pytest -q"],
            "build_steps": ["make"],
            "post_build_steps": ["echo post1", "echo post2"],
        }

        script = generate_build_script(job)
        self.assertEqual(
            script.splitlines(),
            [
                "set -e",
                "cd '~/repo'",
                "echo cleanup1",
                "echo cleanup2",
                "echo pre1",
                "echo pre2",
                "pytest -q",
                "make",
                "echo post1",
                "echo post2",
            ],
        )

    def test_supports_multiple_test_and_build_steps(self) -> None:
        job = {
            "path": "~/repo",
            "cleanup_steps": [],
            "pre_build_steps": [],
            "test": ["echo test1", "echo test2"],
            "build": ["echo build1", "echo build2"],
            "post_build_steps": [],
        }

        script = generate_build_script(job)
        self.assertEqual(
            script.splitlines(),
            [
                "set -e",
                "cd '~/repo'",
                "echo test1",
                "echo test2",
                "echo build1",
                "echo build2",
            ],
        )

    def test_disable_when_run_is_not_injected_into_script(self) -> None:
        job = {
            "name": "pueue-restart",
            "path": "~/repo",
            "cleanup_steps": [],
            "pre_build_steps": ["echo pre"],
            "test": "true",
            "build": "",
            "post_build_steps": [],
            "disable_when_run": True,
            "source_config_path": "/tmp/pueue.yaml",
        }

        script = generate_build_script(job)
        self.assertNotIn("disable-when-run", script)
        self.assertEqual(script.splitlines()[2], "echo pre")


class PrimitiveFunctionTests(unittest.TestCase):
    def test_parse_bool_accepts_strings_and_bools(self) -> None:
        self.assertTrue(parse_bool(True, "f", "j"))
        self.assertTrue(parse_bool("yes", "f", "j"))
        self.assertFalse(parse_bool(False, "f", "j"))
        self.assertFalse(parse_bool("off", "f", "j"))

    def test_parse_bool_rejects_invalid(self) -> None:
        with self.assertRaises(ValueError):
            parse_bool("maybe", "git-strict", "job")

    def test_sanitize_name(self) -> None:
        self.assertEqual(sanitize_name("  hello world  "), "hello-world")
        self.assertEqual(sanitize_name("***"), "job")

    def test_parse_at_time(self) -> None:
        self.assertEqual(parse_at_time("05:00", "job"), "05:00")
        self.assertEqual(parse_at_time(685, "job"), "11:25")
        with self.assertRaises(ValueError):
            parse_at_time("5:00", "job")
        with self.assertRaises(ValueError):
            parse_at_time("24:00", "job")

    def test_parse_command_steps(self) -> None:
        self.assertEqual(parse_command_steps("echo hi", "pre-build", "job"), ["echo hi"])
        self.assertEqual(
            parse_command_steps(["echo one", "  ", "echo two"], "pre-build", "job"),
            ["echo one", "echo two"],
        )
        with self.assertRaises(ValueError):
            parse_command_steps(["ok", 1], "pre-build", "job")

    def test_parse_run_mode(self) -> None:
        self.assertEqual(parse_run_mode(None, "job"), "normal")
        self.assertEqual(parse_run_mode("manual", "job"), "manual")
        self.assertEqual(parse_run_mode("SCHEDULED", "job"), "scheduled")
        with self.assertRaises(ValueError):
            parse_run_mode("bad", "job")

    def test_disable_job_in_source_config_flips_active_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "cfg.yaml"
            config_path.write_text(
                                """- name: other
    active: true
    interval: 60
- name: target
    active: true
    interval: 60
""",
                encoding="utf-8",
            )
            disable_job_in_source_config(
                {
                    "name": "target",
                    "source_config_path": str(config_path),
                },
                dry_run=False,
            )

            text = config_path.read_text(encoding="utf-8")
            self.assertRegex(
                text,
                r"- name: target\n[ \t]*active:[ \t]*false",
            )
            self.assertRegex(
                text,
                r"- name: other\n[ \t]*active:[ \t]*true",
            )

    def test_disable_job_in_source_config_dry_run_keeps_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "cfg.yaml"
            original = """- name: target\n  active: true\n  interval: 60\n"""
            config_path.write_text(original, encoding="utf-8")

            disable_job_in_source_config(
                {
                    "name": "target",
                    "source_config_path": str(config_path),
                },
                dry_run=True,
            )

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_is_job_mode_eligible(self) -> None:
        self.assertTrue(is_job_mode_eligible({"run_mode": "normal"}, run_once=True))
        self.assertTrue(is_job_mode_eligible({"run_mode": "normal"}, run_once=False))
        self.assertTrue(is_job_mode_eligible({"run_mode": "manual"}, run_once=True))
        self.assertFalse(is_job_mode_eligible({"run_mode": "manual"}, run_once=False))
        self.assertFalse(is_job_mode_eligible({"run_mode": "scheduled"}, run_once=True))
        self.assertTrue(
            is_job_mode_eligible(
                {"run_mode": "scheduled"},
                run_once=True,
                allow_scheduled_in_once=True,
            )
        )
        self.assertTrue(is_job_mode_eligible({"run_mode": "scheduled"}, run_once=False))

    def test_next_daily_at_timestamp(self) -> None:
        now = dt.datetime(2026, 1, 2, 4, 30, 0).timestamp()
        next_ts = next_daily_at_timestamp("05:00", now)
        expected = dt.datetime(2026, 1, 2, 5, 0, 0).timestamp()
        self.assertEqual(next_ts, expected)

        now_after = dt.datetime(2026, 1, 2, 5, 0, 1).timestamp()
        next_after_ts = next_daily_at_timestamp("05:00", now_after)
        expected_next_day = dt.datetime(2026, 1, 3, 5, 0, 0).timestamp()
        self.assertEqual(next_after_ts, expected_next_day)

    def test_next_run_for_daily_job_catches_up_within_window(self) -> None:
        now = dt.datetime(2026, 1, 2, 11, 58, 42).timestamp()
        next_ts = next_run_for_daily_job("11:58", now, catch_up_window_s=60)
        self.assertEqual(next_ts, now)

    def test_next_run_for_daily_job_outside_window_goes_to_next_day(self) -> None:
        now = dt.datetime(2026, 1, 2, 11, 58, 42).timestamp()
        next_ts = next_run_for_daily_job("11:58", now, catch_up_window_s=30)
        expected = dt.datetime(2026, 1, 3, 11, 58, 0).timestamp()
        self.assertEqual(next_ts, expected)

    def test_resolve_executable_uses_fallback_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cargo_bin = home / ".cargo" / "bin"
            cargo_bin.mkdir(parents=True, exist_ok=True)
            pueue = cargo_bin / "pueue"
            pueue.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            pueue.chmod(0o755)

            with patch.object(buildbot, "_PUEUE_CMD_CACHE", None):
                with patch.object(buildbot.Path, "home", return_value=home):
                    with patch.object(buildbot.shutil, "which", return_value=None):
                        resolved = resolve_executable("pueue")

            self.assertEqual(resolved, str(pueue))

    def test_check_dependencies_raises_with_fallback_paths_in_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.object(buildbot, "_PUEUE_CMD_CACHE", None):
                with patch.object(buildbot.Path, "home", return_value=home):
                    with patch.object(buildbot.shutil, "which", return_value=None):
                        with self.assertRaises(RuntimeError) as ctx:
                            check_dependencies()

        self.assertIn("fallback locations", str(ctx.exception))

    def test_check_dependencies_logs_resolved_pueue_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cargo_bin = home / ".cargo" / "bin"
            cargo_bin.mkdir(parents=True, exist_ok=True)
            pueue = cargo_bin / "pueue"
            pueue.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            pueue.chmod(0o755)

            def which_side_effect(binary: str):
                if binary == "pueue":
                    return None
                if binary == "git":
                    return "/usr/bin/git"
                return None

            with patch.object(buildbot, "_PUEUE_CMD_CACHE", None):
                with patch.object(buildbot.Path, "home", return_value=home):
                    with patch.object(buildbot.shutil, "which", side_effect=which_side_effect):
                        with patch.object(buildbot, "log_event") as log_mock:
                            check_dependencies()

        log_mock.assert_any_call("INFO", f"using pueue executable: {str(pueue)}")


class LoadYamlConfigTests(unittest.TestCase):
    def test_rejects_non_list_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("name: not-a-list\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_yaml_config(path)

    def test_reads_valid_yaml_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.yaml"
            path.write_text("- name: x\n  active: true\n", encoding="utf-8")
            data = load_yaml_config(path)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["name"], "x")

    def test_reports_invalid_yaml_with_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-syntax.yaml"
            path.write_text(
                "- name: bad\n"
                "  active: true\n"
                "  at: \"11:35\"\"\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                load_yaml_config(path)

        msg = str(ctx.exception)
        self.assertIn("Invalid YAML", msg)
        self.assertIn("line", msg)
        self.assertIn("column", msg)


class ConfigDiscoveryTests(unittest.TestCase):
    def test_discovery_order_cwd_then_user_then_dev(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cwd = Path(tmp) / "cwd"
            home.mkdir(parents=True, exist_ok=True)
            cwd.mkdir(parents=True, exist_ok=True)

            cwd_config = cwd / CONFIG_FILENAME
            cwd_config.write_text("[]\n", encoding="utf-8")

            user_config = home / ".config" / "gfff" / "gfff.yaml"
            user_config.parent.mkdir(parents=True, exist_ok=True)
            user_config.write_text("[]\n", encoding="utf-8")

            dev_config = home / "dev" / "gfff" / "gfff.yaml"
            dev_config.parent.mkdir(parents=True, exist_ok=True)
            dev_config.write_text("[]\n", encoding="utf-8")

            with patch.object(buildbot.Path, "home", return_value=home):
                with patch.object(buildbot.Path, "cwd", return_value=cwd):
                    paths = discover_default_config_paths()

            self.assertEqual(paths, [cwd_config.resolve(), user_config.resolve(), dev_config.resolve()])

    def test_dev_config_stays_last_when_cwd_is_dev_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir(parents=True, exist_ok=True)

            user_config = home / ".config" / "gfff" / "gfff.yaml"
            user_config.parent.mkdir(parents=True, exist_ok=True)
            user_config.write_text("[]\n", encoding="utf-8")

            dev_config = home / "dev" / "gfff" / "gfff.yaml"
            dev_config.parent.mkdir(parents=True, exist_ok=True)
            dev_config.write_text("[]\n", encoding="utf-8")

            with patch.object(buildbot.Path, "home", return_value=home):
                with patch.object(buildbot.Path, "cwd", return_value=dev_config.parent):
                    paths = discover_default_config_paths()

            self.assertEqual(paths, [user_config.resolve(), dev_config.resolve()])

    def test_no_dev_fallback_excludes_dev_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cwd = Path(tmp) / "cwd"
            home.mkdir(parents=True, exist_ok=True)
            cwd.mkdir(parents=True, exist_ok=True)

            cwd_config = cwd / CONFIG_FILENAME
            cwd_config.write_text("[]\n", encoding="utf-8")

            user_config = home / ".config" / "gfff" / "gfff.yaml"
            user_config.parent.mkdir(parents=True, exist_ok=True)
            user_config.write_text("[]\n", encoding="utf-8")

            dev_config = home / "dev" / "gfff" / "gfff.yaml"
            dev_config.parent.mkdir(parents=True, exist_ok=True)
            dev_config.write_text("[]\n", encoding="utf-8")

            with patch.object(buildbot.Path, "home", return_value=home):
                with patch.object(buildbot.Path, "cwd", return_value=cwd):
                    paths = discover_default_config_paths(include_dev_fallback=False)

            self.assertEqual(paths, [cwd_config.resolve(), user_config.resolve()])

    def test_custom_dev_fallback_path_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cwd = Path(tmp) / "cwd"
            home.mkdir(parents=True, exist_ok=True)
            cwd.mkdir(parents=True, exist_ok=True)

            user_config = home / ".config" / "gfff" / "gfff.yaml"
            user_config.parent.mkdir(parents=True, exist_ok=True)
            user_config.write_text("[]\n", encoding="utf-8")

            custom_dev = Path(tmp) / "alt" / "repo" / "gfff.yaml"
            custom_dev.parent.mkdir(parents=True, exist_ok=True)
            custom_dev.write_text("[]\n", encoding="utf-8")

            with patch.object(buildbot.Path, "home", return_value=home):
                with patch.object(buildbot.Path, "cwd", return_value=cwd):
                    paths = discover_default_config_paths(dev_fallback_config=custom_dev)

            self.assertEqual(paths, [user_config.resolve(), custom_dev.resolve()])

    def test_parse_config_path_from_service_execstart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = Path(tmp) / "gfff-buildbot.service"
            service.write_text(
                "[Service]\n"
                "ExecStart=/usr/bin/gfff-buildbot --config /opt/custom/gfff.yaml --once\n",
                encoding="utf-8",
            )

            config_path = parse_config_path_from_service(service)

        self.assertEqual(config_path, Path("/opt/custom/gfff.yaml").resolve())

    def test_parse_config_path_from_service_percent_h(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir(parents=True, exist_ok=True)
            service = Path(tmp) / "gfff-buildbot.service"
            service.write_text(
                "[Service]\n"
                "ExecStart=%h/.local/share/gfff-buildbot/.venv/bin/gfff-buildbot --config %h/dev/alt/gfff.yaml\n",
                encoding="utf-8",
            )

            with patch.object(buildbot.Path, "home", return_value=home):
                config_path = parse_config_path_from_service(service)

        self.assertEqual(config_path, (home / "dev/alt/gfff.yaml").resolve())

    def test_repo_service_does_not_pin_config_path(self) -> None:
        service = Path(__file__).resolve().parent.parent / "gfff-buildbot.service"
        config_path = parse_config_path_from_service(service)
        self.assertIsNone(config_path)

    def test_discovery_uses_installed_service_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cwd = Path(tmp) / "cwd"
            home.mkdir(parents=True, exist_ok=True)
            cwd.mkdir(parents=True, exist_ok=True)

            user_config = home / ".config" / "gfff" / "gfff.yaml"
            user_config.parent.mkdir(parents=True, exist_ok=True)
            user_config.write_text("[]\n", encoding="utf-8")

            service_cfg = home / "repos" / "bot" / "gfff.yaml"
            service_cfg.parent.mkdir(parents=True, exist_ok=True)
            service_cfg.write_text("[]\n", encoding="utf-8")

            service_file = home / ".config" / "systemd" / "user" / "gfff-buildbot.service"
            service_file.parent.mkdir(parents=True, exist_ok=True)
            service_file.write_text(
                "[Service]\n"
                f"ExecStart=/usr/bin/gfff-buildbot --config {service_cfg}\n",
                encoding="utf-8",
            )

            with patch.object(buildbot.Path, "home", return_value=home):
                with patch.object(buildbot.Path, "cwd", return_value=cwd):
                    paths = discover_default_config_paths()

            self.assertEqual(paths, [user_config.resolve(), service_cfg.resolve()])

    def test_user_config_directory_loads_gfff_then_sorted_yaml_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cwd = Path(tmp) / "cwd"
            home.mkdir(parents=True, exist_ok=True)
            cwd.mkdir(parents=True, exist_ok=True)

            user_dir = home / ".config" / "gfff"
            user_dir.mkdir(parents=True, exist_ok=True)

            primary = user_dir / "gfff.yaml"
            first = user_dir / "10firstlist.yaml"
            second = user_dir / "30secondlist.yaml"
            ignored = user_dir / "notes.txt"

            primary.write_text("[]\n", encoding="utf-8")
            first.write_text("[]\n", encoding="utf-8")
            second.write_text("[]\n", encoding="utf-8")
            ignored.write_text("ignore\n", encoding="utf-8")

            with patch.object(buildbot.Path, "home", return_value=home):
                with patch.object(buildbot.Path, "cwd", return_value=cwd):
                    paths = discover_default_config_paths(include_dev_fallback=False)

            self.assertEqual(paths, [
                primary.resolve(),
                first.resolve(),
                second.resolve(),
            ])


class ConfigMergeTests(unittest.TestCase):
    def test_later_duplicate_names_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.yaml"
            second = Path(tmp) / "b.yaml"

            first.write_text(
                "- name: same\n  active: true\n  path: ~/repo/a\n  build: make\n  interval: 60\n",
                encoding="utf-8",
            )
            second.write_text(
                "- name: same\n  active: true\n  path: ~/repo/b\n  build: make\n  interval: 60\n",
                encoding="utf-8",
            )

            merged = merge_jobs_from_configs([first, second])
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["path"], "~/repo/a")


class TaskResultHelpersTests(unittest.TestCase):
    def test_extract_task_state_handles_string_dict_and_unknown(self) -> None:
        self.assertEqual(extract_task_state({"status": "Running"}), "Running")
        self.assertEqual(extract_task_state({"status": {"Done": {}}}), "Done")
        self.assertEqual(extract_task_state({}), "Unknown")

    def test_normalize_task_result(self) -> None:
        self.assertIsNone(normalize_task_result(None))
        self.assertEqual(normalize_task_result("Success"), "Success")
        self.assertEqual(normalize_task_result({"Success": None}), "Success")
        self.assertEqual(normalize_task_result({"Failed": 2}), "Failed(2)")
        self.assertEqual(normalize_task_result({"Other": "x"}), "Other(x)")

    def test_extract_done_result(self) -> None:
        task = {"status": {"Done": {"result": {"Success": None}}}}
        self.assertEqual(extract_done_result(task), "Success")


class QueueJobTests(unittest.TestCase):
    def test_queue_job_parses_task_id(self) -> None:
        job = {
            "name": "repo",
            "path": "~/repo",
            "cleanup": "",
            "pre_build": "",
            "test": "",
            "build": "make",
            "post_build": "",
        }
        with patch.object(buildbot.subprocess, "run", return_value=cp(stdout="Queued as (id 42)")) as run_mock:
            with patch.object(buildbot, "log_event") as log_mock:
                task_id = queue_job(job, group="gfff", dry_run=False)
        self.assertEqual(task_id, 42)
        self.assertTrue(log_mock.called)
        run_args = run_mock.call_args.args[0]
        self.assertIn("-l", run_args)
        self.assertEqual(run_args[run_args.index("-l") + 1], "repo")

    def test_queue_job_dry_run(self) -> None:
        job = {
            "name": "repo",
            "path": "~/repo",
            "cleanup": "",
            "pre_build": "",
            "test": "",
            "build": "make",
            "post_build": "",
        }
        output = io.StringIO()
        with redirect_stdout(output):
            task_id = queue_job(job, group="gfff", dry_run=True)
        self.assertIsNone(task_id)
        self.assertIn("DRY RUN:", output.getvalue())


class LogFinishedTaskOutcomesTests(unittest.TestCase):
    def test_logs_success_and_manual_action(self) -> None:
        status = {
            "tasks": {
                "7": {"status": {"Done": {"result": {"Success": None}}}},
            }
        }
        tracked = {7: {"job_name": "repo", "manual_install_cmd": "sudo make install"}}
        with patch.object(buildbot, "log_event") as log_mock:
            log_finished_task_outcomes(status, tracked)
        self.assertNotIn(7, tracked)
        self.assertGreaterEqual(log_mock.call_count, 2)

    def test_logs_non_success_done_as_error(self) -> None:
        status = {
            "tasks": {
                "8": {"status": {"Done": {"result": {"Failed": 1}}}},
            }
        }
        tracked = {8: {"job_name": "repo", "manual_install_cmd": ""}}
        with patch.object(buildbot, "log_event") as log_mock:
            log_finished_task_outcomes(status, tracked)
        self.assertNotIn(8, tracked)
        error_calls = [call for call in log_mock.call_args_list if call.args[0] == "ERROR"]
        self.assertTrue(error_calls)


class PrepareRepoForBuildTests(unittest.TestCase):
    def test_returns_false_when_path_missing(self) -> None:
        job = {"name": "repo", "path": "/definitely/missing/path"}
        with patch.object(buildbot, "log_event") as log_mock:
            ok = prepare_repo_for_build(job, dry_run=False)
        self.assertFalse(ok)
        self.assertTrue(log_mock.called)

    def test_dry_run_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = {
                "name": "repo",
                "path": tmp,
                "git_pull": "git pull --ff-only",
                "git_remote_ref": "@{u}",
            }
            output = io.StringIO()
            with redirect_stdout(output):
                ok = prepare_repo_for_build(job, dry_run=True)
            self.assertTrue(ok)
            self.assertIn("DRY RUN:", output.getvalue())

    def test_returns_false_when_no_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = {"name": "repo", "path": tmp, "git_pull": "git pull --ff-only", "git_remote_ref": "@{u}"}
            side_effect = [
                cp(),
                cp(),
                cp(stdout="abc\n"),
                cp(stdout="abc\n"),
            ]
            with patch.object(buildbot, "run_repo_command", side_effect=side_effect):
                with patch.object(buildbot, "log_event") as log_mock:
                    ok = prepare_repo_for_build(job, dry_run=False)
            self.assertFalse(ok)
            info_calls = [call for call in log_mock.call_args_list if call.args[0] == "INFO"]
            self.assertTrue(info_calls)

    def test_returns_true_when_pull_advances_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = {"name": "repo", "path": tmp, "git_pull": "git pull --ff-only", "git_remote_ref": "@{u}"}
            side_effect = [
                cp(),
                cp(),
                cp(stdout="aaa\n"),
                cp(stdout="bbb\n"),
                cp(),
                cp(),
                cp(stdout="ccc\n"),
            ]
            with patch.object(buildbot, "run_repo_command", side_effect=side_effect):
                ok = prepare_repo_for_build(job, dry_run=False)
            self.assertTrue(ok)

    def test_returns_false_when_local_ahead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = {"name": "repo", "path": tmp, "git_pull": "git pull --ff-only", "git_remote_ref": "@{u}"}
            side_effect = [
                cp(),
                cp(),
                cp(stdout="bbb\n"),
                cp(stdout="aaa\n"),
                cp(returncode=1),
                cp(returncode=0),
            ]
            with patch.object(buildbot, "run_repo_command", side_effect=side_effect):
                ok = prepare_repo_for_build(job, dry_run=False)
            self.assertFalse(ok)

    def test_strict_fetch_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = {"name": "repo", "path": tmp, "git_strict": True}
            with patch.object(buildbot, "run_repo_command", return_value=cp(returncode=1, stderr="fetch failed")):
                with self.assertRaises(RuntimeError):
                    prepare_repo_for_build(job, dry_run=False)

    def test_non_strict_fetch_failure_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = {"name": "repo", "path": tmp, "git_strict": False}
            with patch.object(buildbot, "run_repo_command", return_value=cp(returncode=1, stderr="fetch failed")):
                with patch.object(buildbot, "log_event") as log_mock:
                    ok = prepare_repo_for_build(job, dry_run=False)
            self.assertFalse(ok)
            self.assertTrue(log_mock.called)

    def test_force_run_executes_git_fetch_and_pull(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = {"name": "repo", "path": tmp, "git_pull": "git pull --ff-only"}
            with patch.object(buildbot, "run_repo_command", side_effect=[cp(), cp()]) as run_repo_mock:
                ok = prepare_repo_for_build(job, dry_run=False, force_run=True)
            self.assertTrue(ok)
            self.assertEqual(run_repo_mock.call_count, 2)
            self.assertEqual(run_repo_mock.call_args_list[0].args[1], "git fetch")
            self.assertEqual(run_repo_mock.call_args_list[1].args[1], "git pull --ff-only")

    def test_force_run_non_strict_fetch_failure_still_runs_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = {
                "name": "repo",
                "path": tmp,
                "git_strict": False,
                "git_pull": "git pull --ff-only",
            }
            side_effect = [
                cp(returncode=1, stderr="fetch failed"),
                cp(),
            ]
            with patch.object(buildbot, "run_repo_command", side_effect=side_effect):
                ok = prepare_repo_for_build(job, dry_run=False, force_run=True)
            self.assertTrue(ok)

    def test_scheduled_daily_job_skips_git_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = {
                "name": "daily",
                "path": tmp,
                "run_mode": "scheduled",
                "at": "04:00",
            }
            with patch.object(buildbot, "run_repo_command") as run_repo_mock:
                ok = prepare_repo_for_build(job, dry_run=False)
            self.assertTrue(ok)
            run_repo_mock.assert_not_called()

    def test_normal_daily_job_still_checks_git_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = {
                "name": "daily-normal",
                "path": tmp,
                "run_mode": "normal",
                "at": "04:00",
                "git_pull": "git pull --ff-only",
                "git_remote_ref": "@{u}",
            }
            side_effect = [
                cp(),
                cp(),
                cp(stdout="abc\n"),
                cp(stdout="abc\n"),
            ]
            with patch.object(buildbot, "run_repo_command", side_effect=side_effect):
                ok = prepare_repo_for_build(job, dry_run=False)
            self.assertFalse(ok)


class ParseArgsTests(unittest.TestCase):
    def test_accepts_force_flag(self) -> None:
        args = parse_args(["--once", "--force"])
        self.assertTrue(args.once)
        self.assertTrue(args.force)

    def test_accepts_short_flags(self) -> None:
        args = parse_args(["-o", "-f", "-n", "-c", "cfg.yaml", "-g", "grp", "-t", "9"])
        self.assertTrue(args.once)
        self.assertTrue(args.force)
        self.assertTrue(args.dry_run)
        self.assertEqual(args.config, "cfg.yaml")
        self.assertEqual(args.group_prefix, "grp")
        self.assertEqual(args.tick, 9)

    def test_accepts_dev_fallback_flags(self) -> None:
        args = parse_args(["--no-dev-fallback", "--dev-fallback-config", "/tmp/dev-gfff.yaml"])
        self.assertTrue(args.no_dev_fallback)
        self.assertEqual(args.dev_fallback_config, "/tmp/dev-gfff.yaml")

    def test_accepts_positional_job_name(self) -> None:
        args = parse_args(["my-job"])
        self.assertEqual(args.job_name, "my-job")

    def test_accepts_check_flag(self) -> None:
        args = parse_args(["--check", "cfg.yaml"])
        self.assertEqual(args.check_config, "cfg.yaml")

    def test_accepts_import_flag(self) -> None:
        args = parse_args(["--import", "cfg.yaml"])
        self.assertEqual(args.import_config, "cfg.yaml")

    def test_accepts_check_short_alias(self) -> None:
        args = parse_args(["-C", "cfg.yaml"])
        self.assertEqual(args.check_config, "cfg.yaml")

    def test_accepts_import_short_alias(self) -> None:
        args = parse_args(["-I", "cfg.yaml"])
        self.assertEqual(args.import_config, "cfg.yaml")

    def test_accepts_overwrite_flag(self) -> None:
        args = parse_args(["--import", "cfg.yaml", "--overwrite"])
        self.assertTrue(args.overwrite)

    def test_accepts_overwrite_short_alias(self) -> None:
        args = parse_args(["--import", "cfg.yaml", "-w"])
        self.assertTrue(args.overwrite)

    def test_accepts_import_adjust_paths_flag(self) -> None:
        args = parse_args(["--import", "cfg.yaml", "--import-adjust-paths"])
        self.assertTrue(args.import_adjust_paths)

    def test_accepts_import_adjust_paths_short_alias(self) -> None:
        args = parse_args(["--import", "cfg.yaml", "-a"])
        self.assertTrue(args.import_adjust_paths)

    def test_accepts_job_name_with_other_flags(self) -> None:
        args = parse_args(["-o", "-f", "my-job"])
        self.assertTrue(args.once)
        self.assertTrue(args.force)
        self.assertEqual(args.job_name, "my-job")

    def test_accepts_reload_config_seconds(self) -> None:
        args = parse_args(["--reload-config-seconds", "120"])
        self.assertEqual(args.reload_config_seconds, 120)

    def test_default_reload_config_seconds(self) -> None:
        args = parse_args([])
        self.assertEqual(args.reload_config_seconds, 60)

    def test_accepts_disable_when_run_flag(self) -> None:
        args = parse_args(["--disable-when-run"])
        self.assertTrue(args.disable_when_run)


class JobNameFilterTests(unittest.TestCase):
    def test_filters_to_exact_name(self) -> None:
        jobs = [
            {"name": "alpha", "slug": "alpha"},
            {"name": "beta", "slug": "beta"},
        ]
        filtered = filter_jobs_by_name(jobs, "beta")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["name"], "beta")

    def test_rejects_empty_filter(self) -> None:
        with self.assertRaises(ValueError):
            filter_jobs_by_name([{"name": "alpha"}], "   ")


class RunLoopTests(unittest.TestCase):
    def test_disable_when_run_happens_before_queue(self) -> None:
        jobs = [
            {
                "name": "pueue-restart",
                "slug": "pueue-restart",
                "path": "/tmp",
                "build": "",
                "test": "systemctl --user restart pueued.service",
                "interval": None,
                "at": "23:59",
                "disable_when_run": True,
                "source_config_path": "/tmp/pueue.yaml",
                "manual_install_cmd": "",
            }
        ]

        events: list[str] = []

        def disable_side_effect(*args: object, **kwargs: object) -> None:
            events.append("disable")

        def queue_side_effect(*args: object, **kwargs: object) -> None:
            events.append("queue")
            return None

        with patch.object(buildbot, "ensure_pueue_group"):
            with patch.object(buildbot, "set_group_parallelism"):
                with patch.object(buildbot, "get_pueue_status", return_value={"tasks": {}}):
                    with patch.object(buildbot, "log_finished_task_outcomes"):
                        with patch.object(buildbot, "prepare_repo_for_build", return_value=True):
                            with patch.object(
                                buildbot,
                                "disable_job_in_source_config",
                                side_effect=disable_side_effect,
                            ):
                                with patch.object(
                                    buildbot,
                                    "queue_job",
                                    side_effect=queue_side_effect,
                                ):
                                    rc = run_loop(
                                        jobs=jobs,
                                        group_prefix="gfff",
                                        tick=1,
                                        dry_run=False,
                                        run_once=True,
                                        force_run=True,
                                    )

        self.assertEqual(rc, 0)
        self.assertEqual(events, ["disable", "queue"])

    def test_once_force_queues_at_job_immediately(self) -> None:
        jobs = [
            {
                "name": "daily",
                "slug": "daily",
                "path": "/tmp",
                "build": "make",
                "test": "",
                "interval": None,
                "at": "23:59",
                "manual_install_cmd": "",
            }
        ]

        with patch.object(buildbot, "ensure_pueue_group"):
            with patch.object(buildbot, "set_group_parallelism"):
                with patch.object(buildbot, "get_pueue_status", return_value={"tasks": {}}):
                    with patch.object(buildbot, "log_finished_task_outcomes"):
                        with patch.object(buildbot, "prepare_repo_for_build", return_value=True) as prep_mock:
                            with patch.object(buildbot, "queue_job", return_value=None) as queue_mock:
                                rc = run_loop(
                                    jobs=jobs,
                                    group_prefix="gfff",
                                    tick=1,
                                    dry_run=False,
                                    run_once=True,
                                    force_run=True,
                                )

        self.assertEqual(rc, 0)
        prep_mock.assert_called_once()
        queue_mock.assert_called_once()

    def test_once_force_queues_all_eligible_jobs(self) -> None:
        jobs = [
            {
                "name": "a",
                "slug": "a",
                "path": "/tmp",
                "build": "make",
                "test": "",
                "interval": 60,
                "at": "",
                "manual_install_cmd": "",
            },
            {
                "name": "b",
                "slug": "b",
                "path": "/tmp",
                "build": "make",
                "test": "",
                "interval": None,
                "at": "23:59",
                "manual_install_cmd": "",
            },
        ]

        with patch.object(buildbot, "ensure_pueue_group"):
            with patch.object(buildbot, "set_group_parallelism"):
                with patch.object(buildbot, "get_pueue_status", return_value={"tasks": {}}):
                    with patch.object(buildbot, "log_finished_task_outcomes"):
                        with patch.object(buildbot, "prepare_repo_for_build", return_value=True):
                            with patch.object(buildbot, "queue_job", return_value=None) as queue_mock:
                                rc = run_loop(
                                    jobs=jobs,
                                    group_prefix="gfff",
                                    tick=1,
                                    dry_run=False,
                                    run_once=True,
                                    force_run=True,
                                )

        self.assertEqual(rc, 0)
        self.assertEqual(queue_mock.call_count, 2)

    def test_once_skips_scheduled_only_jobs(self) -> None:
        jobs = [
            {
                "name": "scheduled-only",
                "slug": "scheduled-only",
                "path": "/tmp",
                "build": "make",
                "test": "",
                "interval": 60,
                "at": "",
                "run_mode": "scheduled",
                "manual_install_cmd": "",
            }
        ]

        with patch.object(buildbot, "ensure_pueue_group"):
            with patch.object(buildbot, "set_group_parallelism"):
                with patch.object(buildbot, "get_pueue_status", return_value={"tasks": {}}):
                    with patch.object(buildbot, "log_finished_task_outcomes"):
                        with patch.object(buildbot, "prepare_repo_for_build", return_value=True) as prep_mock:
                            with patch.object(buildbot, "queue_job", return_value=None) as queue_mock:
                                rc = run_loop(
                                    jobs=jobs,
                                    group_prefix="gfff",
                                    tick=1,
                                    dry_run=False,
                                    run_once=True,
                                    force_run=True,
                                )

        self.assertEqual(rc, 0)
        prep_mock.assert_not_called()
        queue_mock.assert_not_called()

    def test_once_with_named_filter_allows_scheduled_only_jobs(self) -> None:
        jobs = [
            {
                "name": "scheduled-only",
                "slug": "scheduled-only",
                "path": "/tmp",
                "build": "make",
                "test": "",
                "interval": 60,
                "at": "",
                "run_mode": "scheduled",
                "manual_install_cmd": "",
            }
        ]

        with patch.object(buildbot, "ensure_pueue_group"):
            with patch.object(buildbot, "set_group_parallelism"):
                with patch.object(buildbot, "get_pueue_status", return_value={"tasks": {}}):
                    with patch.object(buildbot, "log_finished_task_outcomes"):
                        with patch.object(buildbot, "prepare_repo_for_build", return_value=True) as prep_mock:
                            with patch.object(buildbot, "queue_job", return_value=None) as queue_mock:
                                rc = run_loop(
                                    jobs=jobs,
                                    group_prefix="gfff",
                                    tick=1,
                                    dry_run=False,
                                    run_once=True,
                                    force_run=True,
                                    job_name_filter="scheduled-only",
                                )

        self.assertEqual(rc, 0)
        prep_mock.assert_called_once()
        queue_mock.assert_called_once()

    def test_once_with_named_filter_logs_scheduled_override(self) -> None:
        jobs = [
            {
                "name": "scheduled-only",
                "slug": "scheduled-only",
                "path": "/tmp",
                "build": "make",
                "test": "",
                "interval": 60,
                "at": "",
                "run_mode": "scheduled",
                "manual_install_cmd": "",
            }
        ]

        with patch.object(buildbot, "ensure_pueue_group"):
            with patch.object(buildbot, "set_group_parallelism"):
                with patch.object(buildbot, "get_pueue_status", return_value={"tasks": {}}):
                    with patch.object(buildbot, "log_finished_task_outcomes"):
                        with patch.object(buildbot, "prepare_repo_for_build", return_value=True):
                            with patch.object(buildbot, "queue_job", return_value=None):
                                with patch.object(buildbot, "log_event") as log_mock:
                                    rc = run_loop(
                                        jobs=jobs,
                                        group_prefix="gfff",
                                        tick=1,
                                        dry_run=False,
                                        run_once=True,
                                        force_run=True,
                                        job_name_filter="scheduled-only",
                                    )

        self.assertEqual(rc, 0)
        log_mock.assert_any_call(
            "INFO",
            "allowing scheduled jobs in --once because explicit job filter is set: scheduled-only",
        )

    def test_scheduled_mode_skips_manual_only_jobs(self) -> None:
        jobs = [
            {
                "name": "manual-only",
                "slug": "manual-only",
                "path": "/tmp",
                "build": "make",
                "test": "",
                "interval": 60,
                "at": "",
                "run_mode": "manual",
                "manual_install_cmd": "",
            },
            {
                "name": "scheduled",
                "slug": "scheduled",
                "path": "/tmp",
                "build": "make",
                "test": "",
                "interval": 60,
                "at": "",
                "run_mode": "normal",
                "manual_install_cmd": "",
            },
        ]

        with patch.object(buildbot, "ensure_pueue_group"):
            with patch.object(buildbot, "set_group_parallelism"):
                with patch.object(buildbot, "get_pueue_status", return_value={"tasks": {}}):
                    with patch.object(buildbot, "log_finished_task_outcomes"):
                        with patch.object(buildbot, "prepare_repo_for_build", return_value=True):
                            with patch.object(buildbot, "queue_job", return_value=None) as queue_mock:
                                with self.assertRaises(KeyboardInterrupt):
                                    with patch.object(buildbot.time, "sleep", side_effect=KeyboardInterrupt):
                                        run_loop(
                                            jobs=jobs,
                                            group_prefix="gfff",
                                            tick=1,
                                            dry_run=False,
                                            run_once=False,
                                            force_run=False,
                                        )

        self.assertEqual(queue_mock.call_count, 1)
        queued_job = queue_mock.call_args[0][0]
        self.assertEqual(queued_job["name"], "scheduled")

    def test_scheduler_reloads_config_periodically(self) -> None:
        jobs = [
            {
                "name": "initial",
                "slug": "initial",
                "path": "/tmp",
                "build": "make",
                "test": "",
                "interval": 60,
                "at": "",
                "run_mode": "normal",
                "manual_install_cmd": "",
            }
        ]
        config_paths = [Path("/tmp/reload.yaml")]
        reloaded_raw_jobs = [
            {
                "name": "reloaded",
                "active": True,
                "path": "~/repo",
                "build": "make",
                "interval": 60,
            }
        ]

        with patch.object(buildbot, "ensure_pueue_group"):
            with patch.object(buildbot, "set_group_parallelism"):
                with patch.object(buildbot, "get_pueue_status", return_value={"tasks": {}}):
                    with patch.object(buildbot, "log_finished_task_outcomes"):
                        with patch.object(buildbot, "merge_jobs_from_configs", return_value=reloaded_raw_jobs) as merge_mock:
                            with patch.object(buildbot, "prepare_repo_for_build", return_value=False):
                                with patch.object(buildbot.time, "time", side_effect=[0, 61]):
                                    with self.assertRaises(KeyboardInterrupt):
                                        with patch.object(buildbot.time, "sleep", side_effect=KeyboardInterrupt):
                                            run_loop(
                                                jobs=jobs,
                                                group_prefix="gfff",
                                                tick=1,
                                                dry_run=False,
                                                run_once=False,
                                                force_run=False,
                                                config_paths=config_paths,
                                                reload_config_seconds=60,
                                            )

        merge_mock.assert_called_once_with(config_paths)

    def test_scheduler_logs_next_run_for_at_jobs(self) -> None:
        jobs = [
            {
                "name": "daily",
                "slug": "daily",
                "path": "/tmp",
                "build": "make",
                "test": "",
                "interval": None,
                "at": "11:58",
                "run_mode": "normal",
                "manual_install_cmd": "",
            }
        ]

        with patch.object(buildbot, "ensure_pueue_group"):
            with patch.object(buildbot, "set_group_parallelism"):
                with patch.object(buildbot, "get_pueue_status", return_value={"tasks": {}}):
                    with patch.object(buildbot, "log_finished_task_outcomes"):
                        with patch.object(buildbot, "prepare_repo_for_build", return_value=False):
                            with patch.object(buildbot.time, "sleep", side_effect=KeyboardInterrupt):
                                with patch.object(buildbot, "log_event") as log_mock:
                                    with self.assertRaises(KeyboardInterrupt):
                                        run_loop(
                                            jobs=jobs,
                                            group_prefix="gfff",
                                            tick=1,
                                            dry_run=False,
                                            run_once=False,
                                            force_run=False,
                                        )

        messages = [call.args[1] for call in log_mock.call_args_list if len(call.args) > 1]
        self.assertTrue(any("startup schedule next-run [daily] at 11:58" in msg for msg in messages))

    def test_scheduler_reload_recomputes_when_at_changes(self) -> None:
        jobs = [
            {
                "name": "pueue-restart",
                "slug": "pueue-restart",
                "path": "/tmp",
                "build": "",
                "test": "systemctl --user restart pueued.service",
                "interval": None,
                "at": "11:58",
                "run_mode": "scheduled",
                "manual_install_cmd": "",
            }
        ]
        config_paths = [Path("/tmp/reload.yaml")]
        reloaded_raw_jobs = [
            {
                "name": "pueue-restart",
                "active": True,
                "run-mode": "scheduled",
                "path": "~/dev/pueue",
                "test": "systemctl --user restart pueued.service",
                "at": "12:06",
            }
        ]
        # 2026-07-25 12:05:53 local time in the test environment.
        t0 = dt.datetime(2026, 7, 25, 12, 5, 53).timestamp()

        with patch.object(buildbot, "ensure_pueue_group"):
            with patch.object(buildbot, "set_group_parallelism"):
                with patch.object(buildbot, "get_pueue_status", return_value={"tasks": {}}):
                    with patch.object(buildbot, "log_finished_task_outcomes"):
                        with patch.object(buildbot, "merge_jobs_from_configs", return_value=reloaded_raw_jobs):
                            with patch.object(buildbot, "prepare_repo_for_build", return_value=False):
                                with patch.object(buildbot.time, "time", side_effect=[t0, t0 + 2]):
                                    with patch.object(buildbot.time, "sleep", side_effect=KeyboardInterrupt):
                                        with patch.object(buildbot, "log_event") as log_mock:
                                            with self.assertRaises(KeyboardInterrupt):
                                                run_loop(
                                                    jobs=jobs,
                                                    group_prefix="gfff",
                                                    tick=1,
                                                    dry_run=False,
                                                    run_once=False,
                                                    force_run=False,
                                                    config_paths=config_paths,
                                                    reload_config_seconds=1,
                                                )

        messages = [call.args[1] for call in log_mock.call_args_list if len(call.args) > 1]
        self.assertTrue(
            any(
                "reload schedule next-run [pueue-restart] at 12:06 -> 2026-07-25 12:06:00"
                in msg
                for msg in messages
            )
        )


class MainCheckImportTests(unittest.TestCase):
    def test_main_logs_config_discovery_summary_line(self) -> None:
        path_a = Path("/tmp/a.yaml")
        path_b = Path("/tmp/b.yaml")

        output = io.StringIO()
        with redirect_stdout(output):
            with patch.object(buildbot, "check_dependencies"):
                with patch.object(buildbot, "discover_default_config_paths", return_value=[path_a, path_b]):
                    with patch.object(buildbot, "merge_jobs_from_configs", return_value=[]):
                        with patch.object(buildbot, "run_loop", return_value=0):
                            rc = buildbot.main([])

        self.assertEqual(rc, 0)
        self.assertIn(
            "config discovery order: /tmp/a.yaml, /tmp/b.yaml",
            output.getvalue(),
        )
        self.assertIn(
            "startup currently loaded configs: /tmp/a.yaml, /tmp/b.yaml",
            output.getvalue(),
        )

    def test_once_force_includes_inactive_jobs(self) -> None:
        config_path = Path("/tmp/a.yaml")
        raw_jobs = [
            {
                "name": "inactive-on-purpose",
                "active": False,
                "path": "~/repo",
                "build": "make",
                "interval": 60,
            }
        ]

        with patch.object(buildbot, "check_dependencies"):
            with patch.object(buildbot, "discover_default_config_paths", return_value=[config_path]):
                with patch.object(buildbot, "merge_jobs_from_configs", return_value=raw_jobs):
                    with patch.object(buildbot, "run_loop", return_value=0) as run_loop_mock:
                        rc = buildbot.main(["--once", "--force"])

        self.assertEqual(rc, 0)
        queued_jobs = run_loop_mock.call_args.kwargs["jobs"]
        self.assertEqual(len(queued_jobs), 1)
        self.assertEqual(queued_jobs[0]["name"], "inactive-on-purpose")

    def test_check_validates_and_exits_without_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cfg.yaml"
            cfg.write_text(
                "- name: check-job\n"
                "  active: true\n"
                "  path: ~/repo\n"
                "  test: pytest -q\n"
                "  interval: 60\n",
                encoding="utf-8",
            )

            output = io.StringIO()
            with redirect_stdout(output):
                with patch.object(buildbot, "check_dependencies") as dep_mock:
                    rc = buildbot.main(["--check", str(cfg)])

        self.assertEqual(rc, 0)
        dep_mock.assert_not_called()
        self.assertIn("OK:", output.getvalue())

    def test_import_validates_and_copies_to_user_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir(parents=True, exist_ok=True)
            cfg = Path(tmp) / "custom.yaml"
            cfg.write_text(
                "- name: import-job\n"
                "  active: true\n"
                "  path: ~/repo\n"
                "  build: make\n"
                "  interval: 60\n",
                encoding="utf-8",
            )

            output = io.StringIO()
            with redirect_stdout(output):
                with patch.object(buildbot.Path, "home", return_value=home):
                    with patch.object(buildbot, "check_dependencies") as dep_mock:
                        rc = buildbot.main(["--import", str(cfg)])

            copied = home / ".config" / "gfff" / "custom.yaml"
            self.assertTrue(copied.is_file())
            self.assertEqual(copied.read_text(encoding="utf-8"), cfg.read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            dep_mock.assert_not_called()
            self.assertIn("OK: imported", output.getvalue())

    def test_check_and_import_together_fails(self) -> None:
        rc = buildbot.main(["--check", "a.yaml", "--import", "b.yaml"])
        self.assertEqual(rc, 1)

    def test_check_invalid_config_fails_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "bad.yaml"
            cfg.write_text("name: invalid\n", encoding="utf-8")

            rc = buildbot.main(["--check", str(cfg)])

        self.assertEqual(rc, 1)

    def test_overwrite_without_import_fails(self) -> None:
        rc = buildbot.main(["--overwrite"])
        self.assertEqual(rc, 1)

    def test_import_adjust_paths_without_import_fails(self) -> None:
        rc = buildbot.main(["--import-adjust-paths"])
        self.assertEqual(rc, 1)

    def test_import_existing_file_without_overwrite_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir(parents=True, exist_ok=True)
            cfg = Path(tmp) / "custom.yaml"
            cfg.write_text(
                "- name: import-job\n"
                "  active: true\n"
                "  path: ~/repo\n"
                "  build: make\n"
                "  interval: 60\n",
                encoding="utf-8",
            )

            target = home / ".config" / "gfff" / "custom.yaml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("old\n", encoding="utf-8")

            with patch.object(buildbot.Path, "home", return_value=home):
                rc = buildbot.main(["--import", str(cfg)])

            self.assertEqual(rc, 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_import_existing_file_with_overwrite_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir(parents=True, exist_ok=True)
            cfg = Path(tmp) / "custom.yaml"
            cfg.write_text(
                "- name: import-job\n"
                "  active: true\n"
                "  path: ~/repo\n"
                "  build: make\n"
                "  interval: 60\n",
                encoding="utf-8",
            )

            target = home / ".config" / "gfff" / "custom.yaml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("old\n", encoding="utf-8")

            with patch.object(buildbot.Path, "home", return_value=home):
                rc = buildbot.main(["--import", str(cfg), "--overwrite"])

            self.assertEqual(rc, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), cfg.read_text(encoding="utf-8"))

    def test_import_existing_file_with_overwrite_short_alias_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir(parents=True, exist_ok=True)
            cfg = Path(tmp) / "custom.yaml"
            cfg.write_text(
                "- name: import-job\n"
                "  active: true\n"
                "  path: ~/repo\n"
                "  build: make\n"
                "  interval: 60\n",
                encoding="utf-8",
            )

            target = home / ".config" / "gfff" / "custom.yaml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("old\n", encoding="utf-8")

            with patch.object(buildbot.Path, "home", return_value=home):
                rc = buildbot.main(["--import", str(cfg), "-w"])

            self.assertEqual(rc, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), cfg.read_text(encoding="utf-8"))

    def test_import_with_adjust_paths_rewrites_all_job_paths_to_source_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir(parents=True, exist_ok=True)
            repo_dir = Path(tmp) / "another-name"
            repo_dir.mkdir(parents=True, exist_ok=True)
            cfg = repo_dir / "custom.yaml"
            cfg.write_text(
                "- name: one\n"
                "  active: true\n"
                "  path: ~/dev/gfff\n"
                "  build: make\n"
                "  interval: 60\n"
                "- name: two\n"
                "  active: true\n"
                "  path: /tmp/somewhere\n"
                "  test: pytest -q\n"
                "  interval: 60\n",
                encoding="utf-8",
            )

            with patch.object(buildbot.Path, "home", return_value=home):
                rc = buildbot.main(
                    ["--import", str(cfg), "--import-adjust-paths"]
                )

            self.assertEqual(rc, 0)
            target = home / ".config" / "gfff" / "custom.yaml"
            imported = load_yaml_config(target)
            self.assertEqual(imported[0]["path"], str(repo_dir.resolve()))
            self.assertEqual(imported[1]["path"], str(repo_dir.resolve()))


if __name__ == "__main__":
    unittest.main()
