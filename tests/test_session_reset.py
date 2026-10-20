"""Explicit primary-shell replacement without remote-signal assumptions."""

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_session_daemon import ENV, FAKE, SessionTest, mssh, run
from test_session_forward import Echo, TCPServer, free_port
from test_session_socks import read_exact
from test_session_unix_forward import OpenSSHForwardTest


class TestResetShell(unittest.TestCase):
    def session(self):
        transport = mock.Mock()
        transport.is_active.return_value = True
        session = mssh._Session(mock.Mock(closed=False), "old", "shell",
                                None, 0.1, "target", None, transport)
        session.name = "reset"
        return session

    def test_replace_only_shell_and_reuse_rcfile(self):
        session = self.session()
        previous = session.chan
        job = session._new_job(previous, "shell", "vim", default=True)
        session.default_job = job
        independent = session._new_job(mock.Mock(closed=False), "exec", "sleep 60")
        session._register_job(independent)
        session.rcfile_path = "/tmp/.mssh-rc-existing"
        session.rcfile_paths.add(session.rcfile_path)
        candidate = mock.Mock(closed=False)
        with mock.patch.object(mssh, "_open_session_channel",
                               return_value=(candidate, "new")), \
                mock.patch.object(mssh._Session, "setup") as setup, \
                mock.patch.object(mssh, "_source_rcfile") as source, \
                mock.patch.object(mssh, "_upload_rcfile") as upload:
            session.reset_shell()
        setup.assert_called_once()
        source.assert_called_once_with(
            session.transport, candidate, None, 10.0,
            remote_path="/tmp/.mssh-rc-existing")
        upload.assert_not_called()
        previous.close.assert_called_once()
        candidate.close.assert_not_called()
        independent.chan.close.assert_not_called()
        self.assertFalse(job.aborted)
        self.assertIsNone(session.default_job)
        self.assertEqual(session.execs, {independent})
        self.assertEqual(session.mark, b"new")
        self.assertEqual(session.rcfile_paths, {"/tmp/.mssh-rc-existing"})
        self.assertFalse(session.resetting.is_set())
        self.assertFalse(session.reset_lock.locked())
        self.assertIn("variables", session.shell_notice)

    def test_failed_initialization_preserves_old_job_and_shell(self):
        for stage in ("setup", "rcfile"):
            with self.subTest(stage=stage):
                session = self.session()
                previous = session.chan
                session.default_job = session._new_job(
                    previous, "shell", "vim", default=True)
                session.rcfile_path = "/tmp/.mssh-rc-existing"
                candidate = mock.Mock(closed=False)
                with mock.patch.object(mssh, "_open_session_channel",
                                       return_value=(candidate, "new")), \
                        mock.patch.object(mssh._Session, "setup") as setup, \
                        mock.patch.object(mssh, "_source_rcfile") as source:
                    (setup if stage == "setup" else source).side_effect = \
                        RuntimeError("initialization failed")
                    with self.assertRaisesRegex(RuntimeError, "initialization failed"):
                        session.reset_shell()
                self.assertIs(session.chan, previous)
                self.assertIs(session.default_job.chan, previous)
                previous.close.assert_not_called()
                candidate.close.assert_called_once()
                self.assertFalse(session.resetting.is_set())
                self.assertFalse(session.reset_lock.locked())

    def test_reject_interactive_and_concurrent_resets(self):
        session = self.session()
        session.mode = "interactive"
        with self.assertRaisesRegex(ValueError, "-c sessions"):
            session.reset_shell()
        session.mode = "shell"
        session.reset_lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, "already in progress"):
                session.reset_shell()
        finally:
            session.reset_lock.release()
        session.transport.open_session.assert_not_called()

    def test_stop_during_preparation_closes_only_candidate(self):
        session = self.session()
        previous = session.chan
        candidate = mock.Mock(closed=False)
        with mock.patch.object(mssh, "_open_session_channel",
                               return_value=(candidate, "new")), \
                mock.patch.object(mssh._Session, "setup",
                                  side_effect=lambda wait: session.stopping.set()):
            with self.assertRaises(mssh._SessionDead):
                session.reset_shell()
        self.assertIs(session.chan, previous)
        previous.close.assert_not_called()
        candidate.close.assert_called_once()
        self.assertFalse(session.resetting.is_set())
        self.assertFalse(session.lock.locked())

    def test_token_wait_and_queued_send_release_on_reset(self):
        session = self.session()
        session.resetting.set()
        with self.assertRaises(mssh._SessionReset):
            session._await_token(b"token", 5)
        with self.assertRaises(mssh._SessionReset):
            session.run("echo must-not-run", 5, mssh._BufSink())
        session.chan.recv.assert_not_called()
        session.chan.send.assert_not_called()
        self.assertFalse(session.pending)

    def test_open_refusal_and_release_timeout_preserve_old_shell(self):
        for refused in (True, False):
            with self.subTest(refused=refused):
                session = self.session()
                previous = session.chan
                job = session._new_job(previous, "shell", "vim", default=True)
                session.default_job = job
                candidate = mock.Mock(closed=False)
                if not refused:
                    session.lock = mock.Mock()
                    session.lock.acquire.return_value = False
                with mock.patch.object(mssh, "_open_session_channel",
                                       return_value=(candidate, "new")) as opened, \
                        mock.patch.object(mssh._Session, "setup"):
                    if refused:
                        opened.side_effect = RuntimeError("channel refused")
                    with self.assertRaises((RuntimeError, ValueError)):
                        session.reset_shell()
                self.assertIs(session.chan, previous)
                self.assertIs(session.default_job, job)
                previous.close.assert_not_called()
                if not refused:
                    candidate.close.assert_called_once()
                    session.lock.acquire.assert_called_once_with(timeout=5.0)
                    session.lock.release.assert_not_called()
                self.assertFalse(session.reset_lock.locked())
                self.assertFalse(session.resetting.is_set())

    def test_transport_replaced_during_preparation_does_not_install_candidate(self):
        session = self.session()
        previous = session.chan
        candidate = mock.Mock(closed=False)

        def reconnect(wait):
            session.transport = mock.Mock()

        with mock.patch.object(mssh, "_open_session_channel",
                               return_value=(candidate, "new")), \
                mock.patch.object(mssh._Session, "setup", side_effect=reconnect):
            with self.assertRaisesRegex(mssh._SessionDead, "session changed"):
                session.reset_shell()
        self.assertIs(session.chan, previous)
        previous.close.assert_not_called()
        candidate.close.assert_called_once()
        self.assertFalse(session.resetting.is_set())

    def test_idle_poll_ignores_replaced_channel(self):
        session = self.session()
        previous = session.chan

        def closed():
            session.chan = mock.Mock(closed=False)
            return True

        previous.exit_status_ready.side_effect = closed
        with mock.patch.object(session, "failed") as failed:
            self.assertFalse(session.check_link())
        failed.assert_not_called()

    def test_old_reader_eof_during_reset_is_not_a_fatal_session_failure(self):
        session = self.session()
        session.resetting.set()
        for broken_transport in (False, True):
            session.transport.is_active.return_value = not broken_transport
            for error in (None, OSError("old channel closed")):
                with self.subTest(broken_transport=broken_transport, error=error):
                    session.chan.recv.return_value = b""
                    session.chan.recv.side_effect = error
                    with self.assertRaises(
                            mssh._SessionDead if broken_transport else
                            mssh._SessionReset):
                        session._recv()

    def test_old_daemon_reply_is_not_accepted_as_a_reset(self):
        with mock.patch.object(mssh, "session_request", return_value={"ok": True}):
            with self.assertRaisesRegex(SystemExit, "does not support --reset-shell"):
                mssh.run_session_reset_shell("old")


