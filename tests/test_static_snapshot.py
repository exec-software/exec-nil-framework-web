#!/usr/bin/env python3
"""Real bounded loopback static responses from explicit candidate SDK tools.

Each consumer is compiled independently for every --nilstd root. All files,
links, ports and subprocesses belong to a newly created external artifact
folder. No existing server or installed payload is modified. With
--parent-process-group, an outer driver owns the process group and this harness
reaps only its direct server PID.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import uuid
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
_SPEC = importlib.util.spec_from_file_location("nilweb_wire_helpers", REPO / "scripts/test-http-wire.py")
assert _SPEC and _SPEC.loader
WIRE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(WIRE)


class StaticPeer(WIRE.Peer):
    deadline: float | None = None

    def remaining(self):
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("absolute response deadline exceeded")
            self.socket.settimeout(remaining)

    def receive(self):
        self.remaining()
        super().receive()

    def closed(self):
        self.remaining()
        super().closed()

    def response(self, head: bool = False) -> tuple[int, dict[bytes, bytes], bytes]:
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) > 65536:
                raise AssertionError("response header bound exceeded")
            self.receive()
        header, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        lines = header.split(b"\r\n")
        status = int(lines[0].split()[1])
        fields = {}
        for line in lines[1:]:
            name, value = line.split(b":", 1)
            name = name.lower()
            if name in fields:
                raise AssertionError("duplicate response header")
            fields[name] = value.strip()
        no_body = head or status in (204, 304) or 100 <= status < 200
        size = 0 if no_body else int(fields[b"content-length"])
        if not 0 <= size <= 3 * 1024 * 1024:
            raise AssertionError("response body bound exceeded")
        while len(self.buffer) < size:
            self.receive()
        payload, self.buffer = self.buffer[:size], self.buffer[size:]
        return status, fields, payload


def exchange(port: int, target: str, *, fields: dict[str, str] | None = None,
             method: str = "GET", deadline: float = 5):
    header = b"Connection: close\r\n" + b"".join(
        name.encode("ascii") + b": " + value.encode("ascii") + b"\r\n"
        for name, value in (fields or {}).items())
    with StaticPeer(port) as peer:
        peer.deadline = time.monotonic() + deadline
        peer.socket.settimeout(deadline)
        peer.send(WIRE.request(quote(target, safe="/%").encode("ascii"), header, method=method.encode("ascii")))
        answer = peer.response(head=method == "HEAD")
        peer.closed()
        return answer


def entity_tag(data: bytes) -> bytes:
    return b'"' + hashlib.sha256(data).hexdigest().encode("ascii") + b'"'


def require(good: bool, message: str):
    if not good:
        raise AssertionError(message)


def directory_link(path: Path, target: Path):
    if os.name != "nt":
        path.symlink_to(target, target_is_directory=True)
        return
    # A junction is an actual reparse point and requires no symlink privilege.
    # Every target and link is inside this harness's fresh artifact directory.
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    shown = str(target.resolve()).encode("utf-16-le")
    substitute = ("\\??\\" + str(target.resolve())).encode("utf-16-le")
    names = substitute + b"\0\0" + shown + b"\0\0"
    raw = struct.pack("<IHHHHHH", 0xA0000003, 8 + len(names), 0,
                      0, len(substitute), len(substitute) + 2, len(shown)) + names
    require(len(raw) < 16384, "owned junction path is too long")
    path.mkdir()
    held = kernel.CreateFileW(str(path), 0x40000000, 0, None, 3, 0x02200000, None)
    require(held not in (None, ctypes.c_void_p(-1).value), "cannot open owned junction")
    try:
        buffer = ctypes.create_string_buffer(raw)
        returned = wintypes.DWORD()
        require(bool(kernel.DeviceIoControl(held, 0x000900A4, buffer, len(raw), None, 0,
                                             ctypes.byref(returned), None)), "cannot create owned junction")
    finally:
        kernel.CloseHandle(held)


def prepare(output: Path):
    root = output / "public"
    outside = output / "outside"
    (root / "nested").mkdir(parents=True)
    outside.mkdir()
    binary = bytes(range(256)) + b"\x00\xffUTF-8:\xc3\xa9\r\n"
    asset = root / "nested/asset.bin"
    asset.write_bytes(binary)
    stamp = 1700000000123000000
    os.utime(asset, ns=(stamp, stamp))
    (outside / "asset.bin").write_bytes(b"outside-owned-marker")
    (root / "empty.bin").write_bytes(b"")
    (root / "oversized.bin").write_bytes(b"x" * (2097152 + 1))
    large = bytes(range(256)) * 4096
    (root / "large.bin").write_bytes(large)
    (root / "space λ.bin").write_bytes(binary)
    directory_link(root / "linked", outside)
    directory_link(root / "inside-link", root / "nested")
    if os.name == "nt":
        directory_link(root / "final-link", outside)
    else:
        (root / "final-link").symlink_to(outside / "asset.bin")
    return root, binary, large, stamp


def exercise(port: int, output: Path, row: dict, binary: bytes, large: bytes, stamp: int, benchmark_deadline: float):
    def case(name, operation):
        check = {"name": name, "status": "FAIL"}
        row["checks"].append(check)
        started = time.monotonic()
        try:
            operation()
            check["status"] = "PASS"
        finally:
            check["elapsed_seconds"] = round(time.monotonic() - started, 6)

    def full():
        status, fields, body = exchange(port, "/nested/asset.bin")
        require(status == 200 and body == binary, "binary static payload differs")
        require(fields[b"etag"] == entity_tag(binary), "binary static SHA-256 differs from Python")
        require(int(fields[b"content-length"]) == len(binary), "binary byte length differs")
        require(fields[b"last-modified"] == b"Tue, 14 Nov 2023 22:13:20 GMT", "snapshot modification date differs")
        require(fields[b"accept-ranges"] == b"bytes", "full response does not advertise ranges")
    case("binary body content digest and same-snapshot date", full)

    def partial():
        status, fields, body = exchange(port, "/nested/asset.bin", fields={"Range": "bytes=0-255", "If-Range": entity_tag(binary).decode()})
        require(status == 206 and body == binary[:256], "binary range bytes differ")
        require(fields[b"etag"] == entity_tag(binary), "partial response lost full representation ETag")
        require(fields[b"content-range"] == f"bytes 0-255/{len(binary)}".encode(), "Content-Range differs")
        require(fields[b"cache-control"] == b"public, max-age=60" and b"last-modified" in fields, "partial cache metadata missing")
    case("strong If-Range returns exact binary partial bytes", partial)

    def head():
        status, fields, body = exchange(port, "/nested/asset.bin", method="HEAD", fields={"Range": "bytes=999999-"})
        require(status == 200 and not body, "HEAD did not ignore Range or sent a body")
        require(int(fields[b"content-length"]) == len(binary) and b"content-range" not in fields, "HEAD headers differ from full GET")
    case("HEAD ignores range and sends only full representation headers", head)

    def revalidated():
        status, fields, body = exchange(port, "/nested/asset.bin", fields={"If-None-Match": entity_tag(binary).decode(), "Range": "bytes=999999-"})
        require(status == 304 and not body, "conditional request did not precede Range")
        require(b"content-length" not in fields and b"content-range" not in fields, "304 contains partial/body framing")
        require(fields[b"etag"] == entity_tag(binary), "304 validator differs")
    case("304 precedes unsatisfiable range without body framing", revalidated)

    def stale_metadata():
        replacement = b"Z" + binary[1:]
        asset = output / "public/nested/asset.bin"
        before = asset.stat()
        asset.write_bytes(replacement)
        os.utime(asset, ns=(stamp, stamp))
        after = asset.stat()
        require(before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns,
                "same-metadata control did not preserve exact size and mtime")
        old = entity_tag(binary).decode()
        for conditions in ({"If-None-Match": old}, {"If-Range": old, "Range": "bytes=0-2"}):
            status, fields, body = exchange(port, "/nested/asset.bin", fields=conditions)
            require(status == 200 and body == replacement and fields[b"etag"] == entity_tag(replacement),
                    "same-metadata edit reused stale validator or range")
        row["same_metadata_control"] = {"size": after.st_size, "mtime_ns": after.st_mtime_ns,
            "before_sha256": hashlib.sha256(binary).hexdigest(), "after_sha256": hashlib.sha256(replacement).hexdigest()}
    case("same-size same-mtime edits invalidate conditional and If-Range requests", stale_metadata)

    def refused_link(target):
        status, fields, body = exchange(port, target)
        require(status == 404 and b"outside-owned-marker" not in body, "link exposed an owned outside marker")
        require(b"etag" not in fields, "refused path leaked a representation validator")
    for name, target in [("intermediate outside directory link", "/linked/asset.bin"),
                         ("intermediate inside directory link", "/inside-link/asset.bin"),
                         ("final symlink or junction", "/final-link")]:
        case(name + " is refused as 404", lambda target=target: refused_link(target))
    for target in ("/../outside/asset.bin", "/%2e%2e/outside/asset.bin", "/missing.bin"):
        case("ordinary unsafe or missing target is 404: " + target, lambda target=target: refused_link(target))

    def empty():
        status, fields, body = exchange(port, "/empty.bin", fields={"Range": "bytes=-10"})
        require(status == 200 and body == b"" and fields[b"content-length"] == b"0", "empty file response differs")
        require(b"content-range" not in fields and fields[b"etag"] == entity_tag(b""), "empty range or ETag differs")
    case("empty representation has valid zero-length framing", empty)

    def oversized():
        status, _, body = exchange(port, "/oversized.bin", fields={"If-None-Match": "*", "Range": "bytes=0-1"})
        require(status == 413 and len(body) < 1024, "oversized file did not precede conditions and ranges")
    case("oversized file is bounded before conditional or range handling", oversized)

    def unicode_name():
        status, fields, body = exchange(port, "/space λ.bin")
        require(status == 200 and body == binary and fields[b"etag"] == entity_tag(binary), "Unicode filename or payload differs")
    case("UTF-8 filename reaches the exact owned static file", unicode_name)

    def benchmark():
        started = time.monotonic()
        status, fields, body = exchange(port, "/large.bin", deadline=benchmark_deadline)
        elapsed = time.monotonic() - started
        require(status == 200 and body == large and fields[b"etag"] == entity_tag(large), "one MiB payload or digest differs")
        require(elapsed < benchmark_deadline, "one MiB response exceeded benchmark deadline")
        row["one_mib"] = {"bytes": len(body), "elapsed_seconds": round(elapsed, 6), "deadline_seconds": benchmark_deadline,
                          "sha256": hashlib.sha256(body).hexdigest()}
    case("bounded one MiB real static response", benchmark)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", required=True, type=Path)
    parser.add_argument("--interpreter", required=True, type=Path)
    parser.add_argument("--nilstd", required=True, type=Path, action="append")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--parent-process-group", action="store_true",
                        help="inherit the outer driver process group; clean up only the owned server PID")
    parser.add_argument("--benchmark-deadline", type=float, default=10)
    args = parser.parse_args()
    if not 0 < args.benchmark_deadline <= 30:
        parser.error("--benchmark-deadline must be positive and at most 30 seconds")
    args.output = args.output.resolve()
    if args.output == REPO or REPO in args.output.parents:
        parser.error("--output must be outside the source checkout")
    args.output.mkdir(parents=True, exist_ok=False)
    sources = sorted((REPO / "src").rglob("*.matrix")) + [Path(__file__).resolve(), REPO / "tests/static-server.matrix", REPO / "scripts/test-http-wire.py"]
    pins = {str(path.relative_to(REPO)): WIRE.digest(path) for path in sources}
    tools = {str(path.resolve()): WIRE.digest(path) for path in (args.compiler, args.interpreter)}
    report = {"schema": "nilweb.static-snapshot-wire.v1", "status": "FAIL", "source_sha256": pins,
              "tools_sha256": tools, "roots": [], "parent_process_group": args.parent_process_group}
    try:
        for number, std in enumerate(args.nilstd):
            std = std.resolve(strict=True)
            before = WIRE.pin_tree(std)
            output = args.output / str(number)
            output.mkdir()
            row = {"nilstd": str(std), "nilstd_before": before, "checks": [], "cleanup_verified": False}
            report["roots"].append(row)
            root, binary, large, stamp = prepare(output)
            row["link_kind"] = "junction" if os.name == "nt" else "symlink"
            env = {key: value for key, value in os.environ.items() if not key.startswith(("NIL_", "NY_", "NILWEB_"))}
            env.update({"TMPDIR": str(output), "TMP": str(output), "TEMP": str(output)})
            bytecode = output / "static.nil"
            command = [str(args.compiler.resolve()), "--no-bundle", "-I", str(std), "-I", str(REPO / "src"),
                       "--output", str(bytecode), str(REPO / "tests/static-server.matrix")]
            with (output / "compile.stdout").open("wb") as stdout, (output / "compile.stderr").open("wb") as stderr:
                result = subprocess.run(command, cwd=output, env=env, stdout=stdout, stderr=stderr, timeout=90)
            row["compile_exit"] = result.returncode
            if result.returncode:
                raise AssertionError("static fixture compilation failed; see compiler logs")
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            token = uuid.uuid4().hex
            env.update({"NILWEB_STATIC_PORT": str(port), "NILWEB_STATIC_ROOT": str(root), "NILWEB_STATIC_TOKEN": token})
            row["port"] = port
            with (output / "server.stdout").open("wb") as stdout, (output / "server.stderr").open("wb") as stderr:
                process = subprocess.Popen([str(args.interpreter.resolve()), str(bytecode)], cwd=output, env=env,
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=os.name == "posix" and not args.parent_process_group)
                row["pid"] = process.pid
                try:
                    until = time.monotonic() + 10
                    while True:
                        if process.poll() is not None:
                            raise AssertionError("owned static fixture exited before readiness")
                        try:
                            status, _, body = exchange(port, "/__probe_" + token)
                            require(status == 200 and body == token.encode(), "readiness did not identify this owned fixture")
                            break
                        except ConnectionRefusedError:
                            if time.monotonic() >= until:
                                raise AssertionError("owned static fixture readiness timed out")
                            time.sleep(0.05)
                    exercise(port, output, row, binary, large, stamp, args.benchmark_deadline)
                    status, _, body = exchange(port, "/__shutdown_" + token)
                    require(status == 200 and body == token.encode(), "owned fixture shutdown acknowledgement differs")
                    process.wait(timeout=5)
                    row["server_exit"] = process.returncode
                    require(process.returncode == 0, "owned fixture exit was not clean")
                finally:
                    row["cleanup_verified"] = WIRE.stop_owned(process, own_group=not args.parent_process_group)
                    row["nilstd_unchanged"] = before == WIRE.pin_tree(std)
            require(row["cleanup_verified"] and row["nilstd_unchanged"], "child cleanup or standard-library integrity failed")
        report["status"] = "PASS"
    except Exception as error:
        report["error"] = str(error)
    finally:
        for row in report["roots"]:
            row["nilstd_unchanged"] = row["nilstd_before"] == WIRE.pin_tree(Path(row["nilstd"]))
            if not row["nilstd_unchanged"]:
                report["status"] = "FAIL"
        report["source_unchanged"] = pins == {str(path.relative_to(REPO)): WIRE.digest(path) for path in sources}
        report["tools_unchanged"] = tools == {str(path.resolve()): WIRE.digest(path) for path in (args.compiler, args.interpreter)}
        if not report["source_unchanged"] or not report["tools_unchanged"]:
            report["status"] = "FAIL"
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "checks": sum(len(row["checks"]) for row in report["roots"]),
                      "report": str(args.output / "report.json"), "error": report.get("error")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
