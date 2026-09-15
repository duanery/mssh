#!/usr/bin/env python3.12
"""End-to-end tests for the session daemon, socket and client.

Each mssh invocation is a real, separate process (via fakechain.py, which
swaps only the SSH hops for a local pty), so these cover what the unit tests
cannot: that the daemon detaches, that state survives across processes, and
that the socket is as private as it claims to be.
"""

import importlib.util
import json
import os
import pty
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "fakechain.py")

spec = importlib.util.spec_from_loader(
    "mssh_mod", SourceFileLoader("mssh_mod",
                                 os.path.join(os.path.dirname(HERE), "mssh")))
mssh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mssh)

RUNDIR = tempfile.mkdtemp(prefix="mssh-test-")
# Sessions live under $HOME, deliberately and not under $XDG_RUNTIME_DIR, so
# isolating these tests from the real ones means moving HOME.
ENV = dict(os.environ, HOME=RUNDIR)
SOCKDIR = os.path.join(RUNDIR, ".mssh", "sessions")


def run(*args, **kw):
    return subprocess.run([sys.executable, FAKE] + list(args), env=ENV,
                          capture_output=True, timeout=kw.get("timeout", 60))


class SessionTest(unittest.TestCase):
    NAME = "t"

    def setUp(self):
        self.name = "%s%d" % (self.NAME, int(time.time() * 1000) % 100000)

    def tearDown(self):
        run("--session", self.name, "--stop")

    def start(self, *extra):
        proc = run("--session", self.name, "root@10.0.0.1", *extra)
        self.assertEqual(proc.returncode, 0,
                         "start failed: %s%s" % (proc.stdout, proc.stderr))
        return proc

    def send(self, line, *extra):
        return run("--session", self.name, line, *extra)


class TestLifecycle(SessionTest):
    def test_start_send_stop(self):
        self.start()
        proc = self.send("echo hello")
        self.assertEqual(proc.stdout, b"hello\n")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")

        proc = run("--session", self.name, "--stop")
        self.assertEqual(proc.returncode, 0)
        # The socket is gone and a later send says so.
        self.assertFalse(os.path.exists(
            os.path.join(SOCKDIR, self.name + ".sock")))
        proc = self.send("echo hello")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"no session", proc.stderr)

    def test_state_persists_across_processes(self):
        self.start()
        self.send("cd /tmp")
        self.assertEqual(self.send("pwd").stdout, b"/tmp\n")
        self.send("MSSH_VAR=persisted")
        self.assertEqual(self.send("echo $MSSH_VAR").stdout, b"persisted\n")
        # A background job started in one call is still there in another.
        self.send("sleep 20 & echo started")
        self.assertIn(b"sleep", self.send("jobs").stdout)

    def test_exit_status_is_the_commands(self):
        self.start()
        self.assertEqual(self.send("true").returncode, 0)
        self.assertEqual(self.send("false").returncode, 1)
        self.assertEqual(self.send("(exit 7)").returncode, 7)
        self.assertEqual(self.send("sh -c 'exit 42'").returncode, 42)
        proc = self.send("grep -c nothing /dev/null")
        self.assertEqual(proc.returncode, 1)

    def test_exit_ends_the_session_cleanly(self):
        self.start()
        proc = self.send("exit")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"session ended", proc.stderr)
        self.assertNotIn(b"Traceback", proc.stderr)
        proc = self.send("echo hi")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"no session", proc.stderr)
        self.assertNotIn(b"Traceback", proc.stderr)

    def test_status_counts_commands(self):
        self.start()
        for _ in range(3):
            self.send("true")
        out = run("--session", self.name, "--status").stdout.decode()
        self.assertIn("session:  %s" % self.name, out)
        self.assertIn("target:   root@10.0.0.1:22", out)
        self.assertIn("mode:     shell", out)
        # The setup probe counts too; what matters is that it moves.
        served = int([l for l in out.splitlines()
                      if l.startswith("commands:")][0].split()[-1])
        self.assertGreaterEqual(served, 3)

    def test_sessions_lists_it(self):
        self.start()
        out = run("--sessions").stdout.decode()
        self.assertIn(self.name, out)
        self.assertIn("root@10.0.0.1:22", out)
        run("--session", self.name, "--stop")
        proc = run("--sessions")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"no live sessions", proc.stderr)

    def test_wait_longer_than_default_is_honoured(self):
        self.start()
        proc = self.send("sleep 3; echo slow", "--wait", "20")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"slow\n")

    def test_second_start_is_refused(self):
        self.start()
        proc = run("--session", self.name, "root@10.0.0.1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"already running", proc.stderr)
        # and the first session still works
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_stale_socket_is_cleared(self):
        self.start()
        info = json.loads(subprocess.run(
            [sys.executable, "-c",
             "import socket,json,sys;"
             "s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1]);"
             "s.sendall(b'{\"op\":\"status\"}\\n');s.shutdown(socket.SHUT_WR);"
             "print(s.recv(65536).decode())",
             os.path.join(SOCKDIR, self.name + ".sock")],
            capture_output=True, env=ENV).stdout.decode())
        os.kill(info["pid"], signal.SIGKILL)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(info["pid"], 0)
            except OSError:
                break
            time.sleep(0.1)
        # The socket file is still there, but nothing is listening.
        self.assertTrue(os.path.exists(
            os.path.join(SOCKDIR, self.name + ".sock")))
        proc = self.send("echo hi")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"no session", proc.stderr)
        # Starting again clears the stale socket rather than failing to bind.
        self.start()
        self.assertEqual(self.send("echo again").stdout, b"again\n")

    def test_session_idle_expires(self):
        proc = run("--session", self.name, "root@10.0.0.1",
                   "--session-idle", "2")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.send("echo up").stdout, b"up\n")
        time.sleep(5)
        proc = self.send("echo gone")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"no session", proc.stderr)

    def test_remote_program_exit_ends_the_session(self):
        self.start()
        self.send("exit", "--wait", "5")
        deadline = time.time() + 8
        while time.time() < deadline and os.path.exists(
                os.path.join(SOCKDIR, self.name + ".sock")):
            time.sleep(0.2)
        proc = self.send("echo hi")
        self.assertEqual(proc.returncode, 1)


