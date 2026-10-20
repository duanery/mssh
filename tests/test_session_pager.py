"""Noninteractive pager defaults and recovery of an unresponsive primary job."""

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
from test_session_unit import make_session, drain
from test_session_forward import Echo, TCPServer, free_port
from test_session_socks import read_exact
import test_session_status as status_tests


class TestPagerDefaults(unittest.TestCase):
    def test_setup_overrides_inherited_pagers_after_profiles(self):
        session = make_session(
            "env PAGER=less MANPAGER=less GIT_PAGER=less SYSTEMD_PAGER=less "
            "/bin/bash --noprofile --norc")
        try:
            output, status, timed_out = drain(
                session, 'printf "%s:%s:%s:%s\\n" "$PAGER" "$MANPAGER" '
                         '"$GIT_PAGER" "$SYSTEMD_PAGER"', 3)
            self.assertEqual(output, b"cat:cat:cat:cat\n")
            self.assertEqual(status, 0)
            self.assertFalse(timed_out)
        finally:
            session.chan.close()

    def test_readonly_pager_does_not_break_setup_with_errexit(self):
        command = (
            "readonly PAGER=less; set -e; " + mssh.SESSION_SETUP +
            'printf "%s:%s:%s:%s\\n" "$PAGER" "$MANPAGER" "$GIT_PAGER" '
            '"$SYSTEMD_PAGER"')
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"less:cat:cat:cat\n")
        self.assertEqual(result.stderr, b"")

    @unittest.skipUnless(shutil.which("man"), "man is not installed")
    def test_real_man_on_dumb_terminal_completes_and_preserves_shell_state(self):
        session = make_session(
            "env TERM=dumb MANPAGER=less PAGER=less /bin/bash --noprofile --norc")
        try:
            drain(session, "MSSH_PAGER_STATE=kept; cd /tmp")
            output, status, timed_out = drain(session, "man man", 5)
            self.assertFalse(timed_out)
            self.assertEqual(status, 0, output[-200:])
            self.assertGreater(len(output), 1000)
            self.assertNotIn(b"terminal is not fully functional", output)
            self.assertNotIn(b"press RETURN", output)
            output, status, timed_out = drain(
                session, 'printf "%s:%s\\n" "$MSSH_PAGER_STATE" "$PWD"', 3)
            self.assertEqual((output, status, timed_out), (b"kept:/tmp\n", 0, False))
            self.assertFalse(session.pending)
        finally:
            session.chan.close()


