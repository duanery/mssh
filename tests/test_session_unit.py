#!/usr/bin/env python3.12
"""Unit tests for mssh's persistent session: names, framing, flag rejection.

No network and no ssh: the framing layer is driven against a local pty
running the same programs a session would hold on the target.
"""

import importlib.util
import base64
import getpass
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
    """A paramiko-channel-shaped wrapper around a local pty master fd.

    The daemon selects on the channel rather than polling it with a blocking
    recv, so fileno() is part of the shape a fake has to have.  Returning the
    pty master is close enough to paramiko's event pipe for these tests: both
    are readable exactly when there is something to read.
    """

    def __init__(self, program):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvp("/bin/sh", ["/bin/sh", "-c",
                                  "stty -echo 2>/dev/null; exec " + program])
        self._timeout = None
        self.eof = False

    def settimeout(self, value):
        self._timeout = value

    def fileno(self):
        return self.fd

    def recv_ready(self):
        return bool(select.select([self.fd], [], [], 0)[0])

    def sendall(self, data):
        os.write(self.fd, data)

    def send(self, data):
        os.write(self.fd, data)
        return len(data)

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


class TimedSink(mssh._BufSink):
    """A sink that also remembers when each piece of output reached it.

    Streaming is a timing property -- a line has to be handed over while the
    command is still running, not collected at its end -- and the sink is now
    where that is observable, so the tests that used to time each yielded
    event time each write instead.
    """

    def __init__(self):
        mssh._BufSink.__init__(self)
        self.arrivals = []

    def write(self, data):
        if data:
            self.arrivals.append((time.time(), data))
        mssh._BufSink.write(self, data)


def make_session(program="/bin/bash", mode="shell", prompt=None, idle=0.4):
    chan = FakeChan(program)
    mark = "__MSSH_%s__" % os.urandom(8).hex()
    prompt_re = re.compile(prompt.encode()) if prompt else None
    sess = mssh._Session(chan, mark, mode, prompt_re, idle,
                         "test@localhost:22", program if mode != "shell" else None)
    sess.setup(15.0)
    return sess


def drain(sess, line, wait=10.0, sink=None):
    """Run one command and collect all of it: (output, status, timed_out)."""
    sink = mssh._BufSink() if sink is None else sink
    status, timed_out = sess.run(line, wait, sink)
    return sink.value(), status, timed_out


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


class TestEndpointParsing(unittest.TestCase):
    """The user part is optional, so a bare word is a whole endpoint."""

    def test_the_user_may_be_omitted(self):
        me = getpass.getuser()
        for spec, host, port in [("h", "h", 22),
                                 ("10.0.0.9", "10.0.0.9", 22),
                                 ("h:2222", "h", 2222),
                                 ("host.example.com.", "host.example.com.", 22),
                                 ("my_host", "my_host", 22),
                                 ("[::1]", "::1", 22),
                                 ("[::1]:22", "::1", 22),
                                 ("[fe80::1%eth0]:22", "fe80::1%eth0", 22)]:
            ep = mssh.parse_endpoint(spec)
            self.assertEqual((ep.user, ep.host, ep.port), (me, host, port),
                             spec)
            self.assertIsNone(ep.password, spec)

    def test_an_explicit_user_still_wins(self):
        for spec, want in [("user@h", ("user", "h", 22, None)),
                           ("user:pw@h", ("user", "h", 22, "pw")),
                           ("user:p:a@ss@h:22", ("user", "h", 22, "p:a@ss")),
                           # 'user:@h' asks to be prompted, not for an empty
                           # password.
                           ("user:@h", ("user", "h", 22, None))]:
            ep = mssh.parse_endpoint(spec)
            self.assertEqual((ep.user, ep.host, ep.port, ep.password), want,
                             spec)

    def test_the_default_follows_the_environment(self):
        # getpass.getuser() reads $LOGNAME first, so a sudo-style environment
        # picks the invoking user rather than the uid's passwd entry.
        old = os.environ.get("LOGNAME")
        os.environ["LOGNAME"] = "someoneelse"
        try:
            self.assertEqual(mssh.parse_endpoint("h").user, "someoneelse")
        finally:
            if old is None:
                del os.environ["LOGNAME"]
            else:
                os.environ["LOGNAME"] = old

    def test_a_command_is_not_a_host(self):
        # This is what keeps a mistyped session send from being dialled: with
        # the user optional, nothing else tells these from a hostname.
        bad = ["", "@h", "a b", "echo hi", "cd /var/log && ls", "./x", "-foo",
               "a;b", "h:22:pw", "h:notaport", "h:", "[::1", "x@[::1]junk",
               "h:99999", "wc -l"]
        for spec in bad:
            with self.assertRaises(ValueError, msg=repr(spec)):
                mssh.parse_endpoint(spec)

    def test_an_empty_user_is_a_typo_not_a_default(self):
        with self.assertRaises(ValueError) as caught:
            mssh.parse_endpoint("@h")
        self.assertIn("drop the '@'", str(caught.exception))