class TestTerminalIsKeptForPrompting(unittest.TestCase):
    """The daemon must still be able to ask for a password.

    The chain is built in the forked child before it detaches, so
    connect_hop's getpass reaches the real terminal.  Nothing here fakes the
    chain: these drive the unmodified mssh under a pty, since the whole point
    is what happens to the controlling terminal.
    """

    MSSH = os.path.join(os.path.dirname(HERE), "mssh")

    def _pty_run(self, args, feed=b""):
        """Run mssh on a real pty and return everything it wrote."""
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["HOME"] = RUNDIR
            os.execv(sys.executable, [sys.executable, self.MSSH] + args)
        if feed:
            time.sleep(1.0)
            os.write(fd, feed)
        out = b""
        deadline = time.time() + 45
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            out += data
        os.close(fd)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        return out

    def test_a_password_prompt_reaches_the_terminal(self):
        # 127.0.0.1:1 refuses instantly, so this reaches the prompt and stops
        # without ever touching a real host.
        out = self._pty_run(
            ["--session", "pw-probe", "--no-keys", "--no-agent",
             "root@127.0.0.1:1", "-o", "2"],
            feed=b"whatever\r")
        self.assertIn(b"password for root@127.0.0.1:1", out)
        # And the failure that follows is reported, once.
        self.assertIn(b"cannot reach", out)
        self.assertEqual(out.count(b"cannot reach"), 1, out)

    def test_a_failed_chain_is_reported_once(self):
        # $HOME is moved for isolation, so no key is found and mssh asks up
        # front; feed it something so the run reaches the connect failure.
        out = self._pty_run(["--session", "fail-probe", "root@127.0.0.1:1",
                             "-o", "2"], feed=b"x\r")
        self.assertIn(b"cannot reach", out)
        self.assertEqual(out.count(b"cannot reach"), 1, out)
        self.assertNotIn(b"exited before it was ready", out)
        self.assertNotIn(b"Traceback", out)

    def test_the_daemon_detaches_from_the_terminal(self):
        """A successful session must not hold the terminal open."""
        proc = subprocess.Popen(
            [sys.executable, FAKE, "--session", "detach-probe",
             "root@10.0.0.1"], env=ENV, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        try:
            info = session_status("detach-probe")
            # No controlling terminal, its own session, reparented to init.
            with open("/proc/%d/stat" % info["pid"]) as handle:
                fields = handle.read().rsplit(") ", 1)[1].split()
            tty_nr = int(fields[4])
            self.assertEqual(tty_nr, 0, "daemon still has a controlling tty")
            self.assertEqual(int(fields[3]), info["pid"],
                             "daemon is not its own session leader")
        finally:
            subprocess.run([sys.executable, FAKE, "--session",
                            "detach-probe", "--stop"], env=ENV,
                           capture_output=True)


def session_status(name):
    proc = subprocess.run(
        [sys.executable, "-c",
         "import socket,json,sys\n"
         "s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])\n"
         "s.sendall(b'{\"op\":\"status\"}\\n'); s.shutdown(socket.SHUT_WR)\n"
         "sys.stdout.write(s.recv(65536).decode())\n",
         os.path.join(SOCKDIR, name + ".sock")],
        capture_output=True, env=ENV, timeout=30)
    return json.loads(proc.stdout.decode())


class TestVerbose(SessionTest):
    """-v must show the chain being built, not just the final line."""

    def test_startup_trace_reaches_the_terminal(self):
        proc = run("--session", self.name, "root@10.0.0.1", "-v")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [l for l in proc.stderr.splitlines() if b"mssh:" in l]
        # The daemon used to redirect stderr before building the chain, so the
        # only line that ever reached the terminal was the parent's last one.
        self.assertGreater(len(lines), 1,
                           "only got: %r" % proc.stderr)
        self.assertTrue(any(b"ready" in l for l in lines), lines)
        self.assertTrue(any(b"listening on" in l for l in lines), lines)

    def test_send_reports_what_it_did(self):
        self.start()
        proc = self.send("echo hi", "-v")
        self.assertEqual(proc.stdout, b"hi\n")
        self.assertIn(b"sending", proc.stderr)
        self.assertIn(b"exit 0", proc.stderr)

    def test_log_is_written_only_under_v(self):
        self.start()
        self.assertFalse(os.path.exists(
            os.path.join(SOCKDIR, self.name + ".log")))
        run("--session", self.name, "--stop")
        run("--session", self.name, "root@10.0.0.1", "-v")
        path = os.path.join(SOCKDIR, self.name + ".log")
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(path):
            time.sleep(0.1)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as handle:
            self.assertIn(b"after startup", handle.read())


class TestStreaming(SessionTest):
    """Output must reach the client as the command produces it."""

    def _timed_lines(self, command, *extra):
        """Run a command, recording when each line of stdout arrived."""
        proc = subprocess.Popen(
            [sys.executable, FAKE, "--session", self.name, command] +
            list(extra), env=ENV, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        started = time.time()
        lines = []
        with proc.stdout:
            for raw in proc.stdout:
                lines.append((time.time() - started, raw))
        proc.stderr.close()
        proc.wait(timeout=60)
        return lines, proc.returncode

    def test_lines_arrive_before_the_command_ends(self):
        self.start()
        lines, code = self._timed_lines(
            "for i in 1 2 3 4; do echo tick$i; sleep 0.5; done", "--wait", "30")
        self.assertEqual(code, 0)
        self.assertEqual([l[1].strip() for l in lines],
                         [b"tick1", b"tick2", b"tick3", b"tick4"])
        # The command takes ~2s; the last line must not be the first thing seen.
        self.assertGreater(lines[-1][0] - lines[0][0], 0.8,
                           "all output arrived at once: %r" % lines)

    def test_endless_command_prints_until_the_timeout(self):
        """The reported bug: this showed nothing at all until --wait expired."""
        self.start()
        lines, code = self._timed_lines(
            "while true; do echo poll; sleep 0.3; done", "--wait", "2")
        self.assertEqual(code, 124)
        polls = [l for l in lines if b"poll" in l[1]]
        self.assertGreaterEqual(len(polls), 3, lines)
        self.assertLess(polls[0][0], 1.5,
                        "first line waited %.2fs" % polls[0][0])
        # And the session is usable straight afterwards, without an --interrupt.
        self.assertEqual(self.send("echo after").stdout, b"after\n")

    def test_wait_zero_runs_until_interrupted(self):
        self.start()
        proc = subprocess.Popen(
            [sys.executable, FAKE, "--session", self.name,
             "while true; do echo endless; sleep 0.3; done", "--wait", "0"],
            env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(2.5)
            self.assertIsNone(proc.poll(), "--wait 0 should not time out")
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=60)
        except BaseException:
            proc.kill()
            raise
        self.assertEqual(proc.returncode, 130)
        self.assertGreaterEqual(out.count(b"endless"), 5, out)
        self.assertIn(b"interrupting the remote command", err)
        self.assertNotIn(b"Traceback", err)
        self.assertEqual(self.send("echo recovered").stdout, b"recovered\n")


class TestClientCtrlC(SessionTest):
    """Ctrl-C at the client must interrupt the command, not crash."""

    def _interrupt_after(self, command, delay, signals=1):
        proc = subprocess.Popen(
            [sys.executable, FAKE, "--session", self.name, command,
             "--wait", "60"], env=ENV, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        try:
            time.sleep(delay)
            for _ in range(signals):
                proc.send_signal(signal.SIGINT)
                time.sleep(0.3)
            out, err = proc.communicate(timeout=60)
        except BaseException:
            proc.kill()
            raise
        return out, err, proc.returncode

    def test_no_traceback_and_the_session_survives(self):
        self.start()
        out, err, code = self._interrupt_after("sleep 30; echo never", 2.0)
        self.assertNotIn(b"Traceback", err)
        self.assertNotIn(b"KeyboardInterrupt", err)
        self.assertEqual(code, 130)
        self.assertNotIn(b"never", out)
        # The session is still there and the interrupted command is gone.
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")
        self.assertEqual(self.send("echo again").stdout, b"again\n")

    def test_second_ctrl_c_detaches(self):
        self.start()
        out, err, code = self._interrupt_after("sleep 30", 1.5, signals=2)
        self.assertNotIn(b"Traceback", err)
        self.assertEqual(code, 130)

    def test_interrupt_reports_whether_anything_ran(self):
        self.start()
        proc = run("--session", self.name, "--interrupt")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"nothing was running", proc.stderr)


class TestTimeoutAndInterrupt(SessionTest):
    def test_timeout_then_interrupt(self):
        self.start()
        proc = self.send("sleep 30", "--wait", "1")
        self.assertEqual(proc.returncode, 124)
        self.assertIn(b"still be running", proc.stderr)

        proc = run("--session", self.name, "--interrupt")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"interrupted the running command", proc.stderr)
        self.assertEqual(self.send("echo recovered").stdout, b"recovered\n")

    def test_late_output_is_not_misattributed(self):
        self.start()
        self.send("sleep 2; echo LATE", "--wait", "0.5")
        time.sleep(3)
        # The next command must return its own output, not the stale line.
        proc = self.send("echo CURRENT")
        self.assertEqual(proc.stdout, b"CURRENT\n")

    def test_stale_exit_status_is_not_reported_as_ours(self):
        self.start()
        self.send("sleep 2; (exit 33)", "--wait", "0.5")
        time.sleep(3)
        proc = self.send("true")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_repeated_timeouts_do_not_wedge_the_session(self):
        self.start()
        for _ in range(3):
            self.assertEqual(self.send("sleep 10", "--wait", "0.5").returncode,
                             124)
        proc = self.send("echo survived")
        self.assertEqual((proc.stdout, proc.returncode), (b"survived\n", 0))


class TestOutputFidelity(SessionTest):
    def test_binary_and_unicode_survive(self):
        self.start()
        self.assertEqual(self.send("printf 'a\\tb\\n'").stdout, b"a\tb\n")
        self.assertEqual(self.send("echo '中文 ok'").stdout,
                         "中文 ok\n".encode())

    def test_large_output(self):
        self.start()
        proc = self.send("seq 1 50000", "--wait", "60")
        lines = proc.stdout.split(b"\n")
        self.assertEqual(lines[0], b"1")
        self.assertEqual(lines[49999], b"50000")

    def test_stdin_is_devnull(self):
        self.start()
        proc = self.send("cat", "--wait", "5")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")


class TestInteractiveMode(SessionTest):
    def test_python_session(self):
        proc = run("--session", self.name, "root@10.0.0.1",
                   "-c", "python3 -i", "--prompt", ">>> $")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"Python", proc.stdout)      # the banner is printed
        proc = self.send("1+1")
        self.assertEqual(proc.stdout.strip().split(b"\n")[-1], b"2")
        self.assertEqual(proc.returncode, 0)       # no exit codes here
        proc = self.send("x = 5")
        proc = self.send("x * 3")
        self.assertIn(b"15", proc.stdout)
        out = run("--session", self.name, "--status").stdout.decode()
        self.assertIn("mode:     interactive", out)

    def test_idle_fallback_without_prompt(self):
        proc = run("--session", self.name, "root@10.0.0.1",
                   "-c", "cat", "--idle", "0.3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.send("echoed back")
        self.assertIn(b"echoed back", proc.stdout)


class TestSecurity(SessionTest):
    def test_socket_permissions(self):
        self.start()
        path = os.path.join(SOCKDIR, self.name + ".sock")
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(SOCKDIR).st_mode), 0o700)

    @unittest.skipUnless(os.getuid() == 0, "needs root to become another uid")
    def test_foreign_uid_is_refused(self):
        self.start()
        path = os.path.join(SOCKDIR, self.name + ".sock")
        # Open everything up: the point is that file permissions are not the
        # only guard, and the uid check refuses the peer regardless.
        os.chmod(path, 0o666)
        os.chmod(SOCKDIR, 0o777)
        os.chmod(os.path.dirname(SOCKDIR), 0o755)
        os.chmod(RUNDIR, 0o755)
        probe = (
            "import os,socket,sys\n"
            "os.setgid(65534); os.setuid(65534)\n"
            "s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])\n"
            "s.sendall(b'{\"op\":\"status\"}\\n'); s.shutdown(socket.SHUT_WR)\n"
            "sys.stdout.write(s.recv(65536).decode())\n"
        )
        proc = subprocess.run([sys.executable, "-c", probe, path], cwd="/",
                              capture_output=True, timeout=30)
        os.chmod(SOCKDIR, 0o700)
        os.chmod(path, 0o600)
        self.assertIn(b"permission denied", proc.stdout.lower(),
                      proc.stdout + proc.stderr)
        self.assertNotIn(b"root@10.0.0.1", proc.stdout)