class TestBusyRecovery(unittest.TestCase):
    def session(self):
        session = mssh._Session(mock.Mock(closed=False), "marker", "shell",
                                None, 0.1, "target", None, transport=mock.Mock())
        job = session._new_job(session.chan, "shell", "less", default=True)
        session.default_job = job
        return session, job

    def test_pending_interrupt_failure_retains_job_and_is_not_acknowledged(self):
        session, job = self.session()
        with mock.patch.object(session, "_write"), \
                mock.patch.object(session, "_await_token", return_value=False) as await_token, \
                mock.patch.object(mssh.time, "sleep"):
            with self.assertRaisesRegex(mssh._SessionBusy, "job remains pending"):
                session.interrupt_job(job.id)
        self.assertEqual(await_token.call_count, 2)
        self.assertTrue(all(call[0][1] == mssh.SESSION_RESYNC_WAIT
                            for call in await_token.call_args_list))
        self.assertLess(mssh.SESSION_RESYNC_WAIT * 2 + 0.3, 15)
        self.assertIs(session.default_job, job)
        self.assertFalse(job.aborted)
        self.assertFalse(session.lock.locked())
        self.assertEqual(session.jobs_snapshot()[0]["state"], "pending")
        session.chan.close.assert_not_called()

    def test_new_request_is_not_written_when_pending_recovery_fails(self):
        session, job = self.session()
        with mock.patch.object(session, "_write") as write, \
                mock.patch.object(session, "_await_token", return_value=False), \
                mock.patch.object(session, "_pump") as pump, \
                mock.patch.object(mssh.time, "sleep"):
            with self.assertRaises(mssh._SessionBusy):
                session.run("echo do-not-execute", 3, mssh._BufSink())
        pump.assert_not_called()
        self.assertTrue(all("do-not-execute" not in call[0][0]
                            for call in write.call_args_list))
        self.assertIs(session.default_job, job)
        self.assertEqual(session.job_sequence, 1)
        self.assertEqual(session.served, 0)
        self.assertFalse(session.stopping.is_set())

    def test_streaming_interrupt_failure_preserves_same_pending_job(self):
        session, job = self.session()

        def pump(framer, sink, deadline, **kwargs):
            kwargs["job"].abort()
            return framer.on_abort(sink)

        session.default_job = None
        with mock.patch.object(session, "_write"), \
                mock.patch.object(session, "_await_token", return_value=False), \
                mock.patch.object(session, "_pump", side_effect=pump), \
                mock.patch.object(mssh.time, "sleep"):
            with self.assertRaises(mssh._SessionBusy):
                session.run("less", 3, mssh._BufSink())
        self.assertIsNotNone(session.default_job)
        self.assertFalse(session.default_job.aborted)
        self.assertEqual(session.default_job.command, "less")
        self.assertFalse(session.lock.locked())

    def test_real_channel_failure_during_recovery_remains_fatal(self):
        session, job = self.session()
        with mock.patch.object(session, "_write"), \
                mock.patch.object(session, "_recv",
                                  side_effect=mssh._SessionDead("channel closed")), \
                mock.patch.object(mssh.time, "sleep"):
            with self.assertRaisesRegex(mssh._SessionDead, "channel closed"):
                session._resync()

    def test_stop_does_not_wait_for_recovery_deadline(self):
        session, job = self.session()
        session.stopping.set()
        with mock.patch.object(session, "_recv") as recv:
            with self.assertRaisesRegex(mssh._SessionDead, "session is stopping"):
                session._await_token(b"token", 5)
        recv.assert_not_called()

    def test_busy_interrupt_reply_does_not_end_or_reconnect_daemon(self):
        session = status_tests.TestStatusViews().session()
        session.dead = None
        session.resolve.return_value = None
        session.interrupt_job.side_effect = mssh._SessionBusy("job remains pending")
        reply = status_tests.TestStatusViews().dispatch(
            session, {"op": "interrupt"})
        self.assertIn("job remains pending", reply["error"])
        self.assertNotIn("interrupted", reply)
        session.failed.assert_not_called()
        session.start_reconnect.assert_not_called()
        self.assertIsNone(session.dead)


