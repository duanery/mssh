#!/usr/bin/env python3.12
"""Unit tests for mssh's persistent session: names, framing, flag rejection.

No network and no ssh: the framing layer is driven against a local pty
running the same programs a session would hold on the target.
"""

import importlib.util
import io
import os
import pty
import re
import select
import socket
import subprocess
import sys
import time
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
MSSH = os.path.join(os.path.dirname(HERE), "mssh")

spec = importlib.util.spec_from_loader("mssh", SourceFileLoader("mssh", MSSH))
mssh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mssh)


class FakeChan(object):
    """A paramiko-channel-shaped wrapper around a local pty master fd."""

    def __init__(self, program):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvp("/bin/sh", ["/bin/sh", "-c",
                                  "stty -echo 2>/dev/null; exec " + program])
        self._timeout = None
        self.eof = False

    def settimeout(self, value):
        self._timeout = value

    def sendall(self, data):
        os.write(self.fd, data)

    def recv(self, size):
        ready, _, _ = select.select([self.fd], [], [], self._timeout)
        if not ready:
            raise socket.timeout()
        try:
            data = os.read(self.fd, size)
        except OSError:
            data = b""
        if not data:
            self.eof = True
        return data

    def exit_status_ready(self):
        return self.eof

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, 9)
            os.waitpid(self.pid, 0)
        except OSError:
            pass


def make_session(program="/bin/bash", mode="shell", prompt=None, idle=0.4):
    chan = FakeChan(program)
    mark = "__MSSH_%s__" % os.urandom(8).hex()
    prompt_re = re.compile(prompt.encode()) if prompt else None
    sess = mssh._Session(chan, mark, mode, prompt_re, idle,
                         "test@localhost:22", program if mode != "shell" else None)
    sess.setup(15.0)
    return sess


def drain(sess, line, wait=10.0):
    """Run one command and collect its whole stream: (output, status, timeout)."""
    chunks, status, timed_out = [], None, False
    for event in sess.stream(line, wait):
        if event[0] == "out":
            chunks.append(event[1])
        else:
            status, timed_out = event[1], event[2]
    return b"".join(chunks), status, timed_out


class TestNames(unittest.TestCase):
    def test_valid_and_invalid(self):
        good = ["a", "dbg", "a.b_c-1", "x" * 64]
        # 'a\n' matters: with '$' instead of '\Z' it would pass and then name
        # a file with a newline in it.  '-x' and '.x' stay out so a name can
        # never be read as an option or hide in a listing.
        bad = ["", "../evil", "a/b", "x" * 65, "a b", "a\n", "..", "-x", ".x"]
        for name in good:
            self.assertIsNotNone(mssh.SESSION_NAME_RE.match(name), name)
        for name in bad:
            self.assertIsNone(mssh.SESSION_NAME_RE.match(name), repr(name))

    def test_session_path_rejects_traversal(self):
        for name in ["../evil", "", "a/b", "x" * 65]:
            with self.assertRaises(SystemExit):
                mssh.session_path(name)

    def test_session_path_inside_dir(self):
        path = mssh.session_path("unit-ok")
        self.assertEqual(os.path.dirname(path), mssh.session_dir())
        self.assertTrue(path.endswith("/unit-ok.sock"))

    def test_session_dir_is_private(self):
        st = os.lstat(mssh.session_dir())
        self.assertEqual(st.st_mode & 0o777, 0o700)
        self.assertEqual(st.st_uid, os.getuid())


class TestPromptFraming(unittest.TestCase):
    """--prompt must anchor at the END of a line, not match anywhere in it."""

    def _sess(self, prompt):
        sess = mssh._Session.__new__(mssh._Session)
        sess.mode = "interactive"
        sess.prompt_re = re.compile(prompt.encode())
        return sess

    def test_matches_at_end(self):
        sess = self._sess(r"\(gdb\) $")
        self.assertEqual(sess._prompt_at_end(b"(gdb) "), b"")
        self.assertEqual(sess._prompt_at_end(b"#0  main () (gdb) "),
                         b"#0  main () ")

    def test_mid_line_match_does_not_frame(self):
        # A line that quotes the prompt but continues must not frame.
        sess = self._sess(r"\(gdb\) $")
        self.assertIsNone(sess._prompt_at_end(b"type (gdb) to continue"))

    def test_no_match_at_all(self):
        sess = self._sess(r">>> $")
        self.assertIsNone(sess._prompt_at_end(b"just output"))

    def test_frames_on_the_last_occurrence(self):
        sess = self._sess(r">>> $")
        self.assertEqual(sess._prompt_at_end(b">>> junk >>> "), b">>> junk ")