class TestCopyArgSplitting(unittest.TestCase):
    """Which copy arguments are remote, now that the user may be absent."""

    def test_local_paths_stay_local(self):
        # Remote means the part before the first ':' could be a host name.
        # No host name holds a '/', and ':leading' names none at all.
        for arg in ["./x", "/tmp/a:b", "./mail@archive/x", "x", ":leading",
                    "./notes:2024.txt", "root@h", "10.0.0.9", "./x:y",
                    "[::1]junk:/tmp", ":pw@h:/tmp"]:
            self.assertEqual(mssh.split_copy_arg(arg), (None, arg), arg)

    def test_a_bare_port_is_a_port_not_a_path(self):
        # 'h:2222' is host and port, as it is everywhere else in mssh, so as
        # a copy argument it names no file at all -- which is a usage error
        # rather than a silent guess between the three possible readings.
        for arg in ["h:22", "root@h:2222", "10.0.0.9:8900"]:
            with self.assertRaises(ValueError, msg=arg) as caught:
                mssh.split_copy_arg(arg)
            self.assertIn("no path to copy", str(caught.exception), arg)

    def test_an_empty_port_field_is_refused(self):
        # 'h::8900' is either a port left out or a path starting with ':',
        # and the argument says nothing about which, so neither is assumed.
        for arg in ["10.0.0.9::8900", "10.0.0.9::", "h::/tmp/x",
                    "[::1]::8900"]:
            with self.assertRaises(ValueError, msg=arg) as caught:
                mssh.split_copy_arg(arg)
            self.assertIn("empty", str(caught.exception), arg)

    def test_the_port_reading_can_be_escaped_either_way(self):
        # Both escapes named by the error message have to work.
        ep, path = mssh.split_copy_arg("10.0.0.9:8900:8900")
        self.assertEqual((ep.host, ep.port, path), ("10.0.0.9", 8900, "8900"))
        self.assertEqual(mssh.split_copy_arg("./10.0.0.9:8900"),
                         (None, "./10.0.0.9:8900"))
        # And the one out of the empty-port message: './' keeps the ':'.
        ep, path = mssh.split_copy_arg("h:./:8900")
        self.assertEqual((ep.host, path), ("h", "./:8900"))

    def test_remote_paths_without_a_user(self):
        me = getpass.getuser()
        for arg, host, port, path in [
                ("10.0.0.9:/tmp/x", "10.0.0.9", 22, "/tmp/x"),
                ("10.0.0.9:", "10.0.0.9", 22, "."),
                ("10.0.0.9:36001:/tmp/", "10.0.0.9", 36001, "/tmp/"),
                ("[::1]:/tmp", "::1", 22, "/tmp"),
                # scp reads this as a host too, which is why a local file of
                # that name has to be written './notes:2024.txt'.
                ("notes:2024.txt", "notes", 22, "2024.txt")]:
            ep, got = mssh.split_copy_arg(arg)
            self.assertIsNotNone(ep, arg)
            self.assertEqual((ep.user, ep.host, ep.port, got),
                             (me, host, port, path), arg)

    def test_an_explicit_user_still_parses(self):
        for arg, want in [("root@10.0.0.9:/var/log/syslog",
                           ("root", "10.0.0.9", 22, "/var/log/syslog")),
                          ("root@10.0.0.9:logs/today.log",
                           ("root", "10.0.0.9", 22, "logs/today.log")),
                          ("user@[::1]:22:/tmp", ("user", "::1", 22, "/tmp"))]:
            ep, path = mssh.split_copy_arg(arg)
            self.assertIsNotNone(ep, arg)
            self.assertEqual((ep.user, ep.host, ep.port, path), want, arg)

    def test_plan_copy_accepts_a_userless_endpoint(self):
        direction, srcs, dest, ep = mssh.plan_copy(["./x", "10.0.0.9:/tmp/"])
        self.assertEqual((direction, srcs, dest), ("up", ["./x"], "/tmp/"))
        self.assertEqual((ep.user, ep.host), (getpass.getuser(), "10.0.0.9"))
        direction, srcs, dest, _ = mssh.plan_copy(["10.0.0.9:/var/log/x", "./"])
        self.assertEqual((direction, srcs, dest), ("down", ["/var/log/x"], "./"))

    def test_all_local_is_still_rejected(self):
        for args in [["./a", "./b"], ["./a", "./notes:2024.txt"]]:
            with self.assertRaises(ValueError) as caught:
                mssh.plan_copy(args)
            self.assertIn("no remote path", str(caught.exception))


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