class TestPagerDaemon(SessionTest):
    @unittest.skipUnless(shutil.which("man"), "man is not installed")
    def test_man_then_ls_through_separate_clients(self):
        self.start()
        manual = run(self.name, "--wait", "5", "man man")
        self.assertEqual(manual.returncode, 0, manual.stderr + manual.stdout[-1000:])
        self.assertGreater(len(manual.stdout), 1000)
        self.assertNotIn(b"press RETURN", manual.stdout)
        following = run(self.name, "--wait", "3", "ls /dev/null")
        self.assertEqual(following.returncode, 0, following.stderr)
        self.assertEqual(following.stdout, b"/dev/null\n")

    def test_rcfile_can_override_pager_defaults(self):
        with tempfile.TemporaryDirectory(prefix="mssh-pager-rc-") as directory:
            script = os.path.join(directory, "rc")
            with open(script, "w") as output:
                output.write("export PAGER='cat -n' MANPAGER='cat -n'\n")
            self.start("--rcfile", script)
        result = self.send('printf "%s:%s\\n" "$PAGER" "$MANPAGER"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"cat -n:cat -n\n")

    @unittest.skipUnless(shutil.which("less"), "less is not installed")
    def test_busy_recovery_preserves_session_and_allows_independent_exec(self):
        with tempfile.TemporaryDirectory(prefix="mssh-pager-busy-") as directory:
            source = os.path.join(directory, "input")
            pidfile = os.path.join(directory, "pid")
            with open(source, "w") as output:
                output.write("pager line\n" * 1000)
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
            # A foreground pager is needed: background less would stop on tty
            # reads. Record its PID then exec less. Secure mode prevents old
            # recovery markers from invoking less's shell-command feature.
            command = "cd %s; LESSSECURE=1 sh -c %s" % (
                mssh.shlex.quote(directory),
                mssh.shlex.quote("echo $$ > %s; exec less %s" %
                                (mssh.shlex.quote(pidfile), mssh.shlex.quote(source))))
            timed = self.send(command, "--wait", "0.3")
            self.assertEqual(timed.returncode, 124, timed.stderr)
            self.assertIn(b"press RETURN", timed.stdout)
            with open(pidfile) as source:
                pid = int(source.read())
            try:
                # Bound the real recovery failure using a subprocess timeout,
                # without changing the installed daemon or any real session.
                start = time.monotonic()
                interrupted = run(self.name, "--interrupt", timeout=15)
                self.assertEqual(interrupted.returncode, 1, interrupted.stderr)
                self.assertIn(b"job remains pending", interrupted.stderr)
                self.assertNotIn(b"interrupted the running command", interrupted.stderr)
                self.assertLess(time.monotonic() - start, 15)
                status = run(self.name, "--status")
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertNotIn(b"ended", status.stdout)
                jobs = run(self.name, "--jobs")
                self.assertEqual(jobs.returncode, 0, jobs.stderr)
                self.assertIn(b"pending", jobs.stdout)
                rejected = self.send("echo do-not-run", "--wait", "1", timeout=15)
                self.assertEqual(rejected.returncode, 1, rejected.stderr)
                self.assertIn(b"job remains pending", rejected.stderr)
                self.assertNotIn(b"do-not-run", rejected.stdout)
                independent = run(self.name, "--exec", "echo independent")
                self.assertEqual(independent.returncode, 0, independent.stderr)
                self.assertEqual(independent.stdout, b"independent\n")
                with socket.create_connection(
                        ("127.0.0.1", port), timeout=3) as forwarded:
                    forwarded.sendall(b"forward survives")
                    self.assertEqual(read_exact(forwarded, 16), b"forward survives")
            finally:
                killed = run(self.name, "--exec", "kill -TERM %d" % pid)
                self.assertEqual(killed.returncode, 0, killed.stderr)
            recovered = self.send("echo recovered", "--wait", "3")
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(recovered.stdout, b"recovered\n")

    @unittest.skipUnless(shutil.which("less"), "less is not installed")
    def test_ctrl_c_busy_reply_keeps_daemon_alive(self):
        with tempfile.TemporaryDirectory(prefix="mssh-pager-ctrl-c-") as directory:
            source = os.path.join(directory, "input")
            with open(source, "w") as output:
                output.write("pager line\n" * 1000)
            self.start()
            command = "cd %s; LESSSECURE=1 less %s" % (
                mssh.shlex.quote(directory), mssh.shlex.quote(source))
            process = subprocess.Popen(
                [sys.executable, FAKE, self.name, "--wait", "0", command],
                env=ENV, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if mssh.session_request(self.name, {"op": "jobs"})["jobs"]:
                            break
                        time.sleep(0.05)
                time.sleep(0.2)
                process.send_signal(signal.SIGINT)
                out, err = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 1, err)
                self.assertIn(b"job remains pending", err)
                self.assertNotIn(b"detached", err)
                self.assertEqual(run(self.name, "--status").returncode, 0)
                self.assertEqual(run(self.name, "--exec", "echo independent").stdout,
                                 b"independent\n")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
