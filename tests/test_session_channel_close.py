"""Paramiko channel-close regressions, compatible with Python 3.6."""

import importlib.util
import os
import socket
import threading
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

import paramiko


PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mssh")
SPEC = importlib.util.spec_from_loader(
    "mssh_close", SourceFileLoader("mssh_close", PATH))
mssh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mssh)


class TestSessionChannelClose(unittest.TestCase):
    def test_original_close_exposes_closed_pipe_to_late_feed(self):
        chan = paramiko.Channel(1)
        chan.fileno()
        pipe = chan._pipe
        errors = []

        def close_pipe():
            os.close(pipe._rfd)
            os.close(pipe._wfd)
            try:
                chan.in_buffer.feed(b"late remote data")
            except OSError as exc:
                errors.append(exc)
            finally:
                pipe._closed = True

        try:
            with mock.patch.object(pipe, "close", close_pipe):
                chan.close()
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0].errno, 9)
        finally:
            chan.close()

    def test_feed_while_pipe_close_is_in_progress(self):
        for stream in ("in_buffer", "in_stderr_buffer"):
            with self.subTest(stream=stream):
                chan = paramiko.Channel(1)
                chan.fileno()
                pipe = chan._pipe
                closed = threading.Event()
                fed = threading.Event()
                errors = []

                def close_pipe():
                    # Reproduce PosixPipe.close's vulnerable interval:
                    # descriptors are closed, but _closed is still False.
                    os.close(pipe._rfd)
                    os.close(pipe._wfd)
                    closed.set()
                    if not fed.wait(3):
                        errors.append(RuntimeError("feed did not finish"))
                    pipe._closed = True

                def feed():
                    try:
                        if not closed.wait(3):
                            raise RuntimeError("close did not start")
                        getattr(chan, stream).feed(b"late remote data")
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        fed.set()

                worker = threading.Thread(target=feed)
                worker.start()
                try:
                    with mock.patch.object(pipe, "close", close_pipe):
                        mssh._close_session_channel(chan)
                finally:
                    closed.set()
                    worker.join(5)
                    chan.close()
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertIsNone(chan._pipe)
                self.assertEqual(getattr(chan, stream).read(100),
                                 b"late remote data")
                mssh._close_session_channel(chan)

    def test_close_waits_for_inflight_notification(self):
        chan = paramiko.Channel(1)
        chan.fileno()
        pipe = chan._pipe
        notifying = threading.Event()
        release = threading.Event()
        closing = threading.Event()
        errors = []
        original = pipe.set

        def notify():
            notifying.set()
            if not release.wait(3):
                raise RuntimeError("notification not released")
            original()

        def feed():
            try:
                chan.in_buffer.feed(b"data")
            except BaseException as exc:
                errors.append(exc)

        def close():
            try:
                closing.set()
                mssh._close_session_channel(chan)
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(pipe, "set", notify):
            feeder = threading.Thread(target=feed)
            closer = threading.Thread(target=close)
            feeder.start()
            try:
                self.assertTrue(notifying.wait(3))
                closer.start()
                self.assertTrue(closing.wait(3))
                self.assertFalse(pipe._closed)
            finally:
                release.set()
                feeder.join(5)
                if closer.ident is not None:
                    closer.join(5)
                chan.close()
        self.assertEqual(errors, [])
        self.assertTrue(pipe._closed)

    def test_non_paramiko_channel_keeps_close_behavior(self):
        chan = mock.Mock()
        mssh._close_session_channel(chan)
        chan.close.assert_called_once_with()

    def test_repeated_closes_keep_shared_transport_alive(self):
        class Server(paramiko.ServerInterface):
            def check_auth_none(self, username):
                return paramiko.AUTH_SUCCESSFUL

            def check_channel_request(self, kind, chanid):
                return paramiko.OPEN_SUCCEEDED

        left, right = socket.socketpair()
        client = paramiko.Transport(left)
        server = paramiko.Transport(right)
        workers = []
        errors = []
        try:
            server.add_server_key(paramiko.RSAKey.generate(1024))
            ready = threading.Event()
            server.start_server(event=ready, server=Server())
            client.start_client(timeout=5)
            client.auth_none("test")
            self.assertTrue(ready.wait(5))
            for _ in range(20):
                incoming = client.open_session(timeout=5)
                outgoing = server.accept(5)
                self.assertIsNotNone(outgoing)
                incoming.fileno()
                stop = threading.Event()

                def send(chan=outgoing, done=stop):
                    try:
                        while not done.is_set():
                            chan.send(b"x" * 32768)
                            chan.send_stderr(b"y" * 32768)
                    except (OSError, EOFError):
                        pass  # Closing this channel is expected.
                    except BaseException as exc:
                        errors.append(exc)

                outgoing.settimeout(1)
                worker = threading.Thread(target=send)
                workers.append(worker)
                worker.start()
                self.assertTrue(incoming.recv(1))
                mssh._close_session_channel(incoming)
                stop.set()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                outgoing.close()
                self.assertTrue(client.is_active())
                self.assertTrue(server.is_active())
            probe = client.open_session(timeout=5)
            peer = server.accept(5)
            peer.sendall(b"still alive")
            self.assertEqual(probe.recv(100), b"still alive")
            mssh._close_session_channel(probe)
            peer.close()
            self.assertEqual(errors, [])
            self.assertIsNone(client.saved_exception)
        finally:
            server.close()
            client.close()
            for worker in workers:
                worker.join(5)


if __name__ == "__main__":
    unittest.main()