class TestStdinKind(unittest.TestCase):
    """What stdin *is* decides whether it is forwarded, not what the command is."""

    def test_pipe_and_regular_file_and_devnull(self):
        readfd, writefd = os.pipe()
        try:
            self.assertEqual(mssh.stdin_kind(readfd), "pipe")
        finally:
            os.close(readfd)
            os.close(writefd)

        with open(MSSH, "rb") as handle:
            self.assertEqual(mssh.stdin_kind(handle.fileno()), "file")

        fd = os.open(os.devnull, os.O_RDONLY)
        try:
            # /dev/null must not read as a pipe: it is indistinguishable from
            # having no input, so forwarding it would set up a channel and a
            # fifo to deliver nothing.
            self.assertEqual(mssh.stdin_kind(fd), "null")
        finally:
            os.close(fd)

    def test_a_tty_is_recognised(self):
        primary, secondary = pty.openpty()
        try:
            self.assertEqual(mssh.stdin_kind(secondary), "tty")
        finally:
            os.close(primary)
            os.close(secondary)

    def test_closed_stdin_is_none(self):
        readfd, writefd = os.pipe()
        os.close(readfd)
        os.close(writefd)
        self.assertEqual(mssh.stdin_kind(readfd), "none")

    def test_want_stdin_policy(self):
        class Opts(object):
            def __init__(self, **kw):
                self.no_stdin = kw.get("no_stdin", False)
                self.stdin = kw.get("stdin", False)

        original = mssh.stdin_kind
        try:
            for kind, plain, forced in [("pipe", True, True),
                                        ("file", True, True),
                                        ("tty", False, True),
                                        ("null", False, False),
                                        ("none", False, False)]:
                mssh.stdin_kind = lambda _fd=None, k=kind: k
                self.assertEqual(mssh.want_stdin(Opts()), plain,
                                 "%s, by default" % kind)
                self.assertEqual(mssh.want_stdin(Opts(stdin=True)), forced,
                                 "%s, with --stdin" % kind)
                # -n always wins: it is the escape hatch for a stdin that
                # belongs to something else, such as a script read from a pipe.
                self.assertFalse(mssh.want_stdin(Opts(no_stdin=True)), kind)
        finally:
            mssh.stdin_kind = original


