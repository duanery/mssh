"""Shared terminal handling for direct connections and persistent sessions."""

import io
import fcntl
import json
import os
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import types
import unittest
from unittest import mock

from test_session_unit import mssh
from test_session_daemon import ENV, FAKE, SessionTest


class TestTerminalSettings(unittest.TestCase):
    def test_local_settings_and_wire_defaults(self):
        with mock.patch.object(mssh, "term_size", return_value=(120, 40)), \
                mock.patch.dict(os.environ, TERM="test-terminal"):
            settings = mssh._terminal_pty()
        self.assertEqual(settings, {"term": "test-terminal",
                                    "cols": 120, "rows": 40})
        chan = mock.Mock()
        mssh._request_pty(chan, settings)
        chan.get_pty.assert_called_once_with(
            term="test-terminal", width=120, height=40)
        chan.reset_mock()
        mssh._request_pty(chan, {"cols": "90", "rows": "30"})
        chan.get_pty.assert_called_once_with(
            term="xterm-256color", width=90, height=30)

    def test_direct_command_and_session_request_share_settings(self):
        settings = {"term": "test-terminal", "cols": 110, "rows": 35}
        chan = mock.Mock()
        chan.recv_exit_status.return_value = 7
        client = mock.Mock()
        client.get_transport.return_value.open_session.return_value = chan
        opts = types.SimpleNamespace(
            timeout=3, force_tty=True, exec_channel=True, wait=3,
            stdin=False, no_stdin=True, verbose=0)
        with mock.patch.object(mssh, "_terminal_pty", return_value=settings), \
                mock.patch.object(mssh, "_both_ends_are_tty", return_value=True), \
                mock.patch.object(mssh, "stdin_fd", return_value=None), \
                mock.patch.object(mssh, "pump") as pump, \
                mock.patch.object(mssh, "session_exec",
                                  return_value={"status": 7}) as call:
            self.assertEqual(mssh.run_command(client, "true", opts), 7)
            pump.assert_called_once_with(chan, is_tty=True)
            self.assertEqual(mssh.run_session_send("test", "true", opts), 7)
        chan.get_pty.assert_called_once_with(
            term="test-terminal", width=110, height=35)
        self.assertEqual(call.call_args[0][1]["pty"], settings)


class TestTerminalContexts(unittest.TestCase):
    def test_raw_mode_restored_after_exception(self):
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        depth = mssh._RAW["depth"]
        try:
            with self.assertRaises(RuntimeError):
                with mssh._RawTerminal(slave):
                    attrs = termios.tcgetattr(slave)
                    self.assertFalse(attrs[3] & (termios.ECHO | termios.ICANON))
                    self.assertEqual(mssh._RAW["depth"], depth + 1)
                    raise RuntimeError("interrupted")
            self.assertEqual(termios.tcgetattr(slave), before)
            self.assertEqual(mssh._RAW["depth"], depth)
        finally:
            os.close(master)
            os.close(slave)

    def test_disabled_watcher_allocates_nothing(self):
        with mock.patch.object(mssh.os, "pipe") as pipe:
            with mssh._WindowWatcher(enabled=False) as watcher:
                self.assertIsNone(watcher.fileno())
                watcher.pump()
            pipe.assert_not_called()

    def test_direct_and_session_resize_notifications(self):
        previous = signal.getsignal(signal.SIGWINCH)
        for direct in (True, False):
            with self.subTest(direct=direct):
                size = [80, 24]
                callback = mock.Mock()
                conn = mock.Mock()
                with mock.patch.object(mssh, "term_size",
                                       side_effect=lambda: tuple(size)):
                    with mssh._WindowWatcher(
                            on_resize=callback if direct else None) as watcher:
                        rfd = watcher.fileno()
                        watcher.pump(None if direct else conn)
                        size[:] = [120, 40]
                        signal.getsignal(signal.SIGWINCH)(signal.SIGWINCH, None)
                        self.assertTrue(watcher.pending)
                        watcher.pump(None if direct else conn)
                        self.assertFalse(watcher.pending)
                        watcher.pump(None if direct else conn)
                if direct:
                    callback.assert_called_once_with(120, 40)
                    conn.sendall.assert_not_called()
                else:
                    conn.sendall.assert_called_once()
                    self.assertEqual(
                        json.loads(conn.sendall.call_args[0][0].decode()),
                        {"op": "resize", "cols": 120, "rows": 40})
                self.assertEqual(signal.getsignal(signal.SIGWINCH), previous)
                with self.assertRaises(OSError):
                    os.fstat(rfd)

    def test_resize_failure_does_not_end_command(self):
        callback = mock.Mock(side_effect=RuntimeError("channel closed"))
        with mock.patch.object(mssh, "term_size",
                               side_effect=[(80, 24), (100, 30)]):
            with mssh._WindowWatcher(on_resize=callback) as watcher:
                signal.getsignal(signal.SIGWINCH)(signal.SIGWINCH, None)
                watcher.pump()
        callback.assert_called_once_with(100, 30)

    def test_idle_pump_does_not_read_pipe_or_terminal_size(self):
        with mssh._WindowWatcher() as watcher:
            with mock.patch.object(mssh.os, "read") as read, \
                    mock.patch.object(mssh, "term_size") as size:
                watcher.pump(mock.Mock())
            read.assert_not_called()
            size.assert_not_called()

    def test_full_pipe_keeps_pending_resize(self):
        callback = mock.Mock()
        size = [80, 24]
        with mock.patch.object(mssh, "term_size",
                               side_effect=lambda: tuple(size)):
            with mssh._WindowWatcher(on_resize=callback) as watcher:
                handler = signal.getsignal(signal.SIGWINCH)
                with mock.patch.object(mssh.os, "write",
                                       side_effect=BlockingIOError):
                    handler(signal.SIGWINCH, None)
                self.assertTrue(watcher.pending)
                size[:] = [100, 30]
                watcher.pump()
                self.assertFalse(watcher.pending)
        callback.assert_called_once_with(100, 30)

    def test_resize_during_callback_remains_pending(self):
        size = [80, 24]
        sizes = []

        def resize(cols, rows):
            sizes.append((cols, rows))
            if len(sizes) == 1:
                size[:] = [120, 40]
                signal.getsignal(signal.SIGWINCH)(signal.SIGWINCH, None)

        with mock.patch.object(mssh, "term_size",
                               side_effect=lambda: tuple(size)):
            with mssh._WindowWatcher(on_resize=resize) as watcher:
                size[:] = [100, 30]
                signal.getsignal(signal.SIGWINCH)(signal.SIGWINCH, None)
                watcher.pump()
                self.assertTrue(watcher.pending)
                watcher.pump()
                self.assertFalse(watcher.pending)
        self.assertEqual(sizes, [(100, 30), (120, 40)])