class TestShellFraming(unittest.TestCase):
    def _sess(self):
        sess = mssh._Session.__new__(mssh._Session)
        sess.mark = b"__MSSH_dead__"
        sess.mode = "shell"
        return sess

    def test_marker_line_yields_status(self):
        sess = self._sess()
        self.assertEqual(sess._marker_status(b"__MSSH_dead__7"), 7)
        self.assertEqual(sess._marker_status(b"__MSSH_dead__130"), 130)

    def test_ordinary_output_is_not_a_marker(self):
        sess = self._sess()
        for line in [b"hello", b"", b"__MSSH_de", b"x__MSSH_dead__0",
                     b"the marker is __MSSH_dead__"]:
            self.assertIsNone(sess._marker_status(line), line)

    def test_marker_without_digits_is_flagged_not_ignored(self):
        # A marker whose status did not survive must not be mistaken for
        # output; -1 says "framed, but the status is unknown".
        sess = self._sess()
        self.assertEqual(sess._marker_status(b"__MSSH_dead__"), -1)

    def test_carriage_returns_are_stripped(self):
        sess = self._sess()
        self.assertEqual(sess._clean(b"line\r"), b"line")
        self.assertEqual(sess._clean(b"line"), b"line")

    def test_escapes_survive_in_shell_mode(self):
        # Only interactive mode strips escapes: a shell command that prints
        # colour is printing real output, and mangling it would be wrong.
        sess = self._sess()
        self.assertEqual(sess._clean(b"\x1b[31mred\x1b[0m"),
                         b"\x1b[31mred\x1b[0m")


AWKWARD = [
    # (command, expected status, substring that must appear in the output)
    ("echo plain", 0, "plain"),
    ("false", 1, ""),
    ("sleep 1 &", 0, ""),
    ("echo x | cat", 0, "x"),
    ("true # comment", 0, ""),
    ("if true; then echo yes; fi", 0, "yes"),
    ("for i in 1 2 3; do echo $i; done", 0, "3"),
    ("cat <<EOF\nheredoc body\nEOF", 0, "heredoc body"),
    ("echo 'unterminated", None, "syntax error"),   # nonzero, value is bash's
    ("cat", 0, ""),                      # stdin is /dev/null, must not hang
    ("echo a; echo b", 0, "b"),
    ("printf 'no newline'", 0, "no newline"),
    ("(exit 3)", 3, ""),
    ("echo \"quo'te\"", 0, "quo'te"),
]