class TestFrameBuilder(unittest.TestCase):
    def test_without_stdin_the_command_reads_devnull(self):
        frame = mssh.session_frame("echo hi")
        self.assertIn("/dev/null", frame)
        self.assertIn(base64.b64encode(b"echo hi").decode(), frame)

    def test_with_stdin_the_command_reads_the_fifo(self):
        frame = mssh.session_frame("cat", "/tmp/.mssh-in-abc")
        self.assertIn('< "/tmp/.mssh-in-abc"', frame)
        self.assertNotIn("/dev/null", frame)
        # The command still travels base64-encoded, so nothing in it can be
        # parsed as part of the framing.
        self.assertIn(base64.b64encode(b"cat").decode(), frame)

    def test_the_writer_program_creates_then_unlinks_the_fifo(self):
        prog = mssh.SESSION_WRITER % {"path": "/tmp/.mssh-in-xyz"}
        self.assertIn("mkfifo -m 600 /tmp/.mssh-in-xyz", prog)
        # The readiness byte has to come before the blocking open, or the
        # command could be framed against a path that does not exist yet.
        self.assertLess(prog.index("echo R"), prog.index("exec 3>"))
        # ...and the unlink after it, or the writer would remove the name
        # before the reader could open it.
        self.assertLess(prog.index("exec 3>"), prog.index("rm -f"))


