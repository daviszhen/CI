#!/usr/bin/env python3
"""Run coverage UTs with a bounded deadline and actionable progress records.

The GitHub Actions job timeout is a last resort.  This wrapper gives the test
process an earlier, controlled deadline so it can dump Go stacks, record the
last package/test event, and leave enough time for the following diagnostic
and artifact-upload steps.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field


@dataclass
class Progress:
    counts: dict[str, int] = field(default_factory=dict)
    last_action: str = ""
    last_package: str = ""
    last_test: str = ""
    last_event_time: str = ""
    last_elapsed: object = None
    report_offset: int = 0
    partial_line: str = ""

    def consume(self, report: Path) -> None:
        if not report.exists():
            return
        with report.open("r", errors="replace") as stream:
            stream.seek(self.report_offset)
            data = self.partial_line + stream.read()
            self.report_offset = stream.tell()
        lines = data.splitlines(keepends=True)
        self.partial_line = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.partial_line = lines.pop()
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            action = event.get("Action")
            if not isinstance(action, str):
                continue
            self.counts[action] = self.counts.get(action, 0) + 1
            package = event.get("Package")
            test = event.get("Test")
            if isinstance(package, str) and package:
                self.last_package = package
            if isinstance(test, str) and test:
                self.last_test = test
            self.last_action = action
            event_time = event.get("Time")
            if isinstance(event_time, str):
                self.last_event_time = event_time
            self.last_elapsed = event.get("Elapsed")

    def snapshot(self, elapsed_seconds: float, report: Path) -> str:
        counts = ",".join(
            f"{key}={self.counts[key]}" for key in sorted(self.counts)
        ) or "none"
        test = self.last_test or "<package>"
        event_time = self.last_event_time or "<none>"
        elapsed = "<none>" if self.last_elapsed is None else str(self.last_elapsed)
        size = report.stat().st_size if report.exists() else 0
        return (
            f"elapsed_seconds={elapsed_seconds:.1f} report_bytes={size} "
            f"last_event_time={event_time} action={self.last_action or '<none>'} "
            f"package={self.last_package or '<none>'} test={test} "
            f"last_test_elapsed={elapsed} events={counts}"
        )


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(line.rstrip("\n") + "\n")


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return


def stop_process_group(process: subprocess.Popen[bytes], pgid: int) -> None:
    # Do not use the group's leader as the liveness check.  A test process can
    # exit after SIGQUIT while a child it spawned is still alive in the same
    # session; the group must be reaped independently of the leader.
    signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while process_group_exists(pgid) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.2)
    if process_group_exists(pgid):
        signal_group(pgid, signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def read_tail(path: Path, max_bytes: int = 256 * 1024) -> str:
    """Read a bounded byte tail without loading the full report into memory."""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - max_bytes), os.SEEK_SET)
        return stream.read(max_bytes).decode(errors="replace")


def dump_timeout_diagnostics(
    path: Path,
    progress_line: str,
    timeout_seconds: int,
    report: Path,
    process: subprocess.Popen[bytes],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as output:
        output.write("Coverage UT internal deadline exceeded.\n")
        output.write(f"deadline_seconds={timeout_seconds}\n")
        output.write(f"{progress_line}\n")
        output.write("signal=SIGQUIT sent to the coverage test process group\n")
        output.write("\nProcess snapshot before signal:\n")
        try:
            snapshot = subprocess.run(
                [
                    "ps",
                    "-eo",
                    "pid,ppid,pgid,stat,etime,rss,cmd",
                    "--sort=-rss",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            output.write(snapshot.stdout)
            if snapshot.stderr:
                output.write(f"ps stderr: {snapshot.stderr}\n")
        except OSError as error:
            output.write(f"unable to capture process snapshot: {error}\n")
        output.write(f"\ncoverage_process_pid={process.pid}\n")
        output.write("The full Go JSON stream, including any stack dump, is in the report artifact.\n")
        if report.exists():
            output.write("\nReport tail (last 256 KiB):\n")
            try:
                output.write(read_tail(report))
            except OSError as error:
                output.write(f"unable to read report tail: {error}\n")


def run(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise ValueError("a command is required after --")

    report = args.report
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    args.progress.unlink(missing_ok=True)
    args.status.unlink(missing_ok=True)
    args.timeout_diagnostics.unlink(missing_ok=True)
    if args.pid_file:
        args.pid_file.unlink(missing_ok=True)
    if args.phase_timing:
        append_line(args.phase_timing, "phase=coverage_ut status=started")

    started = time.monotonic()
    progress = Progress()
    interrupted: list[int] = []

    def handle_signal(signum: int, _frame: object) -> None:
        if not interrupted:
            interrupted.append(signum)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    with report.open("wb") as output:
        process = subprocess.Popen(
            command,
            cwd=args.cwd,
            env=os.environ.copy(),
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        process_group_id = process.pid
        if args.pid_file:
            try:
                write_json(
                    args.pid_file,
                    {
                        "wrapper_pid": os.getpid(),
                        "coverage_pid": process.pid,
                        "process_group_id": process_group_id,
                        "started_at_unix": int(time.time()),
                    },
                )
            except BaseException:
                # The parent watchdog can only clean the child group through
                # this file. If publishing ownership fails after Popen, clean
                # the group here before surfacing the I/O error; otherwise a
                # full disk or permission failure would leak the test tree.
                try:
                    stop_process_group(process, process_group_id)
                finally:
                    args.pid_file.unlink(missing_ok=True)
                raise

        last_heartbeat = 0.0
        last_console_heartbeat = 0.0
        timed_out = False
        while process.poll() is None:
            progress.consume(report)
            now = time.monotonic()
            elapsed = now - started
            if now - last_heartbeat >= args.heartbeat_seconds:
                line = progress.snapshot(elapsed, report)
                append_line(args.progress, line)
                if now - last_console_heartbeat >= args.console_heartbeat_seconds:
                    print(f"::notice title=Coverage UT progress::{line}", flush=True)
                    last_console_heartbeat = now
                last_heartbeat = now
            if interrupted:
                reason = f"received_signal={signal.Signals(interrupted[0]).name}"
                line = progress.snapshot(elapsed, report)
                append_line(args.progress, f"cancelled {line} {reason}")
                stop_process_group(process, process_group_id)
                status = {
                    "result": "cancelled",
                    "exit_code": 143,
                    "reason": reason,
                    "elapsed_seconds": round(elapsed, 1),
                    "progress": line,
                }
                write_json(args.status, status)
                if args.phase_timing:
                    append_line(
                        args.phase_timing,
                        f"phase=coverage_ut status=cancelled elapsed_seconds={elapsed:.1f} {reason}",
                    )
                if args.pid_file:
                    args.pid_file.unlink(missing_ok=True)
                return 143
            if elapsed >= args.timeout_seconds:
                timed_out = True
                line = progress.snapshot(elapsed, report)
                append_line(args.progress, f"timeout {line}")
                dump_timeout_diagnostics(
                    args.timeout_diagnostics,
                    line,
                    args.timeout_seconds,
                    report,
                    process,
                )
                print(
                    "::error title=Coverage UT timeout::"
                    f"internal deadline {args.timeout_seconds}s exceeded; {line}",
                    flush=True,
                )
                signal_group(process_group_id, signal.SIGQUIT)
                dump_deadline = time.monotonic() + 20
                while process.poll() is None and time.monotonic() < dump_deadline:
                    time.sleep(0.2)
                stop_process_group(process, process_group_id)
                break
            time.sleep(min(0.5, max(0.05, args.heartbeat_seconds / 4)))

        progress.consume(report)
        elapsed = time.monotonic() - started
        final_line = progress.snapshot(elapsed, report)
        append_line(args.progress, f"finished {final_line}")

        # Reap the group leader before checking the process group. A command
        # can report an exit code while a helper it spawned is still alive;
        # clean that helper before removing the pid file so an ordinary test
        # failure cannot leak work into later workflow steps.
        process.wait()
        if process_group_exists(process_group_id):
            stop_process_group(process, process_group_id)

    if args.pid_file:
        args.pid_file.unlink(missing_ok=True)

    if timed_out:
        status = {
            "result": "timeout",
            "exit_code": 124,
            "reason": "internal_deadline",
            "elapsed_seconds": round(elapsed, 1),
            "progress": final_line,
        }
        code = 124
    else:
        return_code = process.returncode
        code = return_code if return_code is not None else 1
        status = {
            "result": "passed" if code == 0 else "failed",
            "exit_code": code,
            "reason": "command_exit",
            "elapsed_seconds": round(elapsed, 1),
            "progress": final_line,
        }
    write_json(args.status, status)
    if args.phase_timing:
        append_line(
            args.phase_timing,
            f"phase=coverage_ut status={status['result']} elapsed_seconds={elapsed:.1f}",
        )
    return code


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--timeout-diagnostics", type=Path, required=True)
    parser.add_argument("--phase-timing", type=Path)
    parser.add_argument("--pid-file", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--heartbeat-seconds", type=float, default=15)
    parser.add_argument("--console-heartbeat-seconds", type=float, default=60)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.heartbeat_seconds <= 0:
        parser.error("--heartbeat-seconds must be positive")
    if args.console_heartbeat_seconds <= 0:
        parser.error("--console-heartbeat-seconds must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(sys.argv[1:] if argv is None else argv))
    except (OSError, ValueError) as error:
        print(f"::error title=Coverage UT runner::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
