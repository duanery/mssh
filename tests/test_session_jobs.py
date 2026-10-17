"""Job snapshots through the real session daemon."""

import os
import subprocess
import sys
import time
from unittest import mock

from test_session_daemon import ENV, FAKE, SessionTest, mssh, run


class TestSessionJobs(SessionTest):
    def snapshot(self):
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            return mssh.session_request(self.name, {"op": "jobs"}, timeout=3)["jobs"]

    def wait_jobs(self, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = self.snapshot()
            if len(rows) == count:
                return rows
            time.sleep(0.03)
        self.fail("expected %d jobs, got %r" % (count, rows))

    def test_idle_shell_is_not_a_job(self):
        self.start()
        self.assertEqual(self.snapshot(), [])
        proc = run(self.name, "--jobs")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, b"No running jobs.\n")
        self.send("echo done")
        self.assertEqual(self.snapshot(), [])

    def test_shell_and_parallel_exec_jobs(self):
        self.start()
        processes = []
        try:
            for options in ([], ["--exec"], ["--exec"]):
                processes.append(subprocess.Popen(
                    [sys.executable, FAKE, self.name] + options +
                    ["sleep 20"], env=ENV, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            rows = self.wait_jobs(3)
            self.assertEqual(sorted(row["kind"] for row in rows),
                             ["exec", "exec", "shell"])
            self.assertEqual(len(set(row["id"] for row in rows)), 3)
            self.assertTrue(all(row["command"] == "sleep 20" for row in rows))
            proc = run("--session", self.name, "--jobs")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(b"ELAPSED", proc.stdout)
            self.assertEqual(proc.stdout.count(b"sleep 20"), 3)
            run(self.name, "--interrupt")
            for process in processes:
                process.communicate(timeout=10)
            self.wait_jobs(0)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_timed_out_shell_command_remains_pending(self):
        self.start()
        proc = self.send("sleep 20", "--wait", "0.1")
        self.assertEqual(proc.returncode, 124)
        rows = self.wait_jobs(1)
        self.assertEqual(rows[0]["state"], "pending")
        run(self.name, "--interrupt")
        self.wait_jobs(0)

    def test_interactive_program_is_a_job(self):
        self.start("-c", "cat", "--idle", "0.1")
        rows = self.snapshot()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "program")
        self.assertEqual(rows[0]["command"], "cat")

    def test_sftp_channel_is_a_job(self):
        self.start()
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            sftp = mssh.open_session_sftp(
                self.name, 3,
                {"kind": "get", "source": "/tmp/source",
                 "destination": "./target"})
            try:
                rows = self.wait_jobs(1)
                self.assertEqual(rows[0]["kind"], "get")
                self.assertEqual(rows[0]["command"], "/tmp/source -> ./target")
                self.assertTrue(sftp.stat("/tmp"))
            finally:
                sftp.close()
        self.wait_jobs(0)

    def test_login_channel_is_a_job(self):
        self.start()
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            sftp = mssh.open_session_sftp(self.name, 3)
            try:
                rows = self.wait_jobs(1)
                self.assertEqual(rows[0]["kind"], "sftp")
            finally:
                sftp.close()
        self.wait_jobs(0)
        process = subprocess.Popen(
            [sys.executable, FAKE, self.name],
            env=ENV, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            rows = self.wait_jobs(1)
            self.assertEqual(rows[0]["kind"], "login")
            self.assertEqual(rows[0]["command"], "login shell")
            process.communicate(input=b"exit\n", timeout=10)
            self.wait_jobs(0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_older_daemon_reports_unsupported_jobs(self):
        with mock.patch.object(mssh, "session_request", return_value={}):
            with self.assertRaisesRegex(SystemExit, "does not support --jobs"):
                mssh.run_session_jobs(self.name)

    def test_reject_conflicting_options(self):
        self.start()
        for options in (["--status"], ["--stop"], ["--interrupt"],
                        ["--exec"], ["-c", "true"], ["-r"],
                        ["--put", "local", "remote"]):
            proc = run(self.name, "--jobs", *options)
            self.assertEqual(proc.returncode, 2, proc.stderr)
        proc = run("--jobs")
        self.assertEqual(proc.returncode, 2)
