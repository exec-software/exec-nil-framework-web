#!/usr/bin/env python3
"""Bounded loopback HTTP tests using explicit SDK tools and external artifacts.

Compiles the small Matrix consumer from source for every --nilstd root. Each
owned server has a 90-second internal lifetime; Python also enforces deadlines
and reaps only its own child/process group. With --parent-process-group, an
outer driver owns the group and this harness reaps only its direct server PID.
No developer server is reused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import time
import uuid

REPO = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin_tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob("*")) if p.is_file()}


def request(target: bytes, fields: bytes = b"", body: bytes = b"", method: bytes = b"GET") -> bytes:
    return method + b" " + target + b" HTTP/1.1\r\nHost: example.com\r\n" + fields + b"\r\n" + body


def windows_connection_aborted(error: OSError) -> bool:
    # WSAECONNABORTED is a closed connection, not a successful socket timeout.
    # Keep this exception specific to Winsock; other abort/error codes fail.
    return (os.name == "nt" and isinstance(error, ConnectionAbortedError)
            and getattr(error, "winerror", None) == 10053)


class Peer:
    def __init__(self, port: int):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.socket.settimeout(5)
        self.buffer = b""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.socket.close()

    def send(self, data: bytes):
        self.socket.sendall(data)

    def response(self) -> tuple[int, dict[bytes, bytes], bytes]:
        while b"\r\n\r\n" not in self.buffer:
            self.receive()
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        lines = head.split(b"\r\n")
        status = int(lines[0].split()[1])
        fields = {}
        for line in lines[1:]:
            key, value = line.split(b":", 1)
            key = key.lower()
            if key in fields:
                raise AssertionError("duplicate response field")
            fields[key] = value.strip()
        size = int(fields[b"content-length"])
        if size > 9 * 1024 * 1024:
            raise AssertionError("unbounded response")
        while len(self.buffer) < size:
            self.receive()
        body, self.buffer = self.buffer[:size], self.buffer[size:]
        return status, fields, body

    def receive(self):
        data = self.socket.recv(65536)
        if not data:
            raise AssertionError("response ended before its boundary")
        self.buffer += data

    def closed(self):
        if self.buffer:
            raise AssertionError("extra response bytes after close")
        try:
            data = self.socket.recv(1)
        except OSError as error:
            if not isinstance(error, ConnectionResetError) and not windows_connection_aborted(error):
                raise
            data = b""
        if data:
            raise AssertionError("connection was reused after rejection/close")


def check_response(peer: Peer, expected: bytes):
    status, _, body = peer.response()
    if status != 200 or body != expected:
        raise AssertionError("response status or exact payload differs")


def check_first_byte_deadline(peer: Peer):
    started = time.monotonic()
    peer.send(b"POST /slow HTTP/1.1\r\nHost: example.com\r\nX-Slow: ")
    next_fragment = started + 3
    until = started + 25
    while True:
        now = time.monotonic()
        if now >= until:
            raise AssertionError("request deadline extended by fragments")
        # Observe FIN/reset as soon as it arrives. Sleeping until the next send
        # could hide an early close behind buffered successful socket writes.
        ready, _, _ = select.select([peer.socket], [], [], min(next_fragment, until) - now)
        if ready:
            peer.closed()
            break
        now = time.monotonic()
        if now >= next_fragment:
            try:
                peer.send(b"x")
            except OSError as error:
                if not isinstance(error, (BrokenPipeError, ConnectionResetError)) and not windows_connection_aborted(error):
                    raise
                peer.closed()
                break
            next_fragment = now + 3
    elapsed = time.monotonic() - started
    if not 19 <= elapsed <= 25:
        raise AssertionError("request closed outside the 19–25 second first-byte deadline window")


def exercise(port: int, rows: list[dict], real_deadline: bool):
    def case(name, fn):
        started = time.monotonic()
        row = {"name": name, "status": "FAIL"}
        rows.append(row)
        try:
            fn()
            row["status"] = "PASS"
        finally:
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)

    def pipeline():
        with Peer(port) as peer:
            peer.send(request(b"/one") + request(b"/two", b"Connection: close\r\n"))
            check_response(peer, b"/one|")
            check_response(peer, b"/two|")
            peer.closed()
    case("two requests in one write retain response order", pipeline)

    def fixed_body():
        body = b"\x00\xff\xc3\xa9\r\nGET /not-a-request"
        with Peer(port) as peer:
            peer.send(request(b"/raw%2fpath?q=%20", b"Content-Length: " + str(len(body)).encode() + b"\r\n", body, b"POST")
                      + request(b"/after-body", b"Connection: close\r\n"))
            check_response(peer, b"/raw%2fpath?q=%20|" + body)
            check_response(peer, b"/after-body|")
            peer.closed()
    case("fixed body preserves binary octets and raw target", fixed_body)

    def fragmented():
        wire = request(b"/fragment", b"Content-Length: 5\r\n", b"abcde", b"POST")
        with Peer(port) as peer:
            for offset in range(0, len(wire), 3):
                peer.send(wire[offset:offset + 3])
                time.sleep(0.002)
            check_response(peer, b"/fragment|abcde")
            peer.send(request(b"/after-fragment", b"Connection: close\r\n"))
            check_response(peer, b"/after-fragment|")
            peer.closed()
    case("fragmented headers and body finish before dispatch", fragmented)

    def chunked():
        body = b"\x00\xff\xc3\xa9"
        chunks = b"2;flag;key=\"a;b\"\r\n" + body[:2] + b"\r\n2\r\n" + body[2:] + b"\r\n0\r\nX-Note: discarded\r\n\r\n"
        wire = request(b"/chunks", b"Transfer-Encoding: Chunked\r\nTrailer: X-Note\r\n", chunks, b"POST")
        with Peer(port) as peer:
            for offset in range(0, len(wire), 7):
                peer.send(wire[offset:offset + 7])
                time.sleep(0.001)
            peer.send(request(b"/after-chunks", b"Connection: close\r\n"))
            check_response(peer, b"/chunks|" + body)
            check_response(peer, b"/after-chunks|")
            peer.closed()
    case("fragmented chunks trailers and pipeline preserve exact octets", chunked)

    def duplicates():
        with Peer(port) as peer:
            peer.send(request(b"/same", b"Content-Length: 003, 3\r\ncontent-length: 3\r\nConnection: close\r\n", b"abc", b"POST"))
            check_response(peer, b"/same|abc")
            peer.closed()
    case("identical repeated and comma lengths accepted", duplicates)

    def rejection(fields: bytes, body=b"", status=400):
        with Peer(port) as peer:
            peer.send(request(b"/must-not-dispatch", fields, body, b"POST") + request(b"/must-not-follow"))
            actual, headers, payload = peer.response()
            if actual != status or headers.get(b"connection") != b"close" or payload:
                raise AssertionError("framing rejection status/close differs")
            peer.closed()

    invalid = [
        ("nondigit length", b"Content-Length: banana\r\n", b"", 400),
        ("negative length", b"Content-Length: -1\r\n", b"", 400),
        ("empty length", b"Content-Length:\r\n", b"", 400),
        ("conflicting length fields", b"Content-Length: 0\r\nContent-Length: 3\r\n", b"abc", 400),
        ("conflicting list lengths", b"Content-Length: 0, 3\r\n", b"abc", 400),
        ("length overflow", b"Content-Length: 999999999999999999999999\r\n", b"", 413),
        ("transfer encoding plus length", b"Transfer-Encoding: chunked\r\nContent-Length: 0\r\n", b"0\r\n\r\n", 400),
        ("unknown final transfer coding", b"Transfer-Encoding: gzip\r\n", b"", 400),
        ("unsupported preceding transfer coding", b"Transfer-Encoding: gzip, chunked\r\n", b"", 501),
        ("duplicate chunked coding", b"Transfer-Encoding: chunked, chunked\r\n", b"", 400),
        ("chunk size overflow", b"Transfer-Encoding: chunked\r\n", b"FFFFFFFFFFFFFFFFFFFF\r\n", 413),
        ("malformed chunk extension", b"Transfer-Encoding: chunked\r\n", b"1;=x\r\n", 400),
        ("chunk data missing CRLF", b"Transfer-Encoding: chunked\r\n", b"1\r\nxNO", 400),
        ("malformed trailer", b"Transfer-Encoding: chunked\r\n", b"0\r\nBad Field: value\r\n\r\n", 400),
        ("whitespace before field colon", b"Content-Length : 0\r\n", b"", 400),
        ("obsolete folded field", b"X-Test: value\r\n continued\r\n", b"", 400),
    ]
    for name, fields, body, status in invalid:
        case(name + " rejects and closes before pipeline", lambda f=fields, b=body, s=status: rejection(f, b, s))

    def expectation(fields: bytes):
        with Peer(port) as peer:
            peer.socket.settimeout(2)
            # Deliberately send headers only: a client awaiting an expectation
            # decision has not sent any of its declared body or first chunk.
            peer.send(request(b"/expect", fields, method=b"POST"))
            status, headers, body = peer.response()
            if status != 417 or headers.get(b"connection") != b"close" or body:
                raise AssertionError("Expect was not rejected immediately from headers")
            peer.closed()
    for name, fields in [
        ("100-continue", b"Content-Length: 5\r\nExpect: 100-continue\r\n"),
        ("unknown expectation", b"Content-Length: 5\r\nExpect: unsupported\r\n"),
        ("mixed expectations", b"Content-Length: 5\r\nExpect: 100-continue, unsupported\r\n"),
        ("chunked expectation", b"Transfer-Encoding: chunked\r\nExpect: 100-continue\r\n"),
        ("duplicate expectation fields", b"Content-Length: 5\r\nExpect: 100-continue\r\nExpect: 100-CONTINUE, 100-continue\r\n"),
        ("empty expectation", b"Content-Length: 5\r\nExpect:\r\n"),
    ]:
        case(name + " receives417 before any body bytes", lambda f=fields: expectation(f))

    def legacy_expectation():
        with Peer(port) as peer:
            peer.send(b"POST /legacy HTTP/1.0\r\nContent-Length: 5\r\nExpect: 100-continue\r\nExpect: 100-CONTINUE, 100-continue\r\n\r\nabcde")
            check_response(peer, b"/legacy|abcde")
            peer.closed()
    case("HTTP1.0 ignores100-continue while preserving its body", legacy_expectation)

    def retry_without_expectation():
        with Peer(port) as peer:
            peer.send(request(b"/expect", b"Content-Length: 5\r\nConnection: close\r\n", b"abcde", b"POST"))
            check_response(peer, b"/expect|abcde")
            peer.closed()
    case("same request succeeds when retried without Expect", retry_without_expectation)

    def half_closed():
        with Peer(port) as peer:
            peer.send(request(b"/half-one") + request(b"/half-two"))
            peer.socket.shutdown(socket.SHUT_WR)
            check_response(peer, b"/half-one|")
            check_response(peer, b"/half-two|")
            peer.closed()
    case("half-close retains two complete buffered requests", half_closed)

    def large_half_closed():
        body = bytes(range(256)) * 600
        with Peer(port) as peer:
            peer.send(request(b"/large-body", b"Content-Length: " + str(len(body)).encode() + b"\r\n", body, b"POST"))
            peer.socket.shutdown(socket.SHUT_WR)
            check_response(peer, b"/large-body|" + body)
            peer.closed()
    case("half-close drains a body larger than one socket read", large_half_closed)

    def incomplete_eof():
        with Peer(port) as peer:
            peer.send(request(b"/incomplete", b"Content-Length: 5\r\n", b"ab", b"POST"))
            peer.socket.shutdown(socket.SHUT_WR)
            peer.closed()
    case("incomplete body on EOF is never dispatched", incomplete_eof)

    def next_partial():
        with Peer(port) as peer:
            peer.send(request(b"/first") + request(b"/partial", b"Content-Length: 5\r\n", b"ab", b"POST"))
            check_response(peer, b"/first|")
            peer.send(b"cde" + request(b"/last", b"Connection: close\r\n"))
            check_response(peer, b"/partial|abcde")
            check_response(peer, b"/last|")
            peer.closed()
    case("partial next body survives first reply and accepts remaining bytes", next_partial)

    def expired_next():
        with Peer(port) as peer:
            peer.send(request(b"/age-next") + b"GET /incomplete HTTP/1.1\r\nHost:")
            check_response(peer, b"/age-next|")
            peer.closed()
    case("reply and response drain preserve buffered next-request deadline", expired_next)

    def retained_deadline():
        with Peer(port) as peer:
            peer.send(request(b"/age-soon") + request(b"/middle") + b"GET /partial HTTP/1.1\r\nHost:")
            check_response(peer, b"/age-soon|")
            check_response(peer, b"/middle|")
            peer.socket.settimeout(3)
            peer.closed()
    case("consuming another buffered frame does not extend surplus deadline", retained_deadline)

    def backpressure():
        with Peer(port) as peer:
            peer.send(request(b"/large") + request(b"/after-large", b"Connection: close\r\n"))
            time.sleep(0.2)
            check_response(peer, b"x" * (2 ** 23))
            check_response(peer, b"/after-large|")
            peer.closed()
    case("partial response writes retain buffered next request", backpressure)

    def error_after_backpressure():
        with Peer(port) as peer:
            peer.send(request(b"/large") + request(b"/invalid-next", b"Content-Length: bad\r\n", method=b"POST"))
            time.sleep(0.2)
            check_response(peer, b"x" * (2 ** 23))
            status, headers, body = peer.response()
            if status != 400 or headers.get(b"connection") != b"close" or body:
                raise AssertionError("rejection overwrote or followed the prior response incorrectly")
            peer.closed()
    case("framing error follows a partially written response intact", error_after_backpressure)

    if real_deadline:
        def deadline():
            with Peer(port) as peer:
                check_first_byte_deadline(peer)
        case("real first-byte deadline survives continuing fragments", deadline)


def stop_owned(process: subprocess.Popen, own_group: bool = True) -> bool:
    group_owned = os.name == "posix" and own_group
    if process.poll() is None:
        try:
            if group_owned:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if group_owned:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)
    if group_owned:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        return False
    return process.poll() is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", required=True, type=Path)
    parser.add_argument("--interpreter", required=True, type=Path)
    parser.add_argument("--nilstd", required=True, type=Path, action="append")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--parent-process-group", action="store_true",
                        help="inherit the outer driver process group; clean up only the owned server PID")
    parser.add_argument("--real-deadline", action="store_true", help="also verify the 20-second first-byte deadline per root")
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.output == REPO or REPO in args.output.parents:
        parser.error("--output must be outside the source checkout")
    args.output.mkdir(parents=True, exist_ok=False)
    sources = [REPO / "src/nilweb/http/framing.matrix", REPO / "src/nilweb/server/engine.matrix", REPO / "tests/wire-server.matrix", Path(__file__).resolve()]
    pins = {str(p.relative_to(REPO)): digest(p) for p in sources}
    tools = {str(p.resolve()): digest(p) for p in (args.compiler, args.interpreter)}
    report = {"schema": "nilweb.http-wire.v1", "status": "FAIL", "source_sha256": pins, "tools_sha256": tools, "roots": [], "parent_process_group": args.parent_process_group}
    try:
        for number, std in enumerate(args.nilstd):
            std = std.resolve(strict=True)
            before = pin_tree(std)
            output = args.output / str(number)
            output.mkdir()
            row = {"nilstd": str(std), "nilstd_before": before, "checks": [], "cleanup_verified": False}
            report["roots"].append(row)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("NIL_", "NY_", "NILWEB_"))}
            env.update({"TMPDIR": str(output), "TMP": str(output), "TEMP": str(output)})
            bytecode = output / "wire.nil"
            command = [str(args.compiler.resolve()), "--no-bundle", "-I", str(std), "-I", str(REPO / "src"), "--output", str(bytecode), str(REPO / "tests/wire-server.matrix")]
            with (output / "compile.stdout").open("wb") as stdout, (output / "compile.stderr").open("wb") as stderr:
                result = subprocess.run(command, cwd=output, env=env, stdout=stdout, stderr=stderr, timeout=90)
            row["compile_exit"] = result.returncode
            if result.returncode:
                raise AssertionError("fixture compilation failed; see compiler logs")
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            env["NILWEB_WIRE_PORT"] = str(port)
            row["port"] = port
            with (output / "server.stdout").open("wb") as stdout, (output / "server.stderr").open("wb") as stderr:
                process = subprocess.Popen([str(args.interpreter.resolve()), str(bytecode)], cwd=output, env=env,
                                           stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=os.name == "posix" and not args.parent_process_group)
                row["pid"] = process.pid
                try:
                    token = ("/__wire_probe_" + uuid.uuid4().hex).encode()
                    until = time.monotonic() + 10
                    while True:
                        if process.poll() is not None:
                            raise AssertionError("owned fixture exited before readiness")
                        try:
                            with Peer(port) as peer:
                                peer.send(request(token, b"Connection: close\r\n"))
                                check_response(peer, token + b"|")
                            break
                        except ConnectionRefusedError:
                            if time.monotonic() >= until:
                                raise AssertionError("owned fixture readiness timed out")
                            time.sleep(0.05)
                    exercise(port, row["checks"], args.real_deadline)
                    with Peer(port) as peer:
                        peer.send(request(b"/__shutdown", b"Connection: close\r\n"))
                        check_response(peer, b"/__shutdown|")
                    process.wait(timeout=5)
                    row["server_exit"] = process.returncode
                    if process.returncode:
                        raise AssertionError("owned fixture exit was not clean")
                finally:
                    row["cleanup_verified"] = stop_owned(process, own_group=not args.parent_process_group)
                    row["nilstd_unchanged"] = before == pin_tree(std)
            if not row["cleanup_verified"] or not row["nilstd_unchanged"]:
                raise AssertionError("cleanup or nilstd integrity check failed")
        report["status"] = "PASS"
    except Exception as error:
        report["error"] = str(error)
    finally:
        report["source_unchanged"] = pins == {str(p.relative_to(REPO)): digest(p) for p in sources}
        report["tools_unchanged"] = tools == {str(p.resolve()): digest(p) for p in (args.compiler, args.interpreter)}
        if not report["source_unchanged"] or not report["tools_unchanged"]:
            report["status"] = "FAIL"
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "checks": sum(len(r["checks"]) for r in report["roots"]), "report": str(args.output / "report.json"), "error": report.get("error")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
