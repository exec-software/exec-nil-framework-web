#!/usr/bin/env python3
"""Deterministic socket/clock controls for the real HTTP wire test's oracle."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "http_wire_oracle", Path(__file__).resolve().parents[1] / "scripts/test-http-wire.py")
assert SPEC is not None and SPEC.loader is not None
WIRE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WIRE)


def aborted(code=10053):
    error = ConnectionAbortedError("synthetic Winsock abort")
    error.winerror = code
    return error


class SocketFixture:
    def __init__(self, outcome=b"", send_error=None, send_at=21):
        self.outcome = outcome
        self.send_error = send_error
        self.send_at = send_at
        self.sent = []
        self.clock = 0.0

    def recv(self, _):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def sendall(self, data):
        self.sent.append((self.clock, data))
        if self.send_error is not None and self.clock >= self.send_at:
            raise self.send_error


def peer_for(sock):
    peer = object.__new__(WIRE.Peer)
    peer.socket = sock
    peer.buffer = b""
    return peer


class WireOracleTests(unittest.TestCase):
    def closed(self, outcome, host="nt", buffered=b""):
        peer = peer_for(SocketFixture(outcome))
        peer.buffer = buffered
        with patch.object(WIRE.os, "name", host):
            peer.closed()

    def deadline(self, *, close_at=20.0, outcome=b"", host="nt", send_error=None,
                 send_at=21, selection_error=None):
        sock = SocketFixture(outcome, send_error, send_at)

        def select(read, write, errors, timeout):
            self.assertEqual(read, [sock])
            self.assertEqual(write, [])
            self.assertEqual(errors, [])
            self.assertGreaterEqual(timeout, 0)
            if selection_error is not None:
                raise selection_error
            until = sock.clock + timeout
            if close_at is not None and close_at <= until:
                sock.clock = close_at
                return [sock], [], []
            sock.clock = until
            return [], [], []

        with patch.object(WIRE.os, "name", host), \
             patch.object(WIRE.time, "monotonic", side_effect=lambda: sock.clock), \
             patch.object(WIRE.select, "select", side_effect=select):
            WIRE.check_first_byte_deadline(peer_for(sock))
        return sock

    def test_eof_and_reset_are_closed_on_each_host(self):
        for host in ("nt", "posix"):
            for outcome in (b"", ConnectionResetError("synthetic reset")):
                with self.subTest(host=host, outcome=type(outcome).__name__):
                    self.closed(outcome, host)

    def test_exact_windows_abort_is_closed(self):
        self.closed(aborted())

    def test_windows_abort_is_not_accepted_on_posix(self):
        with self.assertRaises(ConnectionAbortedError):
            self.closed(aborted(), "posix")

    def test_different_abort_codes_and_generic_errors_propagate(self):
        for error in (aborted(10052), ConnectionAbortedError("no Winsock code"),
                      OSError(10053, "generic code is insufficient"), OSError("unrelated")):
            with self.subTest(error=repr(error)), self.assertRaises(type(error)):
                self.closed(error)

    def test_receive_timeout_is_not_a_close(self):
        with self.assertRaises(TimeoutError):
            self.closed(socket.timeout("still open"))

    def test_extra_wire_or_buffered_bytes_are_not_a_close(self):
        for received, buffered in ((b"x", b""), (b"", b"previous response bytes")):
            with self.subTest(buffered=bool(buffered)), self.assertRaises(AssertionError):
                self.closed(received, buffered=buffered)

    def test_deadline_accepts_eof_with_continuing_fragments(self):
        sock = self.deadline()
        self.assertEqual([when for when, _ in sock.sent], [0, 3, 6, 9, 12, 15, 18])
        self.assertEqual([body for _, body in sock.sent[1:]], [b"x"] * 6)
        self.assertEqual(sock.clock, 20)

    def test_deadline_accepts_reset_and_windows_abort_at_read(self):
        for outcome in (ConnectionResetError("synthetic reset"), aborted()):
            with self.subTest(outcome=type(outcome).__name__):
                self.assertEqual(self.deadline(outcome=outcome).clock, 20)

    def test_deadline_accepts_closed_send_and_confirms_closed_receive(self):
        for error in (BrokenPipeError("synthetic pipe"), ConnectionResetError("reset"), aborted()):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.deadline(close_at=None, send_error=error,
                                              outcome=aborted()).clock, 21)

    def test_deadline_send_does_not_hide_unrelated_errors_or_timeouts(self):
        for error in (aborted(10052), OSError("unrelated"), socket.timeout("send timed out")):
            with self.subTest(error=repr(error)), self.assertRaises(type(error)):
                self.deadline(close_at=None, send_error=error)

    def test_deadline_does_not_accept_windows_abort_on_posix(self):
        for send in (False, True):
            with self.subTest(send=send), self.assertRaises(ConnectionAbortedError):
                self.deadline(close_at=None if send else 20, host="posix",
                              send_error=aborted() if send else None, outcome=aborted())

    def test_deadline_rejects_early_eof_reset_abort_and_send_failure(self):
        for outcome in (b"", ConnectionResetError("reset"), aborted()):
            with self.subTest(outcome=type(outcome).__name__), self.assertRaisesRegex(AssertionError, "19–25"):
                self.deadline(close_at=18.9, outcome=outcome)
        with self.assertRaisesRegex(AssertionError, "19–25"):
            self.deadline(close_at=None, send_error=aborted(), send_at=3)

    def test_deadline_accepts_lower_and_upper_bound(self):
        for close_at in (19, 25):
            with self.subTest(close_at=close_at):
                self.assertEqual(self.deadline(close_at=close_at).clock, close_at)

    def test_deadline_rejects_open_or_late_connection(self):
        for close_at in (None, 25.1):
            with self.subTest(close_at=close_at), self.assertRaisesRegex(AssertionError, "extended"):
                self.deadline(close_at=close_at)

    def test_deadline_rejects_response_data_and_receive_timeout(self):
        with self.assertRaises(AssertionError):
            self.deadline(outcome=b"unexpected HTTP response")
        with self.assertRaises(TimeoutError):
            self.deadline(outcome=socket.timeout("readiness was not EOF"))
        with self.assertRaises(TimeoutError):
            self.deadline(close_at=None, send_error=aborted(), outcome=socket.timeout("still open"))

    def test_selection_error_is_not_a_close(self):
        with self.assertRaises(OSError):
            self.deadline(selection_error=OSError("select failed"))


if __name__ == "__main__":
    unittest.main()
