import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import coverage_resources as resources


class CoverageResourcesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc = self.root / "proc"
        self.cgroup = self.root / "cgroup"
        self.write(self.proc / "meminfo", "MemTotal: 16384 kB\n")
        mock = patch.object(resources.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, stdout="PID PPID RSS VSZ STAT ELAPSED COMMAND\n12 1 4096 8192 S 00:10 embed.test\n",
        ))
        self.ps = mock.start()
        self.addCleanup(mock.stop)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_v1_counters_and_nested_membership(self):
        self.write(self.proc / "self/cgroup", "4:cpu,cpuacct:/jobs/17\n5:memory:/jobs/17\n")
        group = self.cgroup / "memory/jobs/17"
        for name in resources.CGROUP_FILES["v1"]:
            self.write(group / name, "123\n")
        sample = resources.snapshot(self.proc, self.cgroup)
        self.assertEqual(len(sample["cgroups"]), 1)
        actual = sample["cgroups"][0]
        self.assertEqual(actual["version"], "v1")
        self.assertEqual(actual["path"], str(group))
        for name in ("memory.failcnt", "memory.oom_control", "memory.max_usage_in_bytes", "memory.stat"):
            self.assertEqual(actual["files"][name], "123")
        self.assertIn("embed.test", sample["processes_by_rss"])
        self.assertNotIn("args", self.ps.call_args.args[0][2])

    def test_v2_namespaced_root_is_not_duplicated(self):
        self.write(self.proc / "self/cgroup", "0::/\n")
        self.write(self.cgroup / "memory.current", "100\n")
        self.write(self.cgroup / "memory.max", "1024\n")
        self.write(self.cgroup / "memory.events", "oom 0\noom_kill 0\n")
        sample = resources.snapshot(self.proc, self.cgroup)
        self.assertEqual(len(sample["cgroups"]), 1)
        self.assertEqual(sample["cgroups"][0]["version"], "v2")
        self.assertEqual(sample["cgroups"][0]["files"]["memory.events"], "oom 0\noom_kill 0")
        self.assertIn("unavailable", sample["cgroups"][0]["files"]["memory.peak"])

    def test_keeps_finite_parent_limit_and_oom_counter(self):
        self.write(self.proc / "self/cgroup", "0::/jobs/17\n")
        self.write(self.cgroup / "jobs/17/memory.current", "100\n")
        self.write(self.cgroup / "jobs/17/memory.max", "max\n")
        self.write(self.cgroup / "jobs/memory.current", "1024\n")
        self.write(self.cgroup / "jobs/memory.max", "2048\n")
        self.write(self.cgroup / "jobs/memory.events", "oom_kill 1\n")
        sample = resources.snapshot(self.proc, self.cgroup)
        groups = {item["path"]: item["files"] for item in sample["cgroups"]}
        self.assertEqual(groups[str(self.cgroup / "jobs/17")]["memory.max"], "max")
        self.assertEqual(groups[str(self.cgroup / "jobs")]["memory.max"], "2048")
        self.assertEqual(groups[str(self.cgroup / "jobs")]["memory.events"], "oom_kill 1")

    def test_v1_namespaced_root_and_missing_controller(self):
        self.write(self.proc / "self/cgroup", "5:memory:/../../outside\n")
        self.write(self.cgroup / "memory/memory.usage_in_bytes", "200\n")
        sample = resources.snapshot(self.proc, self.cgroup)
        self.assertEqual(len(sample["cgroups"]), 1)
        self.assertEqual(sample["cgroups"][0]["path"], str(self.cgroup / "memory"))
        paths = resources.cgroup_paths(self.proc, self.cgroup)
        self.assertFalse(any(".." in path.parts for _, path in paths))
        (self.cgroup / "memory/memory.usage_in_bytes").unlink()
        self.assertEqual(resources.snapshot(self.proc, self.cgroup)["cgroups"], [])

    def test_subtree_mount_maps_membership_and_visible_parents(self):
        for version in ("v1", "v2"):
            with self.subTest(version=version):
                mount = self.root / version / "memory subtree"
                escaped_mount = str(mount).replace(" ", r"\040")
                filesystem = "cgroup cgroup rw,memory" if version == "v1" else "cgroup2 cgroup rw"
                self.write(self.proc / "self/mountinfo", f"20 19 0:25 /a {escaped_mount} rw - {filesystem}\n")
                membership = "5:memory:/a/jobs/17\n" if version == "v1" else "0::/a/jobs/17\n"
                self.write(self.proc / "self/cgroup", membership)
                usage, limit = resources.CGROUP_FILES[version][:2]
                self.write(mount / "jobs/17" / usage, "100\n")
                self.write(mount / "jobs/17" / limit, "max\n")
                self.write(mount / "jobs" / usage, "200\n")
                self.write(mount / "jobs" / limit, "1024\n")
                sample = resources.snapshot(self.proc, self.cgroup)
                groups = {item["path"]: item["files"] for item in sample["cgroups"]}
                self.assertEqual(groups[str(mount / "jobs/17")][usage], "100")
                self.assertEqual(groups[str(mount / "jobs")][limit], "1024")
                self.assertNotIn(str(mount / "a/jobs/17"), groups)

    def test_file_and_process_output_are_bounded(self):
        self.write(self.proc / "meminfo", "a" * (resources.MAX_FILE_BYTES + 1000))
        self.ps.return_value.stdout = "\n".join(str(i) for i in range(100))
        sample = resources.snapshot(self.proc, self.cgroup)
        self.assertLess(len(sample["meminfo"]), resources.MAX_FILE_BYTES + 20)
        self.assertTrue(sample["meminfo"].endswith("<truncated>"))
        self.assertEqual(len(sample["processes_by_rss"].splitlines()), 33)

    def test_failed_process_sample_is_explicit(self):
        self.ps.side_effect = subprocess.TimeoutExpired("ps", 3)
        self.assertIn("unavailable", resources.snapshot(self.proc, self.cgroup)["processes_by_rss"])

    def test_appends_jsonl_and_sampling_io_failure_is_nonfatal(self):
        path = self.root / "samples.jsonl"
        with patch.object(resources, "snapshot", return_value={"cgroups": []}):
            resources.append_sample(path, 0)
            resources.append_sample(path, 15)
            samples = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([sample["elapsed_seconds"] for sample in samples], [0, 15])
            with patch.object(resources.Path, "open", side_effect=OSError("disk full")):
                resources.append_sample(path, 30)


