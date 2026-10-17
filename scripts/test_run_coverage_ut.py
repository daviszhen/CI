import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


SCRIPT = Path(__file__).with_name("run_coverage_ut.py")
sys.path.insert(0, str(SCRIPT.parent))
import run_coverage_ut


class RunCoverageUTTest(unittest.TestCase):
    def run_wrapper(self, child_source: str, timeout: int = 5, extra_env=None, pid_file=None):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        root = Path(directory)
        report = root / "report.json"
        progress = root / "progress.txt"
        status = root / "status.json"
        timeout_diagnostics = root / "timeout.txt"
        timing = root / "timing.txt"
        pid_file = pid_file or root / "runner.pid.json"
        command = [
            sys.executable,
            "-u",
            "-c",
            textwrap.dedent(child_source),
        ]
        env = os.environ.copy()
        env.update(extra_env or {})
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--report",
                str(report),
                "--progress",
                str(progress),
                "--status",
                str(status),
                "--timeout-diagnostics",
                str(timeout_diagnostics),
                "--phase-timing",
                str(timing),
                "--pid-file",
                str(pid_file),
                "--timeout-seconds",
                str(timeout),
                "--heartbeat-seconds",
                "0.05",
                "--",
                *command,
            ],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        return result, report, progress, status, timeout_diagnostics, timing, pid_file

    def test_pid_file_failure_cleans_started_process_group(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        root = Path(directory)
        blocked_pid_file = Path("/proc") / f"codex-coverage-pid-{os.getpid()}"
        grandchild_pid_file = root / "grandchild.pid"
        child_source = textwrap.dedent(
            """
            import json
            import os
            import signal
            import subprocess
            import sys
            import time
            grandchild = subprocess.Popen([
                sys.executable,
                '-c',
                'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)',
            ])
            with open(os.environ['GRANDCHILD_PID_FILE'], 'w') as stream:
                stream.write(str(grandchild.pid))
            print(json.dumps({'Action': 'run', 'Package': 'pkg/leak', 'Test': 'TestLeak'}), flush=True)
            time.sleep(30)
            """
        )
        report = root / "report.json"
        progress = root / "progress.txt"
        status = root / "status.json"
        timeout_diagnostics = root / "timeout.txt"
        timing = root / "timing.txt"
        args = run_coverage_ut.parse_args(
            [
                "--report",
                str(report),
                "--progress",
                str(progress),
                "--status",
                str(status),
                "--timeout-diagnostics",
                str(timeout_diagnostics),
                "--phase-timing",
                str(timing),
                "--pid-file",
                str(blocked_pid_file),
                "--timeout-seconds",
                "5",
                "--",
                sys.executable,
                "-u",
                "-c",
                child_source,
            ]
        )
        original_write_json = run_coverage_ut.write_json

        def fail_pid_publication(path, value):
            if path == args.pid_file:
                # Let the child publish its grandchild PID before simulating
                # the disk/permission failure in the ownership record.
                time.sleep(0.2)
                raise OSError("simulated pid-file I/O failure")
            return original_write_json(path, value)

        run_coverage_ut.write_json = fail_pid_publication
        old_grandchild_pid_file = os.environ.get("GRANDCHILD_PID_FILE")
        os.environ["GRANDCHILD_PID_FILE"] = str(grandchild_pid_file)
        try:
            with self.assertRaises(OSError):
                run_coverage_ut.run(args)
        finally:
            run_coverage_ut.write_json = original_write_json
            if old_grandchild_pid_file is None:
                os.environ.pop("GRANDCHILD_PID_FILE", None)
            else:
                os.environ["GRANDCHILD_PID_FILE"] = old_grandchild_pid_file
        grandchild_pid = int(grandchild_pid_file.read_text())
        for _ in range(100):
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            try:
                state = Path(f"/proc/{grandchild_pid}/stat").read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.05)
        else:
            self.fail(f"grandchild {grandchild_pid} survived pid-file failure cleanup")
        try:
            os.kill(grandchild_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_success_records_last_test_and_status(self):
        result, report, progress, status, timeout_diagnostics, timing, pid_file = self.run_wrapper(
            """
            import json
            print(json.dumps({'Action': 'run', 'Package': 'pkg/a', 'Test': 'TestFast'}), flush=True)
            print(json.dumps({'Action': 'pass', 'Package': 'pkg/a', 'Test': 'TestFast', 'Elapsed': 0.2}), flush=True)
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(status.read_text())["result"], "passed")
        self.assertIn("test=TestFast", progress.read_text())
        self.assertIn("phase=coverage_ut status=passed", timing.read_text())
        self.assertFalse(timeout_diagnostics.exists())
        self.assertFalse(pid_file.exists())
        self.assertTrue(report.exists())

    def test_timeout_saves_reason_and_progress_before_killing_group(self):
        result, report, progress, status, timeout_diagnostics, timing, pid_file = self.run_wrapper(
            """
            import json
            import time
            print(json.dumps({'Action': 'run', 'Package': 'pkg/stuck', 'Test': 'TestWait'}), flush=True)
            time.sleep(30)
            """,
            timeout=1,
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        status_data = json.loads(status.read_text())
        self.assertEqual(status_data["result"], "timeout")
        self.assertEqual(status_data["reason"], "internal_deadline")
        self.assertIn("package=pkg/stuck", progress.read_text())
        timeout_text = timeout_diagnostics.read_text()
        self.assertIn("TestWait", timeout_text)
        self.assertIn("coverage_process_pid=", timeout_text)
        self.assertIn("phase=coverage_ut status=timeout", timing.read_text())
        self.assertFalse(pid_file.exists())
        self.assertTrue(report.exists())

    def test_failed_command_keeps_exit_reason_distinct_from_timeout(self):
        result, _report, _progress, status, _timeout_diagnostics, timing, pid_file = self.run_wrapper(
            """
            import json
            print(json.dumps({'Action': 'fail', 'Package': 'pkg/fail', 'Test': 'TestBroken'}), flush=True)
            raise SystemExit(17)
            """
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        status_data = json.loads(status.read_text())
        self.assertEqual(status_data["result"], "failed")
        self.assertEqual(status_data["reason"], "command_exit")
        self.assertIn("phase=coverage_ut status=failed", timing.read_text())
        self.assertFalse(pid_file.exists())

    def test_failed_command_cleans_grandchild_after_group_leader_exits(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        grandchild_pid_file = Path(directory) / "grandchild.pid"
        result, _report, _progress, status, _timeout_diagnostics, _timing, runner_pid_file = self.run_wrapper(
            """
            import json
            import os
            import subprocess
            import sys
            grandchild = subprocess.Popen([
                sys.executable,
                '-c',
                'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)',
            ])
            with open(os.environ['GRANDCHILD_PID_FILE'], 'w') as stream:
                stream.write(str(grandchild.pid))
            print(json.dumps({'Action': 'fail', 'Package': 'pkg/child', 'Test': 'TestChild'}), flush=True)
            raise SystemExit(17)
            """,
            extra_env={"GRANDCHILD_PID_FILE": str(grandchild_pid_file)},
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(json.loads(status.read_text())["result"], "failed")
        self.assertFalse(runner_pid_file.exists())
        grandchild_pid = int(grandchild_pid_file.read_text())
        for _ in range(100):
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            try:
                state = Path(f"/proc/{grandchild_pid}/stat").read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.05)
        else:
            self.fail(f"grandchild {grandchild_pid} survived failed-command cleanup")
        try:
            os.kill(grandchild_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_timeout_kills_grandchild_after_group_leader_exits(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        grandchild_pid_file = Path(directory) / "grandchild.pid"
        result, _report, _progress, status, _timeout_diagnostics, _timing, runner_pid_file = self.run_wrapper(
            """
            import json
            import os
            import signal
            import subprocess
            import sys
            import time
            grandchild = subprocess.Popen([
                sys.executable,
                '-c',
                'import signal,time; signal.signal(signal.SIGQUIT, signal.SIG_IGN); signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)',
            ])
            with open(os.environ['GRANDCHILD_PID_FILE'], 'w') as stream:
                stream.write(str(grandchild.pid))
            print(json.dumps({'Action': 'run', 'Package': 'pkg/child', 'Test': 'TestChild'}), flush=True)
            time.sleep(30)
            """,
            timeout=1,
            extra_env={"GRANDCHILD_PID_FILE": str(grandchild_pid_file)},
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertEqual(json.loads(status.read_text())["result"], "timeout")
        self.assertFalse(runner_pid_file.exists())
        grandchild_pid = int(grandchild_pid_file.read_text())
        for _ in range(100):
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            try:
                state = Path(f"/proc/{grandchild_pid}/stat").read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.05)
        else:
            self.fail(f"grandchild {grandchild_pid} survived process-group cleanup")
        try:
            os.kill(grandchild_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    unittest.main()
