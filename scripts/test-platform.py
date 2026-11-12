#!/usr/bin/env python3
"""Run exact public-web contracts against explicit built framework tools.

The output directory must be new and outside this checkout. Only its evidence/
subdirectory is suitable for upload: work/ contains generated consumer bytecode
and owned server fixtures, never an SDK release. Each loopback harness owns and
reaps its server; this driver adds outer deadlines and verifies their reports.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
UNIT_CHECKS = 1128
WIRE_CHECKS = 38  # Includes the real first-byte deadline check.
STATIC_CHECKS = 15
WIRE_ORACLE_CHECKS = 16
HOST_CHECKS = {"Linux": 87, "Darwin": 87, "Windows": 90}


def require(good: bool, message: str):
    if not good:
        raise AssertionError(message)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_pin(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): digest(path)
            for path in sorted(root.rglob("*")) if path.is_file()}


def source_pin() -> dict[str, str]:
    sources = sorted((REPO / "src").rglob("*.matrix"))
    sources += [path for path in (REPO / "tests").rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
    sources += [REPO / "scripts/test-http-wire.py", Path(__file__).resolve()]
    return {path.relative_to(REPO).as_posix(): digest(path) for path in sorted(set(sources))}


def normalized_machine() -> str:
    raw = platform.machine().lower()
    return {"amd64": "x86_64", "aarch64": "arm64"}.get(raw, raw)


class WindowsJob:
    """An unnamed owned job; descendants inherit it before any command runs."""
    def __init__(self):
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel = self.kernel
        kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL

        class BasicLimits(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                        ("Flags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                        ("MaxWorkingSet", ctypes.c_size_t), ("ActiveLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD),
                        ("Scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("Basic", BasicLimits), ("IO", IoCounters),
                        ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
                        ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("UserTime", ctypes.c_int64), ("KernelTime", ctypes.c_int64),
                        ("PeriodUser", ctypes.c_int64), ("PeriodKernel", ctypes.c_int64),
                        ("PageFaults", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                        ("ActiveProcesses", wintypes.DWORD), ("TerminatedProcesses", wintypes.DWORD)]

        self.Accounting = Accounting
        self.handle = kernel.CreateJobObjectW(None, None)
        require(bool(self.handle), "cannot create owned Windows process job")
        limits = ExtendedLimits()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise AssertionError("cannot set owned Windows job limits")

    def assign(self, process):
        # CPython's Popen owns this stable process handle. The bootstrap is
        # blocked on stdin until assignment succeeds; no descendant can race it.
        require(bool(self.kernel.AssignProcessToJobObject(self.handle, int(process._handle))),
                "cannot assign owned Windows bootstrap to its job")

    def active(self):
        info = self.Accounting()
        returned = ctypes.c_ulong()
        require(bool(self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info),
                    ctypes.sizeof(info), ctypes.byref(returned))) and returned.value == ctypes.sizeof(info),
                "cannot inspect owned Windows job")
        return info.ActiveProcesses != 0

    def terminate(self):
        require(bool(self.kernel.TerminateJobObject(self.handle, 124)), "cannot terminate owned Windows job")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def group_active(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def drain(check, seconds: float) -> bool:
    until = time.monotonic() + seconds
    while check():
        if time.monotonic() >= until:
            return False
        time.sleep(0.025)
    return True


def run(command: list[str], *, cwd: Path, env: dict[str, str], evidence: Path,
        label: str, timeout: int) -> dict:
    started = time.monotonic()
    row = {"command": command, "timeout_seconds": timeout, "cleanup_verified": False}
    process = None
    job = None
    assigned = False
    problem = None
    with (evidence / (label + ".stdout")).open("wb") as stdout, \
         (evidence / (label + ".stderr")).open("wb") as stderr:
        try:
            if os.name == "nt":
                job = WindowsJob()
                # Isolated bootstrap cannot spawn the actual command until its
                # inherited job has been assigned. It waits for that command so
                # Popen's exit status remains the command's status on Windows.
                bootstrap = ("import subprocess,sys; "
                    "go=sys.stdin.buffer.read(1); "
                    "sys.exit(subprocess.call(sys.argv[1:],stdin=subprocess.DEVNULL) if go==b'G' else 125)")
                process = subprocess.Popen([sys.executable, "-I", "-S", "-c", bootstrap, *command],
                    cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
                row["pid"] = process.pid
                job.assign(process)
                assigned = True
                process.stdin.write(b"G")
                process.stdin.flush()
                process.stdin.close()
            else:
                process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, start_new_session=True)
                row["pid"] = process.pid
            row["exit_code"] = process.wait(timeout=timeout)
            if job:
                # Job accounting may retain terminated process references;
                # the exit code is cached, so release Popen's owned handle.
                process._handle.Close()
            active = job.active if job else lambda: group_active(process.pid)
            row["unexpected_descendants"] = not drain(active, 1)
            if row["unexpected_descendants"]:
                problem = AssertionError(label + " left a descendant process after exit")
        except subprocess.TimeoutExpired:
            row["timed_out"] = True
            problem = AssertionError(label + " exceeded its outer deadline")
        except Exception as error:
            problem = error
        finally:
            try:
                if process is not None:
                    if job and assigned:
                        if job.active():
                            job.terminate()
                        process.wait(timeout=5)
                        process._handle.Close()
                        row["cleanup_verified"] = drain(job.active, 5)
                    elif job:
                        # Assignment failed while stdin was still a barrier;
                        # the bootstrap has not been allowed to spawn children.
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=5)
                        process._handle.Close()
                        row["cleanup_verified"] = True
                    else:
                        if group_active(process.pid):
                            try:
                                os.killpg(process.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                        if not drain(lambda: group_active(process.pid), 1):
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        process.wait(timeout=5)
                        row["cleanup_verified"] = drain(lambda: group_active(process.pid), 5)
                else:
                    row["cleanup_verified"] = True
            except Exception as error:
                row["cleanup_error"] = type(error).__name__
                problem = problem or error
            finally:
                if job:
                    job.close()
                row["elapsed_seconds"] = round(time.monotonic() - started, 6)
                (evidence / (label + ".command.json")).write_text(json.dumps(row, indent=2) + "\n")
    if problem:
        raise problem
    require(row["cleanup_verified"], label + " process cleanup could not be verified")
    require(row.get("exit_code") == 0, label + " failed; see preserved logs")
    return row


def verify_unit_output(text: str):
    summaries = re.findall(r"^(\d+) checks, all passed\.$", text, re.MULTILINE)
    require(summaries == [str(UNIT_CHECKS)], "full-suite total does not match the exact inventory")
    require(len(re.findall(r"^  ok   .+$", text, re.MULTILINE)) == UNIT_CHECKS,
            "full-suite per-check results do not match the exact inventory")
    require(not re.search(r"^\s*(?:FAIL|SKIP(?:PED)?|XFAIL|NOT RUN)\b", text, re.MULTILINE | re.IGNORECASE),
            "full suite contains a failure or skipped check")


def verify_wire_oracle_output(text: str):
    totals = re.findall(r"^Ran (\d+) tests in \d+(?:\.\d+)?s$", text, re.MULTILINE)
    require(totals == [str(WIRE_ORACLE_CHECKS)], "wire-oracle total does not match the exact inventory")
    passed = re.findall(r"^(test_[^\r\n]+) \.\.\. ok$", text, re.MULTILINE)
    require(len(passed) == WIRE_ORACLE_CHECKS and len(set(passed)) == WIRE_ORACLE_CHECKS,
            "wire-oracle per-test results include a failure, duplicate or skip")
    require(text.strip().splitlines()[-1] == "OK", "wire-oracle summary contains a failure or skip")


def run_wire_oracle(*, env: dict[str, str], evidence: Path) -> dict:
    result = run([sys.executable, "-B", str(REPO / "tests/test_http_wire_oracle.py"), "-v"],
                 cwd=REPO, env=env, evidence=evidence, label="wire-oracle", timeout=30)
    verify_wire_oracle_output((evidence / "wire-oracle.stderr").read_text())
    result["checks"] = WIRE_ORACLE_CHECKS
    return result


def verify_wire_report(report: dict, *, schema: str, expected: int, roots: list[Path]):
    require(report.get("schema") == schema and report.get("status") == "PASS", "wire report schema or outcome differs")
    require(report.get("source_unchanged") is True and report.get("tools_unchanged") is True,
            "wire source or tool integrity seal failed")
    results = report.get("roots", [])
    require(len(results) == len(roots), "wire standard-library root inventory differs")
    for row, root in zip(results, roots):
        require(Path(row.get("nilstd", "")).resolve() == root, "wire report used an unexpected standard-library root")
        checks = row.get("checks", [])
        require(len(checks) == expected and len({case.get("name") for case in checks}) == expected,
                "wire check count or unique-name inventory differs")
        require(all(case.get("status") == "PASS" for case in checks), "wire checks include a failure or skip")
        require(row.get("compile_exit") == 0 and row.get("server_exit") == 0,
                "wire consumer did not compile and exit cleanly")
        require(row.get("cleanup_verified") is True and row.get("nilstd_unchanged") is True,
                "wire cleanup or standard-library integrity seal failed")


def copy_harness_evidence(work: Path, evidence: Path, label: str, roots: int):
    # Explicit file inventory avoids uploading bytecode, binary fixtures, links,
    # junction targets, source checkouts, or an accidentally staged SDK.
    selected = [Path("report.json")]
    for index in range(roots):
        selected += [Path(str(index)) / name for name in
                     ("compile.stdout", "compile.stderr", "server.stdout", "server.stderr")]
    for relative in selected:
        source = work / label / relative
        if source.is_file() and not source.is_symlink():
            target = evidence / label / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--interpreter", type=Path, required=True)
    parser.add_argument("--host-contract", type=Path, required=True)
    parser.add_argument("--nilstd", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-system", choices=tuple(HOST_CHECKS))
    parser.add_argument("--expected-machine", choices=("x86_64", "arm64"))
    parser.add_argument("--framework-root", type=Path)
    parser.add_argument("--framework-revision")
    args = parser.parse_args()
    if bool(args.framework_root) != bool(args.framework_revision):
        parser.error("--framework-root and --framework-revision must be supplied together")
    args.output = args.output.resolve()
    if args.output == REPO or REPO in args.output.parents:
        parser.error("--output must be outside this source checkout")
    args.output.mkdir(parents=True, exist_ok=False)
    evidence = args.output / "evidence"
    work = args.output / "work"
    evidence.mkdir()
    work.mkdir()
    report = {"schema": "nilweb.platform.v1", "status": "FAIL", "system": platform.system(),
              "machine": normalized_machine(), "python": sys.version, "roots": [],
              "expected": {"unit_checks_per_root": UNIT_CHECKS, "wire_checks_per_root": WIRE_CHECKS,
                           "static_checks_per_root": STATIC_CHECKS, "wire_oracle_checks": WIRE_ORACLE_CHECKS},
              "scope": "host file contract and interpreter web consumers; no native networking or installed SDK qualification"}
    pins, tools, roots_before = {}, {}, {}
    roots = []
    try:
        require(platform.system() in HOST_CHECKS, "unsupported host system")
        require(not args.expected_system or platform.system() == args.expected_system, "unexpected runner operating system")
        require(not args.expected_machine or normalized_machine() == args.expected_machine, "unexpected runner architecture")
        for name in ("compiler", "interpreter", "host_contract"):
            setattr(args, name, getattr(args, name).resolve(strict=True))
        roots = [path.resolve(strict=True) for path in args.nilstd]
        require(len(set(roots)) == len(roots), "duplicate standard-library roots are not independent coverage")
        pins = source_pin()
        tools = {str(path): digest(path) for path in (args.compiler, args.interpreter, args.host_contract)}
        roots_before = {str(root): tree_pin(root) for root in roots}
        report.update(source_sha256=pins, tools_sha256=tools, nilstd_before=roots_before)
        if args.framework_root:
            checkout = args.framework_root.resolve(strict=True)
            expected = args.framework_revision
            require(bool(re.fullmatch(r"[0-9a-f]{40}", expected)), "framework revision must be a full commit SHA")
            actual = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True, timeout=15).strip()
            dirty = subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"], text=True, timeout=15)
            require(actual == expected and not dirty, "framework checkout is not the pinned clean revision")
            report["framework_revision"] = actual
        env = {key: value for key, value in os.environ.items() if not key.startswith(("NIL_", "NY_", "NILWEB_"))}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", TMPDIR=str(work), TMP=str(work), TEMP=str(work))
        report["host"] = run([str(args.host_contract)], cwd=work, env=env, evidence=evidence,
                             label="host", timeout=60)
        expected_host = HOST_CHECKS[platform.system()]
        require((evidence / "host.stdout").read_text().strip() == f"{expected_host} rooted snapshot checks passed",
                "host contract count does not match this platform inventory")
        report["host"]["checks"] = expected_host
        (REPO / "build").mkdir(exist_ok=True)
        for index, root in enumerate(roots):
            bytecode = work / f"unit-{index}.nil"
            row = {"nilstd": str(root)}
            report["roots"].append(row)
            row["compile"] = run([str(args.compiler), "--no-bundle", "-I", str(root), "-I", str(REPO / "src"),
                "-I", str(REPO / "tests"), "--output", str(bytecode), str(REPO / "tests/run-tests.matrix")],
                cwd=REPO, env=env, evidence=evidence, label=f"unit-{index}-compile", timeout=90)
            row["run"] = run([str(args.interpreter), str(bytecode)], cwd=REPO, env=env, evidence=evidence,
                             label=f"unit-{index}", timeout=90)
            verify_unit_output((evidence / f"unit-{index}.stdout").read_text())
            require(not (REPO / "build/static-http-contract-fixture.bin").exists(), "full-suite owned fixture was not cleaned up")
            row.update(checks=UNIT_CHECKS, bytecode_sha256=digest(bytecode), cleanup_verified=True)
        report["wire_oracle"] = run_wire_oracle(env=env, evidence=evidence)
        common = ["--compiler", str(args.compiler), "--interpreter", str(args.interpreter)]
        for root in roots:
            common += ["--nilstd", str(root)]
        for label, script, schema, count, extra in (
            ("wire", REPO / "scripts/test-http-wire.py", "nilweb.http-wire.v1", WIRE_CHECKS, ["--real-deadline"]),
            ("static", REPO / "tests/test_static_snapshot.py", "nilweb.static-snapshot-wire.v1", STATIC_CHECKS, [])):
            report[label] = run([sys.executable, "-B", str(script), *common, "--output", str(work / label), "--parent-process-group", *extra],
                cwd=REPO, env=env, evidence=evidence, label=label + "-runner", timeout=180 * len(roots))
            child_report = json.loads((work / label / "report.json").read_text())
            verify_wire_report(child_report, schema=schema, expected=count, roots=roots)
            report[label].update(checks=count * len(roots), report_sha256=digest(work / label / "report.json"))
        report["status"] = "PASS"
    except Exception as error:
        report["error"] = str(error)
    finally:
        for label in ("wire", "static"):
            copy_harness_evidence(work, evidence, label, len(roots))
        checks = {
            "source_unchanged": lambda: bool(pins) and pins == source_pin(),
            "tools_unchanged": lambda: bool(tools) and all(Path(path).is_file() and digest(Path(path)) == held for path, held in tools.items()),
            "nilstd_unchanged": lambda: bool(roots_before) and all(tree_pin(Path(path)) == held for path, held in roots_before.items()),
        }
        for key, verify in checks.items():
            try:
                report[key] = verify()
            except Exception as error:
                report[key] = False
                report.setdefault("integrity_errors", {})[key] = type(error).__name__
        if args.framework_root:
            try:
                checkout = args.framework_root.resolve()
                current = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True, timeout=15).strip()
                dirty = subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"], text=True, timeout=15)
                report["framework_unchanged"] = current == args.framework_revision and not dirty
            except Exception as error:
                report["framework_unchanged"] = False
                report.setdefault("integrity_errors", {})["framework_unchanged"] = type(error).__name__
            if not report["framework_unchanged"]:
                report["status"] = "FAIL"
        if not all(report[key] for key in checks):
            report["status"] = "FAIL"
        (evidence / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        files = {path.relative_to(evidence).as_posix(): {"sha256": digest(path), "bytes": path.stat().st_size}
                 for path in sorted(evidence.rglob("*")) if path.is_file()}
        (evidence / "manifest.json").write_text(json.dumps({"schema": "nilweb.platform-evidence.v1", "files": files}, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(evidence / "report.json"), "error": report.get("error")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