class TestSpawnRace(SessionTest):
    def test_only_one_of_five_simultaneous_starts_wins(self):
        procs = [subprocess.Popen(
            [sys.executable, FAKE, "--session", self.name, "root@10.0.0.1"],
            env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(5)]
        results = [proc.communicate(timeout=60) + (proc.returncode,)
                   for proc in procs]
        winners = [r for r in results if r[2] == 0]
        self.assertEqual(len(winners), 1, results)
        for _out, err, code in results:
            if code != 0:
                self.assertIn(b"already running", err)
        # and the one that won is a working session
        self.assertEqual(self.send("echo survived").stdout, b"survived\n")


class TestConcurrency(SessionTest):
    def test_parallel_clients_do_not_interleave(self):
        self.start()
        procs = [
            subprocess.Popen([sys.executable, FAKE, "--session", self.name,
                              "sleep 0.%d; echo TAG%d" % (i % 5, i),
                              "--wait", "30"],
                             env=ENV, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
            for i in range(8)
        ]
        for i, proc in enumerate(procs):
            out, err = proc.communicate(timeout=60)
            self.assertEqual(out, b"TAG%d\n" % i,
                             "client %d got %r (%r)" % (i, out, err))


class TestRegression(unittest.TestCase):
    """The non-session paths must be untouched."""

    def test_help(self):
        proc = subprocess.run([sys.executable, FAKE, "--help"],
                              capture_output=True, env=ENV)
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"persistent session", proc.stdout)
        self.assertIn(b"file copy", proc.stdout)

    def test_no_arguments_is_a_usage_error(self):
        proc = subprocess.run([sys.executable, FAKE], capture_output=True,
                              env=ENV)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"no target given", proc.stderr)

    def test_non_session_paths_reach_the_chain(self):
        # The fake chain only implements what a session needs, so the plain
        # -c and copy paths are checked for reaching run_command/run_copy
        # rather than for their output; the real ones are covered elsewhere.
        for argv, want in [(["root@10.0.0.1", "-c", "echo plain"],
                            b"recv_ready"),
                           (["./setup.py", "root@10.0.0.1:/tmp/"],
                            b"open_sftp")]:
            proc = subprocess.run([sys.executable, FAKE] + argv,
                                  capture_output=True, env=ENV, timeout=30)
            self.assertIn(want, proc.stderr, "%r: %r" % (argv, proc.stderr))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        subprocess.run(["rm", "-rf", RUNDIR])