class TestDirectPump(unittest.TestCase):
    def test_partial_stdin_eof_and_separate_binary_output(self):
        rfd, wfd = os.pipe()
        os.write(wfd, b"input")
        os.close(wfd)
        chan = mock.Mock()
        chunks = [b"\x00stdout\xff", b""]
        errors = [b"stderr\n", b""]
        received = bytearray()
        ended = [False]

        def send(data):
            if not received:
                raise_once[0] += 1
                if raise_once[0] == 1:
                    raise socket.timeout()
            received.extend(data[:2])
            return min(2, len(data))

        raise_once = [0]
        chan.recv_ready.side_effect = lambda: bool(chunks[0])
        chan.recv.side_effect = lambda _size: chunks.pop(0)
        chan.recv_stderr_ready.side_effect = lambda: bool(errors[0])
        chan.recv_stderr.side_effect = lambda _size: errors.pop(0)
        chan.send.side_effect = send
        chan.shutdown_write.side_effect = lambda: ended.__setitem__(0, True)
        chan.exit_status_ready.side_effect = lambda: ended[0]
        out, err = io.BytesIO(), io.BytesIO()
        try:
            with mock.patch.object(mssh, "stdin_fd", return_value=rfd), \
                    mock.patch.object(mssh.sys, "stdout",
                                      types.SimpleNamespace(buffer=out)), \
                    mock.patch.object(mssh.sys, "stderr",
                                      types.SimpleNamespace(buffer=err)), \
                    mock.patch.object(mssh.select, "select",
                                      side_effect=lambda r, w, x, timeout: (r, [], [])):
                mssh.pump(chan, initial=b"banner\n")
            self.assertEqual(received, b"input")
            self.assertEqual(out.getvalue(), b"banner\n\x00stdout\xff")
            self.assertEqual(err.getvalue(), b"stderr\n")
            chan.shutdown_write.assert_called_once_with()
        finally:
            os.close(rfd)

    def test_keyboard_interrupt_restores_terminal_and_signal(self):
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        previous = signal.getsignal(signal.SIGWINCH)
        chan = mock.Mock()
        chan.recv_ready.return_value = False
        chan.recv_stderr_ready.return_value = False
        chan.exit_status_ready.side_effect = KeyboardInterrupt
        try:
            with mock.patch.object(mssh, "stdin_fd", return_value=slave):
                with self.assertRaises(KeyboardInterrupt):
                    mssh.pump(chan, is_tty=True)
            self.assertEqual(termios.tcgetattr(slave), before)
            self.assertEqual(signal.getsignal(signal.SIGWINCH), previous)
        finally:
            os.close(master)
            os.close(slave)


class TestTerminalIntegration(SessionTest):
    def test_direct_and_session_pty_resize(self):
        self.start()
        command = ("printf 'MSSH_READY\\n'; stty size; "
                   "IFS= read -r line; stty size; printf 'MSSH_DONE\\n'")
        for session in (False, True):
            with self.subTest(session=session):
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ,
                            struct.pack("HHHH", 24, 80, 0, 0))
                args = ([self.name, "--exec", "-t", command] if session else
                        ["root@10.0.0.1", "-t", "-c", command])
                process = subprocess.Popen(
                    [sys.executable, FAKE] + args, env=ENV,
                    stdin=slave, stdout=slave, stderr=slave,
                    start_new_session=True)
                output = bytearray()

                def read_until(marker):
                    deadline = time.monotonic() + 10
                    while marker not in output and time.monotonic() < deadline:
                        if select.select([master], [], [], 0.1)[0]:
                            output.extend(os.read(master, 65536))
                    self.assertIn(marker, output)

                try:
                    read_until(b"24 80")
                    # READY may precede entry into the local watcher by one
                    # scheduling turn; let it install SIGWINCH first.
                    time.sleep(0.1)
                    fcntl.ioctl(slave, termios.TIOCSWINSZ,
                                struct.pack("HHHH", 37, 112, 0, 0))
                    process.send_signal(signal.SIGWINCH)
                    time.sleep(0.1)
                    os.write(master, b"continue\n")
                    read_until(b"MSSH_DONE")
                    self.assertIn(b"37 112", output)
                    self.assertEqual(process.wait(timeout=10), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
                    os.close(master)
                    os.close(slave)