@unittest.skipUnless(shutil.which("sshd") and os.getuid() == 0,
                     "local OpenSSH inetd tests require sshd and root")
class TestOpenSSHReset(OpenSSHForwardTest):
    def test_real_channel_reset_preserves_transport_and_independent_channel(self):
        chan, mark = mssh._open_session_channel(self.transport, None, 5)
        self.session.chan, self.session.mark = chan, mark.encode("ascii")
        self.session.setup(5)
        independent = self.transport.open_session(timeout=5)
        self.addCleanup(mssh._close_session_channel, independent)
        independent.exec_command("sleep 1; echo independent")
        self.assertEqual(self.session.run(
            "stty -isig; trap '' INT; sleep 60", 0.2, mssh._BufSink()),
            (None, True))
        self.session.reset_shell()
        self.assertTrue(chan.closed)
        self.assertTrue(self.transport.is_active())
        self.assertFalse(self.session.pending)
        sink = mssh._BufSink()
        self.assertEqual(self.session.run("echo recovered", 3, sink), (0, False))
        self.assertEqual(sink.value(), b"recovered\n")
        independent.settimeout(5)
        self.assertEqual(independent.recv(65536), b"independent\n")
        self.assertEqual(independent.recv_exit_status(), 0)


class TestResetDaemon(SessionTest):
    def wait_job(self, command):
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                jobs = mssh.session_request(self.name, {"op": "jobs"})["jobs"]
                match = next((row for row in jobs if command in row["command"]), None)
                if match is not None:
                    return match
                time.sleep(0.05)
        self.fail("job did not start: %s" % command)

    def spawn(self, *args):
        process = subprocess.Popen(
            [sys.executable, FAKE, self.name] + list(args), env=ENV,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)

        def close():
            if process.poll() is None:
                process.kill()
            if not process.stdout.closed or not process.stderr.closed:
                process.communicate(timeout=5)

        self.addCleanup(close)
        return process

    def test_reset_running_signal_ignoring_program_keeps_exec_and_forward(self):
        server = TCPServer(("127.0.0.1", 0), Echo)
        worker = threading.Thread(target=server.serve_forever)
        worker.daemon = True
        worker.start()
        self.addCleanup(worker.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = free_port()
        self.start("-L", "127.0.0.1:%d:127.0.0.1:%d" %
                   (port, server.server_address[1]))
        self.assertEqual(self.send("MSSH_RESET_TEST=lost; cd /tmp").returncode, 0)
        independent = self.spawn("--exec", "sleep 2; echo independent")
        self.wait_job("sleep 2")
        primary = self.spawn("--wait", "0",
                             "stty -isig; trap '' INT; sleep 60; echo must-not-run")
        self.wait_job("must-not-run")
        with socket.create_connection(("127.0.0.1", port), timeout=3) as forwarded:
            forwarded.sendall(b"before")
            self.assertEqual(read_exact(forwarded, 6), b"before")
            started = time.monotonic()
            reset = run(self.name, "--reset-shell", timeout=15)
            self.assertEqual(reset.returncode, 0, reset.stderr)
            self.assertLess(time.monotonic() - started, 10)
            self.assertIn(b"not preserved", reset.stderr)
            output, error = primary.communicate(timeout=5)
            self.assertEqual(primary.returncode, 1, error)
            self.assertIn(b"command was abandoned", error)
            self.assertNotIn(b"must-not-run", output)
            forwarded.sendall(b"after")
            self.assertEqual(read_exact(forwarded, 5), b"after")
        output, error = independent.communicate(timeout=5)
        self.assertEqual(independent.returncode, 0, error)
        self.assertEqual(output, b"independent\n")
        result = self.send('printf "%s:%s\\n" "${MSSH_RESET_TEST-unset}" "$PAGER"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"unset:cat\n")
        self.assertIn(b"default shell was reset", result.stderr)
        following = self.send("echo following")
        self.assertEqual(following.stdout, b"following\n")
        self.assertNotIn(b"default shell was reset", following.stderr)

    def test_reset_interrupts_token_wait_without_ending_session(self):
        self.start()
        command = "stty -isig; trap '' INT; sleep 60"
        self.assertEqual(self.send(command, "--wait", "0.2").returncode, 124)
        interruption = self.spawn("--interrupt")
        time.sleep(0.5)
        reset = run(self.name, "--reset-shell", timeout=15)
        self.assertEqual(reset.returncode, 0, reset.stderr)
        _, error = interruption.communicate(timeout=5)
        self.assertEqual(interruption.returncode, 1, error)
        self.assertIn(b"reset requested", error)
        self.assertEqual(self.send("echo recovered").stdout, b"recovered\n")
        self.assertEqual(run(self.name, "--status").returncode, 0)

    @unittest.skipUnless(shutil.which("vim"), "vim is not installed")
    def test_reset_real_vim_pending_on_main_channel(self):
        with tempfile.TemporaryDirectory(prefix="mssh-reset-vim-") as directory:
            pidfile = os.path.join(directory, "pid")
            self.start()
            command = "sh -c %s" % mssh.shlex.quote(
                "echo $$ > %s; exec vim -Nu NONE -n /dev/null" %
                mssh.shlex.quote(pidfile))
            timed = self.send(command, "--wait", "0.5")
            self.assertEqual(timed.returncode, 124, timed.stderr)
            with open(pidfile) as source:
                pid = int(source.read())
            try:
                reset = run(self.name, "--reset-shell", timeout=15)
                self.assertEqual(reset.returncode, 0, reset.stderr)
                self.assertEqual(self.send("echo after-vim").stdout, b"after-vim\n")
                with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
                    self.assertEqual(
                        mssh.session_request(self.name, {"op": "jobs"})["jobs"], [])
            finally:
                # Channel closure need not kill every remote child. The fake
                # transport is local, so clean up this test's recorded PID.
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_reset_reuses_rcfile_and_initializes_again(self):
        with tempfile.TemporaryDirectory(prefix="mssh-reset-rc-") as directory:
            rc = os.path.join(directory, "rc")
            counter = os.path.join(directory, "counter")
            with open(rc, "w") as source:
                source.write("echo initialized >> %s\n"
                             "export MSSH_RESET_RC=kept\ncd %s\n" %
                             (mssh.shlex.quote(counter), mssh.shlex.quote(directory)))
            self.start("--rcfile", rc)
            with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
                path = mssh.session_request(self.name, {"op": "status"})["rcfile"]
            for _ in range(2):
                self.assertEqual(run(self.name, "--reset-shell").returncode, 0)
                result = self.send('printf "%s:%s\\n" "$MSSH_RESET_RC" "$PWD"')
                self.assertEqual(result.stdout,
                                 ("kept:%s\n" % directory).encode())
                with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
                    self.assertEqual(
                        mssh.session_request(self.name, {"op": "status"})["rcfile"],
                        path)
            with open(counter) as source:
                self.assertEqual(source.read(), "initialized\n" * 3)
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(run(self.name, "--stop").returncode, 0)
            self.assertFalse(os.path.exists(path))

    def test_invalid_retained_rcfile_does_not_destroy_existing_shell(self):
        with tempfile.TemporaryDirectory(prefix="mssh-reset-invalid-") as directory:
            rc = os.path.join(directory, "rc")
            with open(rc, "w") as source:
                source.write("export MSSH_OLD_SHELL=alive\n")
            self.start("--rcfile", rc)
            with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
                path = mssh.session_request(self.name, {"op": "status"})["rcfile"]
            with open(path, "w") as source:
                source.write("return 7\n")
            result = run(self.name, "--reset-shell")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(b"cannot reset default shell", result.stderr)
            self.assertEqual(self.send('echo "$MSSH_OLD_SHELL"').stdout, b"alive\n")
            self.assertEqual(run(self.name, "--status").returncode, 0)

    def test_reset_rejects_interactive_session_and_flag_combinations(self):
        self.start("-c", "cat")
        result = run(self.name, "--reset-shell")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"-c sessions", result.stderr)
        self.assertEqual(self.send("still-running").stdout, b"still-running\n")
        for args in (("--status",), ("--exec",), ("--rcfile", "/nonexistent"),
                     ("--job", "1"), ("--sessions",), ("-L", "1080:h:80"),
                     ("-t",), ("--reconnect", "2"), ("echo", "no")):
            with self.subTest(args=args):
                result = run(self.name, "--reset-shell", *args)
                self.assertEqual(result.returncode, 2, result.stderr)
        missing = run("--session", "missing-reset", "--reset-shell")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn(b"no session", missing.stderr)


if __name__ == "__main__":
    unittest.main()