class TestFdSink(unittest.TestCase):
    """The output path: bytes to a pipe, never blocking, never truncated."""

    def _pipe(self):
        readfd, writefd = os.pipe()
        self.addCleanup(lambda: self._close(readfd))
        self.addCleanup(lambda: self._close(writefd))
        os.set_blocking(writefd, False)
        return readfd, writefd

    @staticmethod
    def _close(fd):
        try:
            os.close(fd)
        except OSError:
            pass

    def test_bytes_arrive_unchanged(self):
        readfd, writefd = self._pipe()
        sink = mssh._FdSink(writefd)
        sink.write(b"hello\n")
        sink.write(b"\x00\x1b[31m\xff binary")
        self.assertEqual(sink.backlog, 0)
        self.assertEqual(os.read(readfd, 4096),
                         b"hello\n\x00\x1b[31m\xff binary")

    def test_a_full_pipe_is_held_not_blocked_on(self):
        # The loop has one thread, so a write that blocked would stop the
        # channel and stdin too.  What will not fit has to wait here instead.
        readfd, writefd = self._pipe()
        sink = mssh._FdSink(writefd)
        payload = b"x" * (1024 * 1024)
        sink.write(payload)
        self.assertGreater(sink.backlog, 0, "a 1 MiB write fitted in a pipe?")

        # Draining the reader lets the held bytes through, and every one of
        # them is still there: a full pipe delays output, it does not drop it.
        got = b""
        while len(got) < len(payload):
            got += os.read(readfd, 65536)
            sink.flush()
        self.assertEqual(got, payload)
        self.assertEqual(sink.backlog, 0)

    def test_drain_hands_over_the_backlog(self):
        # The command has ended and the descriptor is about to close: whatever
        # the pipe was too full to take is still the command's output.
        readfd, writefd = self._pipe()
        sink = mssh._FdSink(writefd)
        payload = b"y" * (512 * 1024)
        sink.write(payload)
        self.assertGreater(sink.backlog, 0)

        got = []

        def reader():
            while len(b"".join(got)) < len(payload):
                chunk = os.read(readfd, 65536)
                if not chunk:
                    break
                got.append(chunk)

        import threading
        worker = threading.Thread(target=reader)
        worker.start()
        sink.drain(limit=30.0)
        worker.join(30)
        self.assertEqual(sink.backlog, 0)
        self.assertEqual(b"".join(got), payload)

    def test_a_closed_reader_is_reported_not_raised(self):
        # A client that walked away must not take the daemon down with a
        # SIGPIPE: the command still frames on its own marker.
        readfd, writefd = self._pipe()
        os.close(readfd)
        sink = mssh._FdSink(writefd)
        sink.write(b"nobody is listening")
        self.assertFalse(sink.flush())
        self.assertEqual(sink.backlog, 0)

    def test_buf_sink_collects_for_the_banner(self):
        sink = mssh._BufSink()
        sink.write(b"one ")
        sink.write(b"two")
        self.assertEqual(sink.value(), b"one two")
        self.assertTrue(sink.flush())
        self.assertEqual(sink.backlog, 0)
        self.assertIsNone(sink.wfd)


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
        sink = TimedSink()
        started = time.time()
        _out, status, _ = drain(
            self.sess, "for i in 1 2 3; do echo tick$i; sleep 0.4; done",
            20.0, sink)
        self.assertEqual(status, 0)
        arrivals = [(when - started, data) for when, data in sink.arrivals]
        self.assertEqual([a[1].strip() for a in arrivals],
                         [b"tick1", b"tick2", b"tick3"])
        # Each tick must arrive near its own moment, not all at the end.
        self.assertLess(arrivals[0][0], 0.8,
                        "first line arrived at %.2fs" % arrivals[0][0])
        self.assertGreater(arrivals[2][0] - arrivals[0][0], 0.5)

    def test_a_single_line_is_released_without_a_following_line(self):
        """The reported bug: 'cat' fed one line showed nothing until EOF.

        A line is held back so the framing printf's newline cannot be mistaken
        for the command's own, but the hold has to end at an idle pause: an
        interactive filter produces one line and then waits, so holding until
        the *next* line meant holding until the input was closed.
        """
        sink = TimedSink()
        started = time.time()
        # 'echo' then a long sleep produces exactly one line and keeps running,
        # the same shape as a filter waiting for more input.
        out, status, _ = drain(self.sess, "echo only-line; sleep 3", 12.0, sink)
        self.assertEqual(status, 0)
        self.assertIn(b"only-line", out)
        self.assertTrue(sink.arrivals, "the line was never emitted")
        first = sink.arrivals[0][0] - started
        # It must appear on the idle release, not 3s later when the sleep ends.
        self.assertLess(first, 2.0,
                        "one-line output waited %.2fs for a second line" % first)

    def test_a_held_line_does_not_gain_a_newline(self):
        # The reason the holdback exists: a command whose last line has no
        # newline of its own must not be given one, even though the idle
        # release now hands lines over early.
        out, status, _ = drain(self.sess, "echo one; sleep 1; printf two", 15.0)
        self.assertEqual(status, 0)
        self.assertEqual(out, b"one\ntwo")

    def test_endless_command_streams_then_times_out(self):
        """The reported bug: a command that never ends showed nothing."""
        sink = TimedSink()
        _out, _status, timed_out = drain(
            self.sess, "while true; do echo forever; sleep 0.2; done", 1.5,
            sink)
        self.assertTrue(timed_out, "should have timed out")
        arrivals = [data for _when, data in sink.arrivals]
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
        sink = TimedSink()
        drain(self.sess, "printf 'Continue? '; sleep 2", 4.0, sink)
        self.assertTrue(any(b"Continue?" in data for _when, data
                            in sink.arrivals), sink.arrivals)

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

        result = {}
        done = threading.Event()
        sink = mssh._BufSink()

        def run():
            result["end"] = self.sess.run("sleep 30; echo never", 30.0, sink)
            done.set()

        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(1.0)
        self.assertTrue(self.sess.interrupt_running(),
                        "a running command should report as signalled")
        self.assertTrue(done.wait(30), "the command never ended")
        worker.join()

        self.assertNotIn(b"never", sink.value())
        self.assertEqual(result["end"], (130, False))
        out, status, _ = drain(self.sess, "echo ok", 10.0)
        self.assertEqual((out.strip(), status), (b"ok", 0))

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

    def test_a_dead_session_tells_a_command_from_a_host(self):
        # With the user optional, only the host check keeps a quoted command
        # from being read as a hostname and dialled.
        for arg in ["echo hi", "cd /var/log && ls", "wc -l"]:
            code, err = self.run_main(["--session", "u-absent", arg])
            self.assertEqual(code, 1, "%r: %s" % (arg, err))
            self.assertIn("no session", err)

    def test_regressions_still_parse(self):
        # A plain login target and a copy must still reach the connect stage,
        # i.e. fail with something other than a usage error.  The userless
        # spellings take the same path.
        for argv in [["root@10.255.255.1", "-c", "true", "-o", "0.3"],
                     ["10.255.255.1", "-c", "true", "-o", "0.3"],
                     ["./x", "root@10.255.255.1:/tmp/", "-o", "0.3"],
                     ["./x", "10.255.255.1:/tmp/", "-o", "0.3"]]:
            code, err = self.run_main(argv)
            self.assertNotEqual(code, 2, "%r: %s" % (argv, err))


if __name__ == "__main__":
    unittest.main(verbosity=2)
