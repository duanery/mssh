"""Shell initialization against local pipe and PTY channels; no SSH server."""

import contextlib
import io
import os
import re
import shlex
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from fakechain import FakeClient, mssh

DEFAULT_RC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mssh.rc")


class RecordingTransport:
    def __init__(self):
        self.client = FakeClient()
        self.commands = []
        self.sent = []

    def open_session(self, timeout=None):
        chan = self.client.get_transport().open_session(timeout)
        original = chan.exec_command

        def execute(command):
            self.commands.append(command)
            original(command)

        chan.exec_command = execute
        original_send = chan.send

        def send(data):
            sent = original_send(data)
            self.sent.append(bytes(data[:sent]))
            return sent

        chan.send = send
        return chan

    def close(self):
        self.client.close()


class TestRcfile(unittest.TestCase):
    def setUp(self):
        self.transport = RecordingTransport()
        self.temp = tempfile.TemporaryDirectory(prefix="mssh-rc-test-")
        self.record = os.path.join(self.temp.name, "path")

    def tearDown(self):
        self.transport.close()
        for path in re.findall(
                rb"\. (/tmp/\.mssh-rc-[A-Za-z0-9]+)",
                b"".join(self.transport.sent)):
            try:
                os.unlink(path.decode("ascii"))
            except IsADirectoryError:
                os.rmdir(path.decode("ascii"))
            except FileNotFoundError:
                pass
        self.temp.cleanup()

    def shell(self, pty=False):
        chan = self.transport.open_session()
        if pty:
            chan.get_pty(term="dumb", width=80, height=24)
        chan.invoke_shell()
        return chan

    def script(self, extra=""):
        return (
            "printf '%s\\n' \"${BASH_SOURCE:-$_}\" > %s\n"
            "RC_VALUE='value with spaces'\n"
            "export RC_VALUE\n"
            "rc_function() { printf 'function-ok\\n'; }\n"
            "cd %s\n"
            "echo hidden-output\n"
            "echo hidden-error >&2\n"
            "%s\n"
            % ("%s", shlex.quote(self.record),
               shlex.quote(self.temp.name), extra)
        ).encode()

    def assert_cleaned(self):
        paths = re.findall(rb"\. (/tmp/\.mssh-rc-[A-Za-z0-9]+)",
                           b"".join(self.transport.sent))
        self.assertTrue(paths)
        for directory in paths:
            self.assertFalse(os.path.exists(directory.decode("ascii")))
        self.assertFalse(any(command.startswith("rm -f ")
                             for command in self.transport.commands))

    def test_state_and_cleanup_with_and_without_pty(self):
        for pty in (False, True):
            with self.subTest(pty=pty):
                chan = self.shell(pty)
                channels_before = len(self.transport.commands)
                initial = mssh._source_rcfile(
                    self.transport, chan, self.script(), 5)
                self.assertNotIn(b"hidden", initial)
                self.assertNotIn(b"\x1e", initial)
                self.assert_cleaned()
                self.assertEqual(len(self.transport.commands), channels_before + 1)
                chan.settimeout(2)
                chan.sendall(b'printf "VALUE=%s\\n" "$RC_VALUE"; '
                             b'rc_function; pwd; exit\n')
                output = initial
                while True:
                    chunk = chan.recv(65536)
                    if not chunk:
                        break
                    output += chunk
                self.assertIn(b"VALUE=value with spaces", output)
                self.assertIn(b"function-ok", output)
                self.assertIn(self.temp.name.encode(), output)
                chan.close()

    def test_nonzero_return_cleans_up(self):
        chan = self.shell()
        with self.assertRaisesRegex(RuntimeError, "exit status 7"):
            mssh._source_rcfile(self.transport, chan,
                               self.script("return 7"), 5)
        self.assert_cleaned()

    def test_shell_exit_opens_no_cleanup_channel(self):
        chan = self.shell()
        with self.assertRaisesRegex(RuntimeError, "shell exited"):
            mssh._source_rcfile(self.transport, chan,
                               self.script("exit 3"), 5)
        self.assertEqual(len(self.transport.commands), 2)

    def test_timeout_opens_no_cleanup_channel(self):
        chan = self.shell()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            mssh._source_rcfile(self.transport, chan,
                               self.script("sleep 2"), 1)
        self.assertEqual(len(self.transport.commands), 2)

    def test_cleanup_failure_is_reported(self):
        script = (b'command rm -f "${BASH_SOURCE}"\n'
                  b'command mkdir "${BASH_SOURCE}"\n')
        chan = self.transport.open_session()
        chan.exec_command("exec /bin/bash --noprofile --norc")
        with self.assertRaisesRegex(RuntimeError, "could not remove"):
            mssh._source_rcfile(self.transport, chan, script, 5)

    def test_retained_file_sources_without_upload(self):
        path = mssh._upload_rcfile(self.transport, b"export RC_VALUE=retained\n", 5)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        for _ in range(2):
            chan = self.shell()
            count = len(self.transport.commands)
            mssh._source_rcfile(self.transport, chan, None, 5, remote_path=path)
            self.assertEqual(len(self.transport.commands), count)
            self.assertTrue(os.path.isfile(path))

    def test_session_reupload_and_cleanup(self):
        chan = self.shell()
        session = mssh._Session(chan, "__test__", "shell", None, 0.4,
                                "localhost", None, self.transport, 5)
        session.rcfile_data = b"export RC_VALUE=retained\n"
        session.setup_rcfile()
        first = session.rcfile_path
        replacement = self.shell()
        with mock.patch.object(mssh, "_open_shell",
                               return_value=([], self.transport, replacement,
                                             "__test_reconnected__")):
            self.assertTrue(session.rebuild(), session.last_error)
        second = session.rcfile_path
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.isfile(first))
        self.assertTrue(os.path.isfile(second))
        session.cleanup_rcfiles()
        self.assertFalse(os.path.exists(first))
        self.assertFalse(os.path.exists(second))

    def test_no_rcfile_opens_no_upload_channel(self):
        self.assertEqual(mssh._source_rcfile(
            self.transport, None, None, 1), b"")
        self.assertEqual(self.transport.commands, [])

    def test_remote_temp_paths_share_naming_and_randomness(self):
        for kind in ("in", "rc"):
            paths = [mssh._remote_temp_path(kind) for _ in range(10)]
            self.assertEqual(len(set(paths)), len(paths))
            for path in paths:
                self.assertRegex(path, r"\A/tmp/\.mssh-%s-[A-Za-z0-9]{16}\Z" % kind)
        with self.assertRaises(ValueError):
            mssh._remote_temp_path("../escape")

    def test_upload_collision_preserves_existing_file(self):
        path = os.path.join(self.temp.name, "existing")
        with open(path, "wb") as existing:
            existing.write(b"keep-original")
        with mock.patch.object(mssh, "_remote_temp_path", return_value=path):
            with self.assertRaisesRegex(RuntimeError, "could not upload"):
                mssh._upload_rcfile(self.transport, b"replacement", 5)
        with open(path, "rb") as existing:
            self.assertEqual(existing.read(), b"keep-original")

    def test_bundled_rc_preserves_shell_settings(self):
        for shell in ("/bin/sh", "/bin/bash"):
            for readonly in (False, True):
                with self.subTest(shell=shell, readonly=readonly):
                    setup = (
                        "TMOUT=60; PS1=prompt; HISTCONTROL=ignoredups; "
                        + ("readonly TMOUT; " if readonly else "")
                        + "set -e; . " + shlex.quote(DEFAULT_RC)
                        + '; printf "%s:%s:%s:%s:%s:%s\\n" '
                        '"$MSSH" "$MSSH_RC_LOADED" "$MSSH_RC_VERSION" '
                        '"${TMOUT-unset}" "$PS1" "$HISTCONTROL"; '
                        'test "$PATH" = "$original_path"; '
                        'test "$PWD" = "$original_pwd"; '
                        'test "$(sh -c \'printf "%s:%s:%s" "$MSSH" '
                        '"$MSSH_RC_LOADED" "$MSSH_RC_VERSION"\')" = "1:1:1"'
                    )
                    proc = subprocess.run(
                        [shell, "-c", "original_path=$PATH; original_pwd=$PWD; "
                         + setup], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    expected = "1:1:1:%s:prompt:ignoredups\n" % (
                        "60" if readonly else "unset")
                    self.assertEqual(proc.stdout, expected.encode())
                    self.assertEqual(proc.stderr, b"")

    def test_bundled_rc_through_upload_and_source(self):
        with open(DEFAULT_RC, "rb") as source:
            script = source.read()
        chan = self.shell()
        mssh._source_rcfile(self.transport, chan, script, 5)
        self.assert_cleaned()
        chan.settimeout(2)
        chan.sendall(b'printf "%s:%s:%s\\n" "$MSSH" "$MSSH_RC_LOADED" '
                     b'"$MSSH_RC_VERSION"; exit\n')
        output = b""
        while True:
            chunk = chan.recv(65536)
            if not chunk:
                break
            output += chunk
        self.assertEqual(output, b"1:1:1\n")

    def test_large_script_does_not_travel_through_pty(self):
        chan = self.shell(True)
        script = b"#" + b"x" * 200000 + b"\n" + self.script()
        mssh._source_rcfile(self.transport, chan, script, 5)
        self.assert_cleaned()

    def test_bash_history_preserves_user_commands_only(self):
        for control in ("", "ignorespace", "ignoreboth"):
            with self.subTest(control=control):
                history_file = os.path.join(self.temp.name, "history-" + control)
                with open(history_file, "w") as history:
                    history.write("echo old-user-command\n")
                chan = self.transport.open_session()
                chan.get_pty(term="dumb", width=80, height=24)
                chan.exec_command(
                    "HISTFILE=%s HISTCONTROL=%s HISTIGNORE='' HISTTIMEFORMAT='' "
                    "exec /bin/bash --noprofile --norc -i"
                    % (shlex.quote(history_file), shlex.quote(control)))
                # Flush history from the script itself, before the next prompt,
                # to ensure removal is not deferred until after initialization.
                script = b"builtin history -a\nexport RC_VALUE=ok\n"
                mssh._source_rcfile(self.transport, chan, script, 5)
                chan.settimeout(2)
                chan.sendall(b"\x1b[A\n")
                recalled = b""
                try:
                    while b"old-user-command\r\n" not in recalled:
                        recalled += chan.recv(65536)
                except socket.timeout:
                    self.fail("Up arrow did not recall the user command: %r"
                              % recalled)
                self.assertNotIn(b".mssh-rc-", recalled)
                chan.sendall(b"echo new-user-command; exit\n")
                while chan.recv(65536):
                    pass
                chan.close()
                with open(history_file) as history:
                    saved = history.read()
                self.assertIn("echo old-user-command\n", saved)
                self.assertIn("echo new-user-command; exit\n", saved)
                self.assertNotIn(".mssh-rc-", saved)
                self.assertNotIn("builtin history -d", saved)
                self.assertNotIn("builtin history -a", saved)

    def test_session_default_is_login_shell(self):
        out, status = mssh._rc_command(
            self.transport,
            "exec " + mssh.SESSION_SHELL
            .replace(' -l', ' --noprofile -l')
            + " -c 'shopt -q login_shell && echo login-shell'", 5)
        self.assertEqual(status, 0)
        self.assertIn(b"login-shell", out)

    def test_cli_rejects_non_shell_modes_and_missing_file(self):
        for args in (
                ["host", "--rcfile", "/nonexistent/mssh-rc"],
                ["host", "--rcfile", self.record, "-c", "true"],
                ["--sessions", "--rcfile", self.record],
                ["--rcfile", self.record, "--copy-id", "key", "host"]):
            with self.subTest(args=args):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        mssh.main(args)
                self.assertEqual(caught.exception.code, 2)

    def test_pty_marker_split_and_trailing_output(self):
        chan = self.shell(True)
        original_recv = chan.recv
        chan.recv = lambda size: original_recv(1)
        initial = mssh._source_rcfile(
            self.transport, chan, self.script(), 5)
        self.assertNotIn(b"hidden", initial)
        self.assert_cleaned()

    def test_banner_preserved_with_and_without_echo(self):
        for echo in (False, True):
            with self.subTest(echo=echo):
                class BufferedChan:
                    def settimeout(self, timeout):
                        pass

                    def send(self, line):
                        token = line.split(b"'")[-2]
                        self.output = (
                            b"Welcome to the server\r\nLast login: yesterday\r\n"
                            + (b"$ " + line.replace(b"\n", b"\r\n")
                               if echo else b"")
                            + b"\x1e" + token + b":0:0\x1f\r\n$ ")
                        return len(line)

                    def recv_stderr_ready(self):
                        return False

                    def recv(self, size):
                        output, self.output = self.output, b""
                        return output

                with mock.patch.object(mssh, "_upload_rcfile",
                                       return_value="/tmp/.mssh-rc-abcdef1234"):
                    output = mssh._source_rcfile(
                        None, BufferedChan(), b"true\n", 1)
                self.assertEqual(
                    output,
                    b"Welcome to the server\r\nLast login: yesterday\r\n$ ")

    def test_all_command_echoes_removed_before_banner_and_marker(self):
        class BufferedChan:
            def settimeout(self, timeout):
                pass

            def send(self, line):
                token = line.split(b"'")[-2]
                echo = line.replace(b"\n", b"\r\n")
                self.chunks = [
                    echo[:40],
                    echo[40:] + b"Last login: yesterday\r\n"
                    + b"$ " + echo + b"Welcome to tlinux\r\n",
                    b"Version 2.2\r\n\x1e" + token + b":0:0\x1f\r",
                    b"\n[root@server ~]# ",
                ]
                return len(line)

            def recv_stderr_ready(self):
                return False

            def recv(self, size):
                return self.chunks.pop(0)

        with mock.patch.object(mssh, "_upload_rcfile",
                               return_value="/tmp/.mssh-rc-abcdef1234"):
            output = mssh._source_rcfile(None, BufferedChan(), b"true\n", 1)
        self.assertEqual(
            output,
            b"Last login: yesterday\r\nWelcome to tlinux\r\n"
            b"Version 2.2\r\n[root@server ~]# ")

    def test_readline_redrawn_echo_removed(self):
        class BufferedChan:
            def settimeout(self, timeout):
                pass

            def send(self, line):
                token = line.split(b"'")[-2]
                # Readline changes the leading space and redraws part of the
                # command.  Neither line contains the original full command.
                echo = line.lstrip().replace(
                    b"; command printf", b"; \x1b[Kcommand printf")
                self.chunks = [
                    b"Last login: yesterday\r\nWelcome to tlinux\r\n",
                    b"[root@server ~]# " + echo.replace(b"\n", b"\r\n"),
                    b"\r\x1b[K[root@server ~]# " + echo[:90],
                    echo[90:] + b"\x1e" + token + b":0:0\x1f\r\n",
                ]
                return len(line)

            def recv_stderr_ready(self):
                return False

            def recv(self, size):
                return self.chunks.pop(0)

        with mock.patch.object(mssh, "_upload_rcfile",
                               return_value="/tmp/.mssh-rc-abcdef1234"):
            output = mssh._source_rcfile(None, BufferedChan(), b"true\n", 1)
        self.assertEqual(
            output, b"Last login: yesterday\r\nWelcome to tlinux\r\n")

    def test_banner_streamed_before_marker_without_sleep(self):
        events = []

        class BufferedChan:
            def settimeout(self, timeout):
                pass

            def send(self, line):
                token = line.split(b"'")[-2]
                self.chunks = [
                    b"Wel",
                    b"come\r\n" + line[:90],
                    line[90:].replace(b"\n", b"\r\n"),
                    b"\x1e" + token + b":0:0\x1f\r",
                    b"\n$ ",
                ]
                return len(line)

            def recv_stderr_ready(self):
                return False

            def recv(self, size):
                if len(self.chunks) == 2:
                    assert events == [("banner", b"Welcome\r\n")]
                return self.chunks.pop(0)

        with mock.patch.object(mssh, "_upload_rcfile",
                               return_value="/tmp/.mssh-rc-abcdef1234"):
            with mock.patch.object(mssh.time, "sleep",
                                   side_effect=lambda seconds:
                                   events.append(("sleep", seconds))):
                prompt = mssh._source_rcfile(
                    None, BufferedChan(), b"true\n", 1,
                    banner_sink=lambda data: events.append(("banner", data)))
        self.assertEqual(events, [("banner", b"Welcome\r\n")])
        self.assertEqual(prompt, b"$ ")


if __name__ == "__main__":
    unittest.main()
