"""Job snapshots through the real session daemon."""

import os
import signal
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
            proc = run(self.name, "--interrupt")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            processes[0].communicate(timeout=10)
            remaining = self.wait_jobs(2)
            self.assertTrue(all(row["kind"] == "exec" for row in remaining))
            self.assertTrue(all(process.poll() is None for process in processes[1:]))
            proc = run(self.name, "--interrupt", "--job", str(remaining[0]["id"]))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            remaining = self.wait_jobs(1)
            proc = run(self.name, "--interrupt", "--job", str(remaining[0]["id"]))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for process in processes[1:]:
                process.communicate(timeout=10)
                self.assertEqual(process.returncode, 130)
            self.wait_jobs(0)
            self.assertEqual(self.send("echo alive").stdout, b"alive\n")
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
        proc = run(self.name, "--interrupt", "--job", str(rows[0]["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_jobs(0)
        proc = run(self.name, "--interrupt", "--job", str(rows[0]["id"]))
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"is not running", proc.stderr)
        self.assertEqual(self.send("echo next").stdout, b"next\n")

    def test_targeted_running_shell_keeps_other_jobs_and_shell_alive(self):
        self.start()
        process = subprocess.Popen(
            [sys.executable, FAKE, self.name, "sleep 20"],
            env=ENV, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            rows = self.wait_jobs(1)
            proc = run(self.name, "--interrupt", "--job", str(rows[0]["id"]))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            _, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, err)
            self.wait_jobs(0)
            self.assertEqual(self.send("echo alive").stdout, b"alive\n")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_exec_client_ctrl_c_only_interrupts_its_own_job(self):
        self.start()
        processes = []
        try:
            for _ in range(2):
                processes.append(subprocess.Popen(
                    [sys.executable, FAKE, self.name, "--exec", "sleep 20"],
                    env=ENV, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            self.wait_jobs(2)
            processes[0].send_signal(signal.SIGINT)
            _, err = processes[0].communicate(timeout=10)
            self.assertEqual(processes[0].returncode, 130, err)
            self.wait_jobs(1)
            self.assertIsNone(processes[1].poll())
            self.assertEqual(self.send("echo alive").stdout, b"alive\n")
        finally:
            run(self.name, "--stop")
            for process in processes:
                process.communicate(timeout=10)

    def test_stop_closes_shell_exec_login_and_sftp(self):
        self.start()
        processes = []
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            sftp = mssh.open_session_sftp(self.name, 3)
        try:
            for options in (["sleep 20"], ["--exec", "sleep 20"], []):
                processes.append(subprocess.Popen(
                    [sys.executable, FAKE, self.name] + options,
                    env=ENV, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            self.wait_jobs(4)
            proc = run(self.name, "--stop")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for process in processes:
                process.communicate(timeout=10)
            with self.assertRaises((EOFError, OSError)):
                sftp.stat("/tmp")
        finally:
            sftp.close()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_reject_invalid_job_options(self):
        self.start()
        for options in (["--job", "1"], ["--interrupt", "--job", "-1"],
                        ["--interrupt", "--job", "bad"]):
            proc = run(self.name, *options)
            self.assertEqual(proc.returncode, 2, proc.stderr)
        proc = run(self.name, "--interrupt", "--job", "9999")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            for invalid in (-1, True, "1"):
                reply = mssh.session_request(
                    self.name, {"op": "interrupt", "job": invalid}, timeout=3)
                self.assertIn("nonnegative integer", reply["error"])

    def test_interactive_request_is_default_job(self):
        self.start("-c", "cat", "--idle", "0.1")
        self.assertEqual(self.snapshot(), [])
        proc = run(self.name, "--interrupt")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"nothing was running", proc.stderr)
        # A reply timeout leaves the primary request pending, not the idle
        # program itself. Resynchronizing it ends that job's lifecycle.
        proc = self.send("hello", "--wait", "0.01")
        self.assertEqual(proc.returncode, 124, proc.stderr)
        rows = self.snapshot()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "interactive")
        self.assertEqual(rows[0]["command"], "hello")
        self.assertEqual(rows[0]["state"], "pending")
        proc = run(self.name, "--interrupt", "--job", str(rows[0]["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_jobs(0)

    def test_running_interactive_request_can_be_interrupted(self):
        self.start("-c", "python3 -iq", "--prompt", ">>> $")
        for targeted in (False, True):
            process = subprocess.Popen(
                [sys.executable, FAKE, self.name,
                 "__import__('time').sleep(20)"],
                env=ENV, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                rows = self.wait_jobs(1)
                self.assertEqual(rows[0]["kind"], "interactive")
                options = ["--job", str(rows[0]["id"])] if targeted else []
                proc = run(self.name, "--interrupt", *options)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                _, err = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 130, err)
                self.wait_jobs(0)
                self.assertEqual(self.send("print('alive')").stdout, b"alive\n")
                self.assertEqual(self.snapshot(), [])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

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
            proc = run(self.name, "--interrupt", "--job", str(rows[0]["id"]))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130)
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