class TestShellSessionLive(unittest.TestCase):
    """Drive the real framing against a real bash behind a real pty."""

    @classmethod
    def setUpClass(cls):
        cls.sess = make_session("/bin/bash")

    @classmethod
    def tearDownClass(cls):
        cls.sess.chan.close()

    def test_awkward_inputs(self):
        for cmd, want_status, want_text in AWKWARD:
            out, status, timed_out = drain(self.sess, cmd, 10.0)
            self.assertFalse(timed_out, "timed out on %r" % cmd)
            if want_status is None:
                self.assertNotEqual(status, 0, "%r framed with status 0" % cmd)
            else:
                self.assertEqual(status, want_status,
                                 "%r -> status %r, output %r"
                                 % (cmd, status, out))
            if want_text:
                self.assertIn(want_text, out.decode(), "%r -> %r" % (cmd, out))

    def test_state_persists(self):
        drain(self.sess, "cd /tmp", 10.0)
        out, status, _ = drain(self.sess, "pwd", 10.0)
        self.assertEqual((out.strip(), status), (b"/tmp", 0))
        drain(self.sess, "MSSH_X=hello", 10.0)
        out, _, _ = drain(self.sess, "echo $MSSH_X", 10.0)
        self.assertEqual(out.strip(), b"hello")
        drain(self.sess, "cd /", 10.0)

    def test_no_output_drift_over_many_sends(self):
        for i in range(200):
            out, status, _ = drain(self.sess, "echo %d" % i, 10.0)
            self.assertEqual(out.strip(), str(i).encode())
            self.assertEqual(status, 0)

    def test_long_command_line(self):
        payload = "z" * 20000
        out, status, _ = drain(self.sess, "echo %s | wc -c" % payload, 15.0)
        self.assertEqual(status, 0)
        self.assertEqual(out.strip(), str(len(payload) + 1).encode())

    def test_large_output(self):
        out, status, _ = drain(self.sess, "seq 1 20000", 20.0)
        self.assertEqual(status, 0)
        lines = out.strip().split(b"\n")
        self.assertEqual(len(lines), 20000)
        self.assertEqual(lines[-1], b"20000")

    def test_stderr_is_merged(self):
        out, status, _ = drain(self.sess, "echo err >&2", 10.0)
        self.assertEqual(out.strip(), b"err")
        self.assertEqual(status, 0)

    def test_output_streams_while_the_command_runs(self):
        """The point of streaming: lines arrive before the command finishes."""
        arrivals = []
        started = time.time()
        for event in self.sess.stream(
                "for i in 1 2 3; do echo tick$i; sleep 0.4; done", 20.0):
            if event[0] == "out":
                arrivals.append((time.time() - started, event[1]))
            else:
                self.assertEqual(event[1], 0)
        self.assertEqual([a[1].strip() for a in arrivals],
                         [b"tick1", b"tick2", b"tick3"])
        # Each tick must arrive near its own moment, not all at the end.  A
        # line is held until the next one comes (the framing printf's newline
        # is indistinguishable until then), so tick1 lands with tick2 at ~0.4s
        # -- still streaming, since the command runs for 1.2s.
        self.assertLess(arrivals[0][0], 0.8,
                        "first line arrived at %.2fs" % arrivals[0][0])
        self.assertGreater(arrivals[2][0] - arrivals[0][0], 0.5)

    def test_endless_command_streams_then_times_out(self):
        """The reported bug: a command that never ends showed nothing."""
        arrivals = []
        for event in self.sess.stream(
                "while true; do echo forever; sleep 0.2; done", 1.5):
            if event[0] == "out":
                arrivals.append(event[1])
            else:
                self.assertTrue(event[2], "should have timed out")
        self.assertGreaterEqual(len(arrivals), 3, arrivals)
        # Most lines are the command's; bash may slip in a job-completion
        # notice from an earlier background command, which is genuine output.
        self.assertGreaterEqual(sum(1 for a in arrivals if b"forever" in a), 3,
                                arrivals)
        # The endless command is still running with nobody watching; the next
        # command must stop it rather than wait for a marker it will never send.
        out, status, timed_out = drain(self.sess, "echo after", 10.0)
        self.assertFalse(timed_out)
        self.assertEqual((out.strip(), status), (b"after", 0))

    def test_partial_line_is_released_when_it_goes_quiet(self):
        # A prompt-like write with no newline must not be held hostage.
        chunks = []
        for event in self.sess.stream("printf 'Continue? '; sleep 2", 4.0):
            if event[0] == "out":
                chunks.append((time.time(), event[1]))
        self.assertTrue(any(b"Continue?" in c[1] for c in chunks), chunks)

    def test_timeout_then_interrupt_recovers(self):
        out, status, timed_out = drain(self.sess, "sleep 30", 1.0)
        self.assertTrue(timed_out)
        self.assertTrue(self.sess.pending)
        # Nothing is streaming, but a command is still running out there.
        self.assertTrue(self.sess.interrupt_running())
        self.assertFalse(self.sess.pending)
        out, status, _ = drain(self.sess, "echo back", 10.0)
        self.assertEqual((out.strip(), status), (b"back", 0))

    def test_interrupt_with_nothing_running(self):
        drain(self.sess, "true", 10.0)
        self.assertFalse(self.sess.interrupt_running())
        out, status, _ = drain(self.sess, "echo fine", 10.0)
        self.assertEqual((out.strip(), status), (b"fine", 0))

    def test_interrupt_while_streaming_aborts_the_whole_list(self):
        import threading

        seen = []
        done = threading.Event()

        def run():
            for event in self.sess.stream("sleep 30; echo never", 30.0):
                seen.append(event)
            done.set()

        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(1.0)
        self.assertTrue(self.sess.interrupt_running(),
                        "a running command should report as signalled")
        self.assertTrue(done.wait(30), "the stream never ended")
        worker.join()

        output = b"".join(e[1] for e in seen if e[0] == "out")
        self.assertNotIn(b"never", output)
        self.assertEqual(seen[-1][0], "end")
        self.assertEqual(seen[-1][1], 130)
        out, status, _ = drain(self.sess, "echo ok", 10.0)
        self.assertEqual((out.strip(), status), (b"ok", 0))

    def test_stale_marker_is_not_read_as_a_status(self):
        """A timed-out command's late marker must not frame the next one."""
        out, status, timed_out = drain(self.sess, "sleep 1.5; (exit 33)", 0.5)
        self.assertTrue(timed_out)
        time.sleep(2.5)                   # the stale marker is now in flight
        out, status, _ = drain(self.sess, "echo fresh", 10.0)
        self.assertEqual(out.strip(), b"fresh")
        self.assertEqual(status, 0, "picked up the stale command's status")

    def test_repeated_timeouts_do_not_wedge_the_session(self):
        for _ in range(3):
            _out, _status, timed_out = drain(self.sess, "sleep 5", 0.4)
            self.assertTrue(timed_out)
        out, status, timed_out = drain(self.sess, "echo survived", 10.0)
        self.assertFalse(timed_out)
        self.assertEqual((out.strip(), status), (b"survived", 0))

    def test_stale_marker_is_not_read_as_a_status(self):
        """A timed-out command's late marker must not frame the next one."""
        out, status, timed_out = drain(self.sess, "sleep 1.5; (exit 33)", 0.5)
        self.assertTrue(timed_out)
        time.sleep(2.5)                   # the stale marker is now in flight
        out, status, _ = drain(self.sess, "echo fresh", 10.0)
        self.assertEqual(out.strip(), b"fresh")
        self.assertEqual(status, 0, "picked up the stale command's status")

    def test_concurrent_sends_are_serialized(self):
        import threading
        results = {}

        def run(key, cmd, want):
            out, status, _ = drain(self.sess, cmd, 20.0)
            results[key] = (out.strip(), status, want)

        threads = [
            threading.Thread(target=run, args=("a", "sleep 0.3; echo AAA", b"AAA")),
            threading.Thread(target=run, args=("b", "echo BBB", b"BBB")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len(results), 2)
        for key, (out, status, want) in results.items():
            self.assertEqual(status, 0, key)
            self.assertEqual(out, want, "%s got %r" % (key, out))


class TestInteractiveSessionLive(unittest.TestCase):
    def test_python_prompt(self):
        sess = make_session("python3 -i", mode="interactive", prompt=r">>> $")
        try:
            out, status, timed_out = drain(sess, "1+1", 10.0)
            self.assertFalse(timed_out)
            self.assertIsNone(status)
            self.assertIn(b"2", out)
            out, _, _ = drain(sess, "print('x' * 5)", 10.0)
            self.assertIn(b"xxxxx", out)
        finally:
            sess.chan.close()

    @unittest.skipUnless(
        subprocess.call(["sh", "-c", "command -v gdb >/dev/null"]) == 0,
        "gdb not installed")
    def test_gdb_prompt(self):
        sess = make_session("gdb -q -ex 'set pagination off' /bin/sleep",
                            mode="interactive", prompt=r"\(gdb\) $")
        try:
            out, status, timed_out = drain(sess, "print 6*7", 20.0)
            self.assertFalse(timed_out)
            self.assertIn(b"42", out)
            out, _, _ = drain(sess, "nosuchcommand", 20.0)
            self.assertIn(b"Undefined command", out)
        finally:
            sess.chan.close()


class TestFlagRejection(unittest.TestCase):
    """Every impossible combination must fail before anything connects."""

    def run_main(self, argv):
        err = io.StringIO()
        old = sys.stderr
        sys.stderr = err
        try:
            code = mssh.main(argv)
        except SystemExit as exc:
            # SystemExit("message") is an exit code of 1 plus that message on
            # stderr, which is how the interpreter itself treats it.
            if isinstance(exc.code, str):
                err.write(exc.code + "\n")
                code = 1
            else:
                code = exc.code
        finally:
            sys.stderr = old
        return code, err.getvalue()

    def test_rejections(self):
        cases = [
            ["--session", "u1", "--copy-id", "root@h"],
            ["--session", "u1", "-t", "root@h"],
            ["--session", "u1", "-r", "root@h"],
            ["--session", "u1", "-p", "root@h"],
            ["--session", "u1", "--prompt", ">>> $", "root@h"],
            ["--session", "u1", "--prompt", "*bad(", "-c", "x", "root@h"],
            ["--session", "u1", "--stop", "--status"],
            ["--session", "u1", "--stop", "extra"],
            ["--session", "../evil", "--status"],
            ["--stop"],
            ["--interrupt"],
            ["--status", "root@h"],
            ["--prompt", ">>> $", "root@h"],
            ["--sessions", "root@h"],
            ["--sessions", "--session", "u1"],
            [],
        ]
        for argv in cases:
            code, err = self.run_main(argv)
            self.assertEqual(code, 2, "%r exited %r: %s" % (argv, code, err))

    def test_no_session_names_the_first_call_form(self):
        code, err = self.run_main(["--session", "u-absent", "echo hi"])
        self.assertEqual(code, 1)
        self.assertIn("no session", err)
        self.assertIn("mssh --session u-absent", err)

    def test_regressions_still_parse(self):
        # A plain login target and a copy must still reach the connect stage,
        # i.e. fail with something other than a usage error.
        for argv in [["root@10.255.255.1", "-c", "true", "-o", "0.3"],
                     ["./x", "root@10.255.255.1:/tmp/", "-o", "0.3"]]:
            code, err = self.run_main(argv)
            self.assertNotEqual(code, 2, "%r: %s" % (argv, err))


if __name__ == "__main__":
    unittest.main(verbosity=2)
