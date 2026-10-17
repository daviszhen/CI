"""Bounded, read-only resource samples for coverage UTs (cgroup v1 and v2)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess


CGROUP_FILES = {
    "v1": (
        "memory.usage_in_bytes", "memory.limit_in_bytes",
        "memory.max_usage_in_bytes", "memory.failcnt", "memory.oom_control",
        "memory.stat", "memory.soft_limit_in_bytes",
    ),
    "v2": (
        "memory.current", "memory.max", "memory.peak", "memory.events",
        "memory.events.local", "memory.stat", "memory.high", "memory.pressure",
    ),
}
MAX_FILE_BYTES = 32768


def read_file(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        text = data[:MAX_FILE_BYTES].decode(errors="replace").strip()
        return text + ("\n<truncated>" if len(data) > MAX_FILE_BYTES else "")
    except OSError as error:
        return f"unavailable: {error}"


def cgroup_paths(proc: Path, cgroup: Path) -> list[tuple[str, Path]]:
    # Try both namespaced mount roots and the process membership below them.
    # Do not assume v2 merely because /sys/fs/cgroup exists: Guangzhou uses v1.
    paths = [("v2", cgroup), ("v1", cgroup), ("v1", cgroup / "memory")]
    mounts = []
    for line in read_file(proc / "self/mountinfo").splitlines():
        parts = line.split(" - ", 1)
        if len(parts) != 2:
            continue
        fields, filesystem = parts[0].split(), parts[1].split()
        if len(fields) < 5 or len(filesystem) < 3:
            continue
        if filesystem[0] == "cgroup2":
            version = "v2"
        elif filesystem[0] == "cgroup" and "memory" in filesystem[2].split(","):
            version = "v1"
        else:
            continue
        # mountinfo escapes whitespace/backslashes with octal sequences.
        root, mountpoint = (
            Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), field))
            for field in fields[3:5]
        )
        mounts.append((version, root, mountpoint))
    for line in read_file(proc / "self/cgroup").splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        _, controllers, membership = fields
        relative = Path(membership.lstrip("/"))
        # A cgroup namespace can expose ancestors as /../...; never escape
        # the controller mount while resolving a diagnostic path.
        if ".." in relative.parts:
            continue
        if controllers == "":
            version, mount = "v2", cgroup
        elif "memory" in controllers.split(","):
            version, mount = "v1", cgroup / "memory"
        else:
            continue
        candidates = [(mount, relative)]
        for mounted_version, root, mountpoint in mounts:
            if mounted_version != version:
                continue
            try:
                mounted_relative = Path(membership).relative_to(root)
            except ValueError:
                # Namespaced membership '/' can refer to a non-root subtree
                # exposed at the controller's mountpoint.
                if membership != "/":
                    continue
                mounted_relative = Path(".")
            candidates.append((mountpoint, mounted_relative))
        # A finite parent limit can be authoritative even if the leaf is
        # unlimited. Retain ancestors too, bounded independently of depth.
        for mountpoint, member in candidates:
            for parent in [member, *member.parents][:16]:
                paths.append((version, mountpoint / parent))
    return list(dict.fromkeys(paths))


def snapshot(proc: Path = Path("/proc"), cgroup: Path = Path("/sys/fs/cgroup")) -> dict:
    groups = []
    for version, path in cgroup_paths(proc, cgroup):
        if not (path / CGROUP_FILES[version][0]).exists():
            continue
        groups.append({
            "version": version, "path": str(path),
            "files": {name: read_file(path / name) for name in CGROUP_FILES[version]},
        })
    try:
        # comm, not args: process attribution must not copy secret-bearing
        # command lines into artifacts. Keep this diagnostic bounded in time.
        result = subprocess.run(
            ["ps", "-eo", "pid,ppid,rss,vsz,stat,etime,comm", "--sort=-rss"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        processes = "\n".join(result.stdout.splitlines()[:33])
        if result.returncode:
            processes = f"unavailable: ps exit {result.returncode}"
    except (OSError, subprocess.TimeoutExpired) as error:
        processes = f"unavailable: {error}"
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "cgroups": groups,
        "meminfo": read_file(proc / "meminfo"),
        "processes_by_rss": processes,
    }


def append_sample(path: Path, elapsed_seconds: float) -> None:
    try:
        sample = snapshot()
        sample["elapsed_seconds"] = round(elapsed_seconds, 3)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write(json.dumps(sample, sort_keys=True) + "\n")
    except OSError as error:
        # Diagnostics must not turn a test failure/cancellation into a pass,
        # or prevent the wrapper from terminating its owned process group.
        print(f"::warning::coverage resource sample failed: {error}", flush=True)
