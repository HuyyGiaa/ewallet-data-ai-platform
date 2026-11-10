"""The EDAI-1 terminal workflow: scripts/edai1/*.sh and ops/gcp/edai1_campaign_cli.py.

Python-level tests use a recording runner for git, gcloud and Airflow. Shell-level
tests run the real scripts with stub git/gcloud/airflow binaries on PATH, so no test
reaches Git history, GCS, Dataproc or the local Airflow database.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ops.gcp import campaign_task, edai1_campaign_cli as cli
from ops.gcp.campaign_task import parse_parameters
from tests.unit.campaign_fakes import SHA, RecordingRunner, TerraformFixture, terraform_outputs


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/edai1"
BENCHMARK = "ab-20261112090000"
SOURCE_SHA = cli.KNOWN_SOURCE_MANIFESTS["rehearsal-5gb-v1"]


def make_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class CliRunner(RecordingRunner):
    """RecordingRunner plus the local Airflow CLI and the runtime status helper."""

    def __init__(self, airflow_bin: str, *, dag_runs=None, healthy: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.airflow_bin = airflow_bin
        self.dag_runs: dict[str, list[dict[str, str]]] = dag_runs or {}
        self.healthy = healthy
        self.airflow_calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        command = [str(item) for item in command]
        if command[0] == self.airflow_bin:
            self.calls.append(command)
            self.airflow_calls.append(command[1:])
            arguments = command[1:]
            if arguments[:2] == ["dags", "details"]:
                return self._done(stdout='[{"dag_id": "%s"}]' % arguments[2])
            if arguments[:2] == ["dags", "list-runs"]:
                return self._done(stdout="WARNING noise line\n" + json.dumps(self.dag_runs.get(arguments[2], [])))
            if arguments[:2] == ["tasks", "states-for-dag-run"]:
                return self._done(stdout=json.dumps([{"task_id": "bronze.submit_transformation", "state": "running"}]))
            if arguments[:2] in (["dags", "unpause"], ["dags", "trigger"]):
                return self._done(stdout="[]")
            raise AssertionError(f"unexpected airflow call {arguments}")
        if command[0].endswith("scripts/start_airflow_local.sh"):
            self.calls.append(command)
            return self._done(0 if self.healthy else 1, stdout="overall READY\n")
        return super().__call__(command, **kwargs)


class CliFixture(TerraformFixture):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.airflow = make_executable(self.root / "airflow", "#!/usr/bin/env bash\nexit 99\n")
        self.environ = {"EDAI1_CAMPAIGN_DIR": str(self.root / "campaigns"), "AIRFLOW_BIN": str(self.airflow)}

    def runner(self, **kwargs) -> CliRunner:
        return CliRunner(str(self.airflow), **kwargs)

    def cli(self, *argv: str, runner: CliRunner) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv), runner=runner, environ=self.environ)
        return code, out.getvalue(), err.getvalue()

    def prepare(self, runner: CliRunner | None = None, *extra: str) -> tuple[int, str, str]:
        return self.cli("prepare", "--benchmark-id", BENCHMARK, "--terraform-output-json", str(self.outputs),
                        "--skip-source-check", *extra, runner=runner or self.runner())

    def directory(self) -> Path:
        return self.root / "campaigns" / BENCHMARK


class PrepareTest(CliFixture):
    def test_prepare_writes_six_pinned_non_executing_configurations(self):
        code, out, err = self.prepare()
        self.assertEqual(0, code, err)
        plan = json.loads((self.directory() / "campaign.json").read_text())
        self.assertEqual((BENCHMARK, SHA, "main", "rehearsal-5gb-v1", SOURCE_SHA),
                         (plan["benchmark_id"], plan["git_sha"], plan["git_branch"], plan["dataset_version"],
                          plan["source_manifest_sha256"]))
        self.assertEqual("NOT_VERIFIED", plan["source_verification"]["status"])
        run_ids = set()
        for profile in ("baseline", "optimized"):
            for pipeline in ("dp1", "dp2", "dp3"):
                path = self.directory() / profile / f"{pipeline}.json"
                conf = json.loads(path.read_text())
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
                self.assertEqual("NOT_APPROVED", conf["execution_approval"])
                self.assertEqual(profile, conf["spark_profile"])
                self.assertEqual(str(self.directory() / "terraform-outputs.json"), conf["terraform_output_json"])
                parse_parameters(conf, pipeline=pipeline, require_approval=False)
                with self.assertRaises(campaign_task.CampaignError):
                    parse_parameters(conf, pipeline=pipeline)
            run_ids |= set(plan["profiles"][profile]["run_ids"].values())
        self.assertEqual(8, len(run_ids))
        self.assertEqual({"bronze_run_id"}, {k for k in json.loads((self.directory() / "baseline/dp1.json").read_text())
                                             if k.endswith("_run_id")})
        self.assertEqual(0o700, stat.S_IMODE(self.directory().stat().st_mode))
        self.assertIn("nothing was triggered or submitted", out)
        self.assertEqual(f"{BENCHMARK}-optimized-dp2", plan["profiles"]["optimized"]["dag_runs"]["dp2"])

    def test_the_git_sha_is_the_current_head_never_a_constant(self):
        runner = self.runner(git_sha="f" * 40)
        self.assertEqual(0, self.prepare(runner)[0])
        plan = json.loads((self.directory() / "campaign.json").read_text())
        self.assertEqual("f" * 40, plan["git_sha"])
        self.assertTrue(json.loads((self.directory() / "optimized/dp3.json").read_text())["git_sha"] == "f" * 40)

    def test_prepare_refuses_dirty_trees_other_branches_and_reuse(self):
        for runner, message in ((self.runner(dirty=" M x\n"), "not clean"),
                                (self.runner(branch="feature/x"), "branch")):
            with self.subTest(message):
                code, _, err = self.prepare(runner)
                self.assertEqual(1, code)
                self.assertIn(message, err)
                self.assertFalse(self.directory().exists())
        self.assertEqual(0, self.prepare()[0])
        code, _, err = self.prepare()
        self.assertEqual(1, code)
        self.assertIn("already exists", err)
        code, _, err = self.cli("prepare", "--benchmark-id", "ab-20261111033139", "--skip-source-check",
                                "--terraform-output-json", str(self.outputs), runner=self.runner())
        self.assertEqual(1, code)
        self.assertIn("abandoned", err)

    def test_prepare_refuses_a_deployment_other_than_the_dataset_bucket_contract(self):
        self.outputs.write_text(json.dumps(terraform_outputs(project="other-project-id")), encoding="utf-8")
        code, _, err = self.prepare()
        self.assertEqual(1, code)
        self.assertIn("differs", err)
        self.assertFalse(self.directory().exists())

    def test_prepare_verifies_the_source_read_only_by_default(self):
        runner = self.runner()
        code, _, err = self.cli("prepare", "--benchmark-id", BENCHMARK, "--terraform-output-json", str(self.outputs),
                                runner=runner)
        self.assertEqual(0, code, err)
        self.source.assert_called_once()
        self.assertEqual(SOURCE_SHA, self.source.call_args.kwargs["expected_manifest_sha256"])
        self.assertFalse([call for call in runner.calls if call[:3] == ["gcloud", "storage", "cp"]])

    def test_edited_configurations_are_refused(self):
        self.assertEqual(0, self.prepare()[0])
        path = self.directory() / "baseline/dp1.json"
        conf = json.loads(path.read_text())
        conf["bronze_run_id"] = conf["bronze_run_id"].replace("baseline", "optimized")
        path.chmod(0o600)
        path.write_text(json.dumps(conf))
        code, _, err = self.cli("run", "--pipeline", "dp1", "--benchmark-id", BENCHMARK, "--profile", "baseline",
                                runner=self.runner())
        self.assertEqual(1, code)
        self.assertIn("must not be edited", err)


class RunTest(CliFixture):
    def run_cli(self, pipeline: str, *extra: str, profile: str = "baseline", runner: CliRunner | None = None):
        runner = runner or self.runner()
        result = self.cli("run", "--pipeline", pipeline, "--benchmark-id", BENCHMARK, "--profile", profile, *extra,
                          runner=runner)
        return (*result, runner)

    def mutations(self, runner: CliRunner) -> list[list[str]]:
        cloud = [call for call in runner.calls if call[:3] == ["gcloud", "storage", "cp"]
                 or call[:4] == ["gcloud", "dataproc", "batches", "submit"]]
        return cloud + [call for call in runner.airflow_calls if call[:2] in (["dags", "unpause"], ["dags", "trigger"])]

    def test_dp1_dry_run_is_ready_and_changes_nothing(self):
        self.assertEqual(0, self.prepare()[0])
        code, out, err, runner = self.run_cli("dp1")
        self.assertEqual(0, code, err)
        self.assertIn("MODE: DRY-RUN", out)
        self.assertIn("READY. DRY-RUN only", out)
        self.assertIn("EXECUTE-PAID-DATAPROC:DP1:", out)
        self.assertIn("AQE=false SHUFFLE_PARTITIONS=200", out)
        self.assertEqual([], self.mutations(runner))

    def test_profile_summaries_are_the_approved_configurations(self):
        self.assertEqual(0, self.prepare()[0])
        expected = {
            ("dp1", "optimized"): ["AQE=true SHUFFLE_PARTITIONS=200 SKEW_JOIN=false COALESCE=spark-default "
                                   "ADVISORY=spark-default ALLOCATION_PROFILE=performance"],
            ("dp2", "optimized"): ["AQE=true SHUFFLE_PARTITIONS=200 SKEW_JOIN=false COALESCE=true ADVISORY=67108864",
                                   "AQE=true SHUFFLE_PARTITIONS=400 SKEW_JOIN=false COALESCE=spark-default"],
            ("dp3", "optimized"): ["AQE=true SHUFFLE_PARTITIONS=200 SKEW_JOIN=false COALESCE=true ADVISORY=67108864"],
            ("dp2", "baseline"): ["AQE=false SHUFFLE_PARTITIONS=200 SKEW_JOIN=false COALESCE=spark-default"],
        }
        for (pipeline, profile), fragments in expected.items():
            with self.subTest(pipeline=pipeline, profile=profile):
                _, out, _, _ = self.run_cli(pipeline, "--offline", profile=profile)
                for fragment in fragments:
                    self.assertIn(fragment, out)

    def test_dp2_and_dp3_refuse_without_a_successful_upstream_handoff(self):
        self.assertEqual(0, self.prepare()[0])
        for pipeline, message in (("dp2", "DP1 handoff is missing"), ("dp3", "DP2 handoff is missing")):
            with self.subTest(pipeline):
                code, out, err, runner = self.run_cli(pipeline)
                self.assertEqual(1, code)
                self.assertIn("NOT READY", err)
                self.assertIn(message, err)
                self.assertEqual([], self.mutations(runner))
                code, _, err, runner = self.run_cli(pipeline, "--execute", "--confirm-paid")
                self.assertEqual(1, code)
                self.assertEqual([], self.mutations(runner))

    def test_execute_requires_confirm_paid_and_forbids_offline(self):
        self.assertEqual(0, self.prepare()[0])
        for extra, message in ((("--execute",), "--confirm-paid"), (("--confirm-paid",), "only meaningful"),
                               (("--execute", "--confirm-paid", "--offline"), "only allowed for a dry run")):
            with self.subTest(extra):
                code, _, err, runner = self.run_cli("dp1", *extra)
                self.assertEqual(1, code)
                self.assertIn(message, err)
                self.assertEqual([], runner.calls)

    def test_execute_triggers_exactly_one_approved_dag_run(self):
        self.assertEqual(0, self.prepare()[0])
        code, out, err, runner = self.run_cli("dp1", "--execute", "--confirm-paid", profile="optimized")
        self.assertEqual(0, code, err)
        unpause, trigger = [call for call in runner.airflow_calls if call[:2] in (["dags", "unpause"], ["dags", "trigger"])]
        self.assertEqual(["dags", "unpause", "dp1_source_to_bronze"], unpause)
        self.assertEqual(["dags", "trigger", "dp1_source_to_bronze", "--run-id", f"{BENCHMARK}-optimized-dp1"], trigger[:5])
        conf = json.loads(trigger[trigger.index("--conf") + 1])
        params = parse_parameters(conf, pipeline="dp1")  # the approval token is exact
        self.assertEqual("optimized", params.spark_profile)
        self.assertIn(f"TRIGGERED dp1_source_to_bronze DagRun {BENCHMARK}-optimized-dp1", out)
        self.assertIn(f"http://127.0.0.1:8084/dags/dp1_source_to_bronze/runs/{BENCHMARK}-optimized-dp1", out)
        self.assertFalse([call for call in runner.calls if call[:3] == ["gcloud", "storage", "cp"]
                          or call[:4] == ["gcloud", "dataproc", "batches", "submit"]])

    def test_execute_refuses_when_the_runtime_is_unhealthy(self):
        self.assertEqual(0, self.prepare()[0])
        code, _, err, runner = self.run_cli("dp1", "--execute", "--confirm-paid", runner=self.runner(healthy=False))
        self.assertEqual(1, code)
        self.assertIn("not healthy", err)
        self.assertEqual([], self.mutations(runner))

    def test_a_planned_dag_run_is_never_triggered_twice(self):
        self.assertEqual(0, self.prepare()[0])
        runner = self.runner(dag_runs={"dp1_source_to_bronze": [
            {"run_id": f"{BENCHMARK}-baseline-dp1", "state": "failed"}]})
        code, _, err, _ = self.run_cli("dp1", "--execute", "--confirm-paid", runner=runner)
        self.assertEqual(1, code)
        self.assertIn("already exists", err)
        self.assertEqual([], self.mutations(runner))
        runner = self.runner(dag_runs={"dp1_source_to_bronze": [{"run_id": "other", "state": "running"}]})
        code, _, err, _ = self.run_cli("dp1", runner=runner)
        self.assertEqual(1, code)
        self.assertIn("active DagRuns", err)

    def test_the_pinned_sha_is_enforced_when_main_moves_on(self):
        self.assertEqual(0, self.prepare()[0])
        code, _, err, runner = self.run_cli("dp1", "--execute", "--confirm-paid", runner=self.runner(git_sha="e" * 40))
        self.assertEqual(1, code)
        self.assertIn("pinned Git SHA", err)
        self.assertEqual([], self.mutations(runner))

    def test_existing_outputs_of_the_target_run_are_refused(self):
        self.assertEqual(0, self.prepare()[0])
        plan = json.loads((self.directory() / "campaign.json").read_text())
        bronze = plan["profiles"]["baseline"]["run_ids"]["bronze_run_id"]
        code, _, err, runner = self.run_cli("dp1", runner=self.runner(existing_batches=[bronze]))
        self.assertEqual(1, code)
        self.assertIn("already exists", err)


class StatusAndVerifyTest(CliFixture):
    def test_status_never_invents_success(self):
        self.assertEqual(0, self.prepare()[0])
        runner = self.runner(dag_runs={"dp1_source_to_bronze": [{"run_id": f"{BENCHMARK}-baseline-dp1", "state": "running"}]})
        code, out, err = self.cli("status", "--benchmark-id", BENCHMARK, runner=runner)
        self.assertEqual(0, code, err)
        lines = {tuple(line.split()[:4]) for line in out.splitlines()}
        self.assertIn(("baseline", "DP1", "RUNNING", "ABSENT"), lines)
        for profile, pipeline in (("baseline", "DP2"), ("baseline", "DP3"), ("optimized", "DP1")):
            self.assertIn((profile, pipeline, "NOT_STARTED", "ABSENT"), lines)
        self.assertNotIn("SUCCESS", out)
        self.assertIn("bronze.submit_transformation (running)", out)

    def test_status_reports_unknown_without_airflow(self):
        self.assertEqual(0, self.prepare()[0])
        self.environ["AIRFLOW_BIN"] = str(self.root / "missing-airflow")
        code, out, _ = self.cli("status", "--benchmark-id", BENCHMARK, "--offline", runner=self.runner())
        self.assertEqual(0, code)
        self.assertEqual(6, sum(" UNKNOWN " in f" {line} " for line in out.splitlines()))

    def test_verify_handoffs_follows_the_chain(self):
        self.assertEqual(0, self.prepare()[0])
        plan = json.loads((self.directory() / "campaign.json").read_text())
        # Publish real handoffs for this plan's run IDs through the campaign runner.
        runner = self.runner()
        conf = json.loads((self.directory() / "baseline/dp3.json").read_text())
        for pipeline in ("dp1", "dp2"):
            values = {name: conf[name] for name in campaign_task.PIPELINES[pipeline].parameter_names}
            values["execution_approval"] = campaign_task.expected_approval(values, pipeline)
            params = parse_parameters(values, pipeline=pipeline)
            stages = campaign_task.PIPELINES[pipeline].stages
            runner.existing_batches |= {params.stage_run_id(stage) for stage in stages}
            runner.existing_batches |= {campaign_task.submit_stage_validation.validation_batch_id(
                params.stage_run_id(stage), 1) for stage in stages}
            with redirect_stdout(io.StringIO()):
                campaign_task.run_action("publish-handoff", params, attempt=1, runner=runner,
                                         producer=campaign_task.Producer(params.spec.dag_id, f"{BENCHMARK}-baseline-{pipeline}"))
        self.assertTrue(plan)
        code, out, err = self.cli("verify-handoffs", "--benchmark-id", BENCHMARK, "--profile", "baseline", runner=runner)
        self.assertEqual(0, code, err)
        self.assertIn("ready for DP2   YES", out)
        self.assertIn("ready for DP3   YES", out)
        self.assertIn("exists          NO", out)  # dp3_completion not yet published
        uri = campaign_task.handoff_uri(parse_parameters(conf, pipeline="dp3", require_approval=False), "dp1")
        tampered = json.loads(runner.objects[uri])
        tampered["git_sha"] = "9" * 40
        runner.objects[uri] = json.dumps(tampered)
        code, out, _ = self.cli("verify-handoffs", "--benchmark-id", BENCHMARK, "--profile", "baseline", runner=runner)
        self.assertEqual(1, code)
        self.assertIn("verdict         FAIL", out)
        self.assertFalse([call for call in runner.calls[-20:] if call[:3] == ["gcloud", "storage", "cp"]])


class ShellScriptTest(unittest.TestCase):
    """The real scripts with stub git/gcloud/airflow; nothing outside a temp dir changes."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        stub = '#!/usr/bin/env bash\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$STUB_LOG"\n'
        make_executable(self.bin / "git", stub + (
            'case "$*" in\n'
            '  *"--abbrev-ref HEAD"*) echo main ;;\n'
            '  *"rev-parse HEAD"*) echo "$STUB_GIT_SHA" ;;\n'
            '  *"status --porcelain"*) printf "%s" "${STUB_GIT_DIRTY:-}" ;;\n'
            '  *) exit 1 ;;\n'
            'esac\n'))
        make_executable(self.bin / "gcloud", stub + (
            'case "$*" in\n'
            '  "dataproc batches describe"*) echo "NOT_FOUND: batch" >&2; exit 1 ;;\n'
            '  "storage objects describe"*) echo "ERROR: not found: 404" >&2; exit 1 ;;\n'
            '  "storage ls"*) echo "ERROR: One or more URLs matched no objects." >&2; exit 1 ;;\n'
            '  "storage buckets describe"*) echo "{}" ;;\n'
            '  *) echo "unexpected gcloud $*" >&2; exit 97 ;;\n'
            'esac\n'))
        self.airflow = make_executable(self.bin / "airflow", stub + (
            'case "$*" in\n'
            '  version) echo 3.3.1 ;;\n'
            '  "dags details"*) echo "[{}]" ;;\n'
            '  "dags list-runs"*) echo "[]" ;;\n'
            '  *) echo "unexpected airflow $*" >&2; exit 98 ;;\n'
            'esac\n'))
        self.outputs = self.root / "terraform.json"
        self.outputs.write_text(json.dumps(terraform_outputs()), encoding="utf-8")
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "STUB_LOG": str(self.log),
                    "STUB_GIT_SHA": "c" * 40, "AIRFLOW_BIN": str(self.airflow), "EDAI1_PYTHON": sys.executable,
                    "EDAI1_CAMPAIGN_DIR": str(self.root / "campaigns")}

    def script(self, name: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(SCRIPTS / name), *args], cwd=ROOT, env={**self.env, **env},
                              capture_output=True, text=True, check=False)

    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def prepare(self, **env: str) -> subprocess.CompletedProcess[str]:
        return self.script("prepare_campaign.sh", "--benchmark-id", BENCHMARK, "--skip-source-check",
                           "--terraform-output-json", str(self.outputs), **env)

    def test_every_script_is_strict_bash_with_help(self):
        names = ("prepare_campaign.sh", "run_dp1.sh", "run_dp2.sh", "run_dp3.sh", "status_campaign.sh",
                 "verify_handoffs.sh")
        for name in names:
            with self.subTest(name):
                text = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertTrue(text.startswith("#!/usr/bin/env bash\n"))
                self.assertIn("set -euo pipefail", text)
                self.assertTrue(os.access(SCRIPTS / name, os.X_OK))
                self.assertEqual(0, subprocess.run(["bash", "-n", str(SCRIPTS / name)]).returncode)
                for flag in ("--help", "-h"):
                    result = self.script(name, flag)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertTrue(result.stdout.startswith("Usage: bash scripts/edai1/"))
                for forbidden in ("--daemon", "db reset", "storage rm", "git reset", "git clean", "push"):
                    self.assertNotIn(forbidden, text)
        self.assertEqual("", self.calls())
        common = (SCRIPTS / "_common.sh").read_text(encoding="utf-8")
        self.assertNotIn("trigger", common)

    def test_prepare_and_dry_runs_end_to_end_without_mutation(self):
        result = self.prepare()
        self.assertEqual(0, result.returncode, result.stderr)
        plan = json.loads((self.root / "campaigns" / BENCHMARK / "campaign.json").read_text())
        self.assertEqual("c" * 40, plan["git_sha"])
        dp1 = self.script("run_dp1.sh", "--benchmark-id", BENCHMARK, "--profile", "baseline")
        self.assertEqual(0, dp1.returncode, dp1.stderr)
        self.assertIn("READY. DRY-RUN only", dp1.stdout)
        for name, message in (("run_dp2.sh", "DP1 handoff is missing"), ("run_dp3.sh", "DP2 handoff is missing")):
            with self.subTest(name):
                result = self.script(name, "--benchmark-id", BENCHMARK, "--profile", "optimized")
                self.assertEqual(1, result.returncode)
                self.assertIn(message, result.stderr)
        status = self.script("status_campaign.sh", "--benchmark-id", BENCHMARK)
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertEqual(6, status.stdout.count("NOT_STARTED"))
        verify = self.script("verify_handoffs.sh", "--benchmark-id", BENCHMARK, "--profile", "baseline")
        self.assertEqual(0, verify.returncode, verify.stderr)
        self.assertIn("ready for DP2   NO", verify.stdout)
        calls = self.calls()
        for mutation in ("storage cp", "storage rm", "batches submit", "dags trigger", "dags unpause", "db "):
            self.assertNotIn(mutation, calls)

    def test_dirty_tree_and_unsafe_arguments_are_refused(self):
        result = self.prepare(STUB_GIT_DIRTY=" M data_platform/x.py\n")
        self.assertEqual(1, result.returncode)
        self.assertIn("not clean", result.stderr)
        self.assertFalse((self.root / "campaigns").exists())
        hostile = "ab-1; touch /tmp/edai1-hostile"
        result = self.script("run_dp1.sh", "--benchmark-id", hostile, "--profile", "baseline")
        self.assertEqual(1, result.returncode)
        self.assertFalse(Path("/tmp/edai1-hostile").exists())
        result = self.script("run_dp1.sh", "--benchmark-id", BENCHMARK, "--profile", "fast")
        self.assertEqual(2, result.returncode)
        result = self.prepare(EDAI1_CAMPAIGN_DIR=str(self.root / "with space"))
        self.assertEqual(1, result.returncode)
        self.assertIn("safe DagRun parameter", result.stderr)

    def test_execute_needs_explicit_confirmation(self):
        self.assertEqual(0, self.prepare().returncode)
        result = self.script("run_dp1.sh", "--benchmark-id", BENCHMARK, "--profile", "baseline", "--execute")
        self.assertEqual(1, result.returncode)
        self.assertIn("--confirm-paid", result.stderr)
        self.assertNotIn("dags trigger", self.calls())


if __name__ == "__main__":
    unittest.main()
