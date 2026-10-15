"""Shell initialization against local pipe and PTY channels; no SSH server."""

import contextlib
import io
import os
import re
import shlex
import shutil
import socket
import tempfile
import unittest
from unittest import mock

from fakechain import FakeClient, mssh


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
                rb"\. (/tmp/mssh-rc\.[A-Za-z0-9]+)/rc",
                b"".join(self.transport.sent)):
            shutil.rmtree(path.decode("ascii"), ignore_errors=True)
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
        paths = re.findall(rb"\. (/tmp/mssh-rc\.[A-Za-z0-9]+)/rc",
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
        script = b'command touch "$(dirname "${BASH_SOURCE}")/extra"\n'
        chan = self.transport.open_session()
        chan.exec_command("exec /bin/bash --noprofile --norc")
        with self.assertRaisesRegex(RuntimeError, "could not remove"):
            mssh._source_rcfile(self.transport, chan, script, 5)

    def test_no_rcfile_opens_no_upload_channel(self):
        self.assertEqual(mssh._source_rcfile(
            self.transport, None, None, 1), b"")
        self.assertEqual(self.transport.commands, [])

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
                self.assertNotIn(b"mssh-rc.", recalled)
                chan.sendall(b"echo new-user-command; exit\n")
                while chan.recv(65536):
                    pass
                chan.close()
                with open(history_file) as history:
                    saved = history.read()
                self.assertIn("echo old-user-command\n", saved)
                self.assertIn("echo new-user-command; exit\n", saved)
                self.assertNotIn("mssh-rc.", saved)
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

                with mock.patch.object(mssh, "_rc_command", side_effect=[
                        (b"/tmp/mssh-rc.abcdef1234\n", 0)]):
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

        with mock.patch.object(mssh, "_rc_command", side_effect=[
                (b"/tmp/mssh-rc.abcdef1234\n", 0)]):
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

        with mock.patch.object(mssh, "_rc_command", side_effect=[
                (b"/tmp/mssh-rc.abcdef1234\n", 0)]):
            output = mssh._source_rcfile(None, BufferedChan(), b"true\n", 1)
        self.assertEqual(
            output, b"Last login: yesterday\r\nWelcome to tlinux\r\n")

    def test_banner_flushed_before_delay_and_prompt_return(self):
        events = []

        class BufferedChan:
            def settimeout(self, timeout):
                pass

            def send(self, line):
                token = line.split(b"'")[-2]
                self.output = (b"Welcome\r\n" + line.replace(b"\n", b"\r\n")
                               + b"\x1e" + token + b":0:0\x1f\r\n$ ")
                return len(line)

            def recv_stderr_ready(self):
                return False

            def recv(self, size):
                output, self.output = self.output, b""
                return output

        with mock.patch.object(mssh, "_rc_command",
                               return_value=(b"/tmp/mssh-rc.abcdef1234\n", 0)):
            with mock.patch.object(mssh.time, "sleep",
                                   side_effect=lambda seconds:
                                   events.append(("sleep", seconds))):
                prompt = mssh._source_rcfile(
                    None, BufferedChan(), b"true\n", 1,
                    banner_sink=lambda data: events.append(("banner", data)))
        self.assertEqual(events, [("banner", b"Welcome\r\n"), ("sleep", 0.2)])
        self.assertEqual(prompt, b"$ ")


if __name__ == "__main__":
    unittest.main()
