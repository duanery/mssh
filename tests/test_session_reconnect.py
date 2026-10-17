"""Default-job cleanup on broken and rebuilt session transports."""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_session_daemon import ENV, FAKE, SessionTest, mssh, run


class TestDefaultJobCleanup(unittest.TestCase):
    def make_session(self):
        chan = mock.Mock(closed=False)
        transport = mock.Mock()
        transport.is_active.return_value = True
        return mssh._Session(chan, "marker", "shell", None, 0.1,
                             "target", None, transport=transport, timeout=5)

    def default_job(self, session):
        job = session._new_job(session.chan, session.mode, "sleep 20",
                               default=True)
        session.default_job = job
        return job

    def test_disconnect_clears_default_before_starting_reconnect(self):
        for mode, retries in (("shell", 0), ("shell", 2), ("interactive", 2)):
            with self.subTest(mode=mode, retries=retries):
                session = self.make_session()
                session.mode, session.retries = mode, retries
                job = self.default_job(session)
                independent = session._new_job(mock.Mock(closed=False),
                                               "exec", "sleep 20")
                session._register_job(independent)
                session.transport.is_active.return_value = False

                def reconnect():
                    self.assertIsNone(session.default_job)
                    self.assertFalse(session.pending)

                with mock.patch.object(session, "start_reconnect",
                                       side_effect=reconnect) as start:
                    self.assertEqual(session.failed("connection dropped"),
                                     retries == 0)
                    self.assertEqual(start.call_count, int(retries > 0))
                self.assertIsNone(session.default_job)
                self.assertFalse(session.pending)
                self.assertEqual(session.execs, {independent})
                # Removing the dead request must not send Ctrl-C/resync.
                self.assertFalse(job.aborted)
                session.chan.send.assert_not_called()

    def test_independent_channel_failure_keeps_live_default_job(self):
        session = self.make_session()
        job = self.default_job(session)
        self.assertTrue(session.failed("exec channel refused"))
        self.assertIs(session.default_job, job)
        self.assertTrue(session.pending)

    def test_disconnect_cleanup_does_not_remove_replacement_channel_job(self):
        session = self.make_session()
        session.retries = 2
        replacement = mock.Mock(closed=False)
        successor = session._new_job(replacement, "shell", "echo successor",
                                     default=True)

        def link_down():
            # A reconnect replaced the shell while the old failure was
            # checking its transport. Its job is not owned by that failure.
            session.chan = replacement
            session.default_job = successor
            return True

        with mock.patch.object(session, "link_down", side_effect=link_down), \
                mock.patch.object(session, "start_reconnect"):
            self.assertFalse(session.failed("old connection dropped"))
        self.assertIs(session.default_job, successor)

    def test_run_clears_only_fatal_default_job_errors(self):
        for stage in ("write", "pump"):
            with self.subTest(stage=stage):
                session = self.make_session()
                with mock.patch.object(session, "_write") as write, \
                        mock.patch.object(session, "_pump") as pump:
                    (write if stage == "write" else pump).side_effect = \
                        mssh._SessionDead("connection dropped")
                    with self.assertRaises(mssh._SessionDead):
                        session.run("sleep 20", 5, mssh._BufSink())
                self.assertIsNone(session.default_job)
                self.assertFalse(session.pending)

    def test_timeout_and_abandoned_client_keep_live_job_pending(self):
        for abandoned in (False, True):
            with self.subTest(abandoned=abandoned):
                session = self.make_session()
                with mock.patch.object(session, "_write"), \
                        mock.patch.object(session, "_pump") as pump:
                    if abandoned:
                        pump.side_effect = mssh._ClientGone("client hung up")
                        with self.assertRaises(mssh._ClientGone):
                            session.run("sleep 20", 5, mssh._BufSink())
                    else:
                        pump.return_value = (None, True)
                        self.assertEqual(
                            session.run("sleep 20", 5, mssh._BufSink()),
                            (None, True))
                self.assertTrue(session.pending)
                self.assertEqual(session.default_job.command, "sleep 20")

    def test_queued_request_does_not_recreate_job_on_broken_transport(self):
        session = self.make_session()
        session.retries = 2
        self.default_job(session)
        errors = []
        entered = threading.Event()

        def queued():
            entered.set()
            try:
                session.run("echo queued", 5, mssh._BufSink())
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(session, "_write") as write, \
                mock.patch.object(session, "start_reconnect"):
            session.lock.acquire()
            worker = threading.Thread(target=queued)
            try:
                worker.start()
                self.assertTrue(entered.wait(3))
                session.transport.is_active.return_value = False
                self.assertFalse(session.failed("connection dropped"))
            finally:
                session.lock.release()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], mssh._SessionDead)
            write.assert_not_called()
        self.assertIsNone(session.default_job)
        self.assertFalse(session.pending)

    def test_setup_is_not_a_user_job(self):
        session = self.make_session()

        def pump(framer, sink, deadline, **kwargs):
            self.assertIsNone(session.default_job)
            self.assertEqual(session.jobs_snapshot(), [])
            self.assertIsNone(kwargs.get("job"))
            sink.write(b"ready\n")
            return 0, False

        with mock.patch.object(session, "_write"), \
                mock.patch.object(session, "_pump", side_effect=pump):
            self.assertEqual(session.setup(5), b"")
        self.assertIsNone(session.default_job)
        self.assertEqual(session.job_sequence, 0)

    def test_failed_setup_leaves_no_default_job(self):
        for failure in ("write", "pump", "timeout", "invalid"):
            with self.subTest(failure=failure):
                session = self.make_session()
                with mock.patch.object(session, "_write") as write, \
                        mock.patch.object(session, "_pump") as pump:
                    if failure == "write":
                        write.side_effect = [None, mssh._SessionDead("write failed")]
                    elif failure == "pump":
                        pump.side_effect = mssh._SessionDead("read failed")
                    else:
                        pump.return_value = (None, True) if failure == "timeout" \
                            else (0, False)
                    with self.assertRaises((mssh._SessionDead, SystemExit)):
                        session.setup(5)
                self.assertIsNone(session.default_job)
                self.assertFalse(session.pending)
                self.assertEqual(session.jobs_snapshot(), [])

    def test_failed_rebuild_restores_channel_without_setup_job(self):
        session = self.make_session()
        old_chan = session.chan
        self.default_job(session)
        replacement = mock.Mock(closed=False)
        clients = [mock.Mock()]
        with mock.patch.object(mssh, "_open_shell", return_value=(
                clients, mock.Mock(), replacement, "new-marker")), \
                mock.patch.object(mssh, "close_all") as close, \
                mock.patch.object(session, "_write"), \
                mock.patch.object(session, "_pump",
                                  side_effect=mssh._SessionDead("setup failed")):
            self.assertFalse(session.rebuild())
            close.assert_called_once_with(clients)
        self.assertIs(session.chan, old_chan)
        self.assertIsNone(session.default_job)
        self.assertFalse(session.pending)
        self.assertEqual(session.jobs_snapshot(), [])
        self.assertIn("setup failed", session.last_error)

    def test_successful_rebuild_starts_without_default_job(self):
        session = self.make_session()
        self.default_job(session)
        session.broken = "connection dropped"
        replacement = mock.Mock(closed=False)

        def pump(framer, sink, deadline, **kwargs):
            self.assertEqual(session.jobs_snapshot(), [])
            sink.write(b"ready\n")
            return 0, False

        with mock.patch.object(mssh, "_open_shell", return_value=(
                [], mock.Mock(), replacement, "new-marker")), \
                mock.patch.object(session, "_write"), \
                mock.patch.object(session, "_pump", side_effect=pump):
            self.assertTrue(session.rebuild())
        self.assertIs(session.chan, replacement)
        self.assertIsNone(session.default_job)
        self.assertFalse(session.pending)
        self.assertEqual(session.jobs_snapshot(), [])
        self.assertEqual(session.link_state()[0], "up")


