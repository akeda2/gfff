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
    discover_default_config_paths,
    extract_done_result,
    extract_task_state,
    generate_build_script,
    load_yaml_config,
    log_finished_task_outcomes,
    merge_jobs_from_configs,
    normalize_jobs,
    normalize_task_result,
    next_daily_at_timestamp,
    parse_config_path_from_service,
    parse_bool,
    parse_at_time,
    parse_args,
    prepare_repo_for_build,
    queue_job,
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
        self.assertEqual(jobs[0]["build"], "make")
        self.assertEqual(jobs[0]["test"], "")

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
        self.assertEqual(jobs[0]["build"], "")
        self.assertEqual(jobs[0]["test"], "pytest -q")

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
        with self.assertRaises(ValueError):
            parse_at_time("5:00", "job")
        with self.assertRaises(ValueError):
            parse_at_time("24:00", "job")

    def test_next_daily_at_timestamp(self) -> None:
        now = dt.datetime(2026, 1, 2, 4, 30, 0).timestamp()
        next_ts = next_daily_at_timestamp("05:00", now)
        expected = dt.datetime(2026, 1, 2, 5, 0, 0).timestamp()
        self.assertEqual(next_ts, expected)

        now_after = dt.datetime(2026, 1, 2, 5, 0, 1).timestamp()
        next_after_ts = next_daily_at_timestamp("05:00", now_after)
        expected_next_day = dt.datetime(2026, 1, 3, 5, 0, 0).timestamp()
        self.assertEqual(next_after_ts, expected_next_day)


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
        with patch.object(buildbot.subprocess, "run", return_value=cp(stdout="Queued as (id 42)")):
            with patch.object(buildbot, "log_event") as log_mock:
                task_id = queue_job(job, group="gfff", dry_run=False)
        self.assertEqual(task_id, 42)
        self.assertTrue(log_mock.called)

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

    def test_force_run_skips_git_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = {"name": "repo", "path": tmp}
            with patch.object(buildbot, "run_repo_command") as run_repo_mock:
                ok = prepare_repo_for_build(job, dry_run=False, force_run=True)
            self.assertTrue(ok)
            run_repo_mock.assert_not_called()


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


class RunLoopTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
