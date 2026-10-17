"""File transfers through the real session daemon and a local SFTP server."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from test_session_daemon import ENV, FAKE, SessionTest, mssh, run


@unittest.skipUnless(os.path.exists("/usr/libexec/openssh/sftp-server"),
                     "no sftp-server")
class TestSessionCopy(SessionTest):
    def test_upload_download_binary_and_shell_state(self):
        self.start()
        self.send("cd /; SESSION_COPY_STATE=kept")
        with tempfile.TemporaryDirectory(prefix="mssh-session-copy-") as tmp:
            source = os.path.join(tmp, "source")
            remote = os.path.join(tmp, "remote")
            target = os.path.join(tmp, "download")
            data = bytes(range(256)) * 500
            with open(source, "wb") as file:
                file.write(data)
            os.chmod(source, 0o750)
            proc = run(self.name, "-p", "--put", source, remote)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(os.stat(remote).st_mode & 0o777, 0o750)
            proc = run("--session", self.name, "--get", remote, target)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(target, "rb") as file:
                self.assertEqual(file.read(), data)
            self.assertEqual(self.send('echo "$SESSION_COPY_STATE"; pwd').stdout,
                             b"kept\n/\n")

    def test_recursive_destination_directory(self):
        self.start()
        with tempfile.TemporaryDirectory(prefix="mssh-session-tree-") as tmp:
            source = os.path.join(tmp, "tree")
            remote = os.path.join(tmp, "remote")
            target = os.path.join(tmp, "download")
            os.mkdir(source)
            os.mkdir(remote)
            os.mkdir(target)
            with open(os.path.join(source, "file"), "wb") as file:
                file.write(b"contents")
            for args in (
                    (self.name, "-r", "--put", source, remote),
                    (self.name, "-r", "--get", os.path.join(remote, "tree"), target)):
                proc = run(*args)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(os.path.join(target, "tree", "file"), "rb") as file:
                self.assertEqual(file.read(), b"contents")

    def test_missing_source_keeps_session_usable(self):
        self.start()
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(self.name, "--get", tmp + "/missing", tmp + "/out")
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_conflicting_options_rejected(self):
        self.start()
        for options in (
                ("--exec",), ("--status",), ("-c", "true"), ("-t",)):
            proc = run(self.name, "--put", "local", "/tmp/remote", *options)
            self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_parallel_transfers_while_shell_busy(self):
        self.start()
        busy = subprocess.Popen(
            [sys.executable, FAKE, self.name, "sleep 2; echo finished"],
            env=ENV, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.2)
            with tempfile.TemporaryDirectory(prefix="mssh-copy-parallel-") as tmp:
                source = os.path.join(tmp, "source")
                with open(source, "wb") as file:
                    file.write(b"parallel-data" * 20000)
                def upload(number):
                    return run(self.name, "--put", source,
                               os.path.join(tmp, "remote-%d" % number))
                with ThreadPoolExecutor(max_workers=3) as pool:
                    results = list(pool.map(upload, range(3)))
                for result in results:
                    self.assertEqual(result.returncode, 0, result.stderr)
            out, err = busy.communicate(timeout=10)
            self.assertEqual(busy.returncode, 0, err)
            self.assertEqual(out, b"finished\n")
        finally:
            if busy.poll() is None:
                busy.kill()
                busy.communicate()

    def test_unavailable_sftp_keeps_shell_alive(self):
        with mock.patch.dict(ENV, MSSH_FAKE_NO_SFTP="1"):
            self.start()
            proc = run(self.name, "--get", "/tmp/missing", "./unused")
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn(b"no sftp subsystem", proc.stderr)
            self.assertNotIn(b"Traceback", proc.stderr)
            self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_transfer_requires_live_session(self):
        proc = run("--session", self.name, "--put", "source", "destination")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(b"no session", proc.stderr)

    def test_interactive_program_session_supports_transfers(self):
        self.start("-c", "cat", "--idle", "0.1")
        with tempfile.TemporaryDirectory(prefix="mssh-copy-interactive-") as tmp:
            source = os.path.join(tmp, "source")
            remote = os.path.join(tmp, "remote")
            with open(source, "wb") as file:
                file.write(b"interactive-session")
            proc = run(self.name, "--put", source, remote)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(self.send("still-interactive").stdout,
                             b"still-interactive\n")

    def test_interrupt_closes_transfer_not_session(self):
        self.start()
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            sftp = mssh.open_session_sftp(self.name, 3)
            try:
                proc = run(self.name, "--interrupt")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertTrue(sftp.stat("/tmp"))
                jobs = mssh.session_request(
                    self.name, {"op": "jobs"}, timeout=3)["jobs"]
                proc = run(self.name, "--interrupt", "--job", str(jobs[0]["id"]))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                with self.assertRaises((EOFError, OSError)):
                    sftp.stat("/tmp")
            finally:
                sftp.close()
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_sftp_request_token_interrupts_only_its_channel(self):
        self.start()
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            first = mssh.open_session_sftp(
                self.name, 3, job_token="first-transfer")
            second = None
            try:
                second = mssh.open_session_sftp(
                    self.name, 3, job_token="second-transfer")
                reply = mssh.session_request(
                    self.name, {"op": "interrupt",
                                "job_token": "first-transfer"}, timeout=3)
                self.assertTrue(reply.get("interrupted"), reply)
                with self.assertRaises((EOFError, OSError)):
                    first.stat("/tmp")
                self.assertTrue(second.stat("/tmp"))
            finally:
                first.close()
                if second is not None:
                    second.close()
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_disconnected_clients_release_daemon_descriptors(self):
        self.start()
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            info = mssh.session_request(self.name, {"op": "status"}, timeout=3)
            fd_dir = "/proc/%s/fd" % info["pid"]
            time.sleep(0.1)
            baseline = len(os.listdir(fd_dir))
            for _ in range(8):
                sftp = mssh.open_session_sftp(self.name, 3)
                self.assertTrue(sftp.stat("/tmp").st_mode)
                sftp.close()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if len(os.listdir(fd_dir)) <= baseline:
                    break
                time.sleep(0.05)
            self.assertLessEqual(len(os.listdir(fd_dir)), baseline)
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_paths_with_spaces_and_colons(self):
        self.start()
        with tempfile.TemporaryDirectory(prefix="mssh-copy-paths-") as tmp:
            source = os.path.join(tmp, "local name:one")
            remote = os.path.join(tmp, "remote name:two")
            target = os.path.join(tmp, "download name:three")
            with open(source, "wb") as file:
                file.write(b"path-data")
            for args in (
                    (self.name, "--put", source, remote),
                    (self.name, "--get", remote, target)):
                proc = run(*args)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(target, "rb") as file:
                self.assertEqual(file.read(), b"path-data")

    def test_empty_file_and_preserved_timestamps(self):
        self.start()
        with tempfile.TemporaryDirectory(prefix="mssh-copy-empty-") as tmp:
            source = os.path.join(tmp, "source")
            remote = os.path.join(tmp, "remote")
            target = os.path.join(tmp, "target")
            with open(source, "wb"):
                pass
            os.chmod(source, 0o640)
            os.utime(source, (1600000000, 1600000001))
            for args in (
                    (self.name, "--put", source, remote, "-p"),
                    (self.name, "--get", remote, target, "-p")):
                proc = run(*args)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            attrs = os.stat(target)
            self.assertEqual(attrs.st_size, 0)
            self.assertEqual(attrs.st_mode & 0o777, 0o640)
            self.assertEqual(int(attrs.st_mtime), 1600000001)

    def test_ctrl_c_removes_partial_download(self):
        self.start()
        with tempfile.TemporaryDirectory(prefix="mssh-copy-interrupt-") as tmp:
            remote = os.path.join(tmp, "remote")
            target = os.path.join(tmp, "target")
            with open(remote, "wb") as file:
                file.truncate(8 * 1024 * 1024)
            # Slow callbacks, not the protocol, to reliably interrupt between
            # packet operations while using the real SFTP server and daemon.
            script = (
                "import sys,time; sys.path.insert(0,sys.argv.pop(1)); "
                "import fakechain; m=fakechain.mssh; "
                "original=m._transfer_callback; "
                "m._transfer_callback=lambda s,l: "
                "(lambda callback: lambda d,t: "
                "(time.sleep(.01),callback(d,t)))(original(s,l)); "
                "sys.exit(m.main(sys.argv[1:]))"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script, os.path.dirname(FAKE),
                 self.name, "--get", remote, target],
                env=ENV, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if os.path.exists(target) and os.path.getsize(target) > 0:
                        break
                    if proc.poll() is not None:
                        self.fail("download exited before interrupt: %r"
                                  % (proc.communicate(),))
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(target))
                proc.send_signal(signal.SIGINT)
                out, err = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 130, err)
                self.assertNotIn(b"Traceback", err)
                self.assertFalse(os.path.exists(target))
                self.assertEqual(self.send("echo alive").stdout, b"alive\n")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()


if __name__ == "__main__":
    unittest.main()