class TestReconnectJobs(SessionTest):
    def snapshot(self):
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            return mssh.session_request(self.name, {"op": "jobs"}, timeout=3)["jobs"]

    def status(self):
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            return mssh.session_request(self.name, {"op": "status"}, timeout=3)

    def wait_link(self, up):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = self.status()
            if (info["link"] == "up") == up:
                return info
            # Allow the daemon's between-request poll to detect a break.
            time.sleep(1.1 if up is False else 0.05)
        self.fail("unexpected link state: %r" % info)

    def test_running_request_removed_during_reconnect_and_after(self):
        with tempfile.TemporaryDirectory(prefix="mssh-job-reconnect-") as tmp:
            down = os.path.join(tmp, "down")
            with mock.patch.dict(ENV, MSSH_FAKE_DOWN=down):
                self.start("--reconnect", "10")
                # The fake transport does not signal channel FDs on a link
                # break. Output makes the real pump reach its failing recv.
                process = subprocess.Popen(
                    [sys.executable, FAKE, self.name,
                     "while :; do echo tick; sleep 0.1; done"],
                    env=ENV, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        rows = self.snapshot()
                        if rows:
                            break
                        time.sleep(0.05)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["kind"], "shell")
                    with open(down, "w"):
                        pass
                    process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 1)
                    self.assertEqual(self.wait_link(False)["link"], "reconnecting")
                    self.assertEqual(self.snapshot(), [])
                    self.assertEqual(self.status()["jobs"], 0)
                    # Keep the destination down through a failed attempt.
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        info = self.status()
                        self.assertEqual(self.snapshot(), [])
                        if info["last_error"]:
                            break
                        time.sleep(0.05)
                    self.assertTrue(info["last_error"])
                    os.unlink(down)
                    self.wait_link(True)
                    self.assertEqual(self.snapshot(), [])
                    self.assertEqual(self.status()["jobs"], 0)
                    self.assertEqual(self.send("echo alive").stdout, b"alive\n")
                    self.assertEqual(self.snapshot(), [])
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_pending_request_removed_when_idle_poll_detects_disconnect(self):
        with tempfile.TemporaryDirectory(prefix="mssh-pending-reconnect-") as tmp:
            down = os.path.join(tmp, "down")
            with mock.patch.dict(ENV, MSSH_FAKE_DOWN=down):
                self.start("--reconnect", "10")
                proc = self.send("sleep 20", "--wait", "0.1")
                self.assertEqual(proc.returncode, 124, proc.stderr)
                rows = self.snapshot()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["state"], "pending")
                with open(down, "w"):
                    pass
                self.wait_link(False)
                self.assertEqual(self.snapshot(), [])
                os.unlink(down)
                self.wait_link(True)
                self.assertEqual(self.snapshot(), [])
                self.assertEqual(self.send("echo alive").stdout, b"alive\n")
                self.assertEqual(self.snapshot(), [])