class CoverageSchedulingContractTest(unittest.TestCase):
    def test_workflow_bounds_instrumented_processes_and_preserves_scope(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/coverage-ut.yaml").read_text()
        self.assertIn('COVERAGE_UT_PACKAGE_PARALLELISM: "2"', workflow)
        self.assertIn('COVERAGE_UT_GOMAXPROCS: "2"', workflow)
        self.assertIn('-p "${COVERAGE_UT_PACKAGE_PARALLELISM}"', workflow)
        self.assertIn('env GOMAXPROCS="${COVERAGE_UT_GOMAXPROCS}"', workflow)
        self.assertIn('-covermode=set -coverprofile="${coverage_profile}" -coverpkg="${cover_pkgs}"', workflow)
        self.assertIn('COVERAGE_UT_TEST_TIMEOUT_SECONDS: "3600"', workflow)
        self.assertIn('scripts/coverage_resources.py', workflow)
        self.assertIn('--resource-samples "${RUNNER_TEMP}/ut-coverage-resources.jsonl"', workflow)
        self.assertIn('${{ runner.temp }}/ut-coverage-resources.jsonl', workflow)
        self.assertIn('memory.oom_control', workflow)
        self.assertNotIn('MO_MEMORY_DOMAIN_RUNTIME_RESERVE_BYTES', workflow)
        self.assertNotIn('MO_MEMORY_STATEMENT_WAIT_TIMEOUT', workflow)


if __name__ == "__main__":
    unittest.main()
