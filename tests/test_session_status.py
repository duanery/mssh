"""Session status summaries, legacy replies and live daemon snapshots."""

import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_session_daemon import ENV, FAKE, SessionTest, mssh, run
from test_session_forward import free_port


def job(job_id, kind="exec", state="running", default=False, command="sleep 20",
        forward_type=None):
    return {"id": job_id, "kind": kind, "state": state, "default": default,
            "command": command, "channel": None if kind == "forward" else job_id,
            "started": 940.0, "forward_type": forward_type}


class TestStatusOutput(unittest.TestCase):
    def info(self, **fields):
        info = {"target": "root@host:22", "mode": "shell", "command": None,
                "rcfile": "/tmp/mssh-rc.example", "started": 400.0,
                "served": 4, "running": False, "pid": 123, "link": "up",
                "jobs": 0, "job_details": [], "last_activity": 985.0,
                "idle_limit": 300, "stopping": False}
        info.update(fields)
        return info

    def render(self, info):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mssh, "session_request", return_value=info) as request, \
                mock.patch.object(mssh.time, "time", return_value=1000.0), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            result = mssh.run_session_status("hop")
        request.assert_called_once_with("hop", {"op": "status"}, timeout=15.0)
        return result, out.getvalue(), err.getvalue()

    def test_idle_details_and_activity(self):
        result, out, err = self.render(self.info())
        self.assertEqual(result, 0)
        self.assertEqual(err, "")
        for line in ("session:  hop", "target:   root@host:22",
                     "program:  login shell", "rcfile:   /tmp/mssh-rc.example",
                     "uptime:   0:10:00", "commands: 4", "pid:      123",
                     "state:    idle", "default:  idle", "jobs:     0",
                     "activity: 0:00:15 ago", "expiry:   300s of inactivity",
                     "link:     up"):
            self.assertIn(line + "\n", out)
        self.assertNotIn("ELAPSED", out)

    def test_independent_jobs_are_running_with_idle_default(self):
        rows = [job(1, "exec"), job(2, "login"), job(3, "put"), job(4, "get"),
                job(5, "sftp")]
        _, out, _ = self.render(self.info(jobs=5, job_details=rows))
        self.assertIn("state:    running\n", out)
        self.assertIn("default:  idle\n", out)
        self.assertIn("jobs:     5 (exec=1, get=1, login=1, put=1, sftp=1)\n", out)
        self.assertNotIn("ELAPSED", out)
        self.assertNotIn("sleep 20", out)

    def test_default_shell_and_interactive_states(self):
        for kind in ("shell", "interactive"):
            for task_state, session_state in (("running", "running"),
                                               ("pending", "pending"),
                                               ("stopping", "interrupting")):
                with self.subTest(kind=kind, state=task_state):
                    _, out, _ = self.render(self.info(
                        jobs=1, job_details=[job(7, kind, task_state, default=True)]))
                    self.assertIn("state:    %s\n" % session_state, out)
                    self.assertIn("default:  job 7 (%s)\n" % task_state, out)

    def test_local_and_dynamic_forwarding_breakdown(self):
        rows = [job(1, "forward", command="127.0.0.1:8080 -> remote:80",
                    forward_type="local"),
                job(2, "forward", command="127.0.0.1:1080 -> SOCKS4/5",
                    forward_type="socks")]
        _, out, _ = self.render(self.info(jobs=2, job_details=rows))
        self.assertIn("state:    forwarding\n", out)
        self.assertIn("default:  idle\n", out)
        self.assertIn("jobs:     2 (forward=1, socks=1)\n", out)
        self.assertNotIn("127.0.0.1:8080", out)
        self.assertNotIn("SOCKS4/5", out)
        # Idle listeners cannot hide a pending or interrupted primary request.
        for task_state, session_state in (("pending", "pending"),
                                           ("stopping", "interrupting"),
                                           ("running", "running")):
            _, out, _ = self.render(self.info(
                jobs=3, job_details=rows + [job(3, "shell", task_state, True)]))
            self.assertIn("state:    %s\n" % session_state, out)

    def test_running_job_keeps_other_task_states_visible(self):
        _, out, _ = self.render(self.info(
            jobs=2, job_details=[job(1), job(2, "shell", "pending", True)]))
        self.assertIn("state:    running\n", out)
        self.assertIn("default:  job 2 (pending)\n", out)

    def test_lifecycle_overrides_running_jobs(self):
        cases = [
            ({"link": "down", "broken": "EOF", "retries": 0}, "disconnected"),
            ({"link": "reconnecting", "broken": "EOF", "attempt": 2,
              "retries": 10, "reconnects": 3}, "reconnecting"),
            ({"stopping": True, "link": "reconnecting"}, "stopping"),
            ({"dead": "shell exited", "stopping": True,
              "link": "reconnecting"}, "ended (shell exited)"),
        ]
        for fields, state in cases:
            with self.subTest(state=state):
                _, out, _ = self.render(self.info(
                    jobs=1, job_details=[job(1)], **fields))
                self.assertIn("state:    %s\n" % state, out)
                self.assertEqual(out.count("state:"), 1)
                if state == "reconnecting":
                    self.assertIn("attempt 2 of 10\n", out)
                    self.assertIn("rebuilt:  3 time(s) since it started\n", out)
                if state == "disconnected":
                    self.assertIn("reconnection disabled\n", out)

    def test_reconnection_refusal_and_exhausted_attempts(self):
        for fields, message in (
                ({"refusal": "password required"}, "password required"),
                ({"last_error": "connection refused"},
                 "next command arrives, last error: connection refused")):
            _, out, _ = self.render(self.info(
                link="down", broken="EOF", retries=2, **fields))
            self.assertIn(message + "\n", out)

    def test_legacy_count_only_reply(self):
        info = self.info(running=True, jobs=3)
        for field in ("job_details", "last_activity", "idle_limit", "link"):
            del info[field]
        _, out, _ = self.render(info)
        self.assertIn("state:    running a command\n", out)
        self.assertIn("jobs:     3\n", out)
        self.assertIn("link:     up\n", out)
        for absent in ("default:", "activity:", "expiry:", "ELAPSED", "exec="):
            self.assertNotIn(absent, out)

    def test_legacy_reply_without_count_is_unknown(self):
        info = self.info()
        del info["job_details"], info["jobs"]
        _, out, _ = self.render(info)
        self.assertIn("jobs:     unknown\n", out)
        self.assertNotIn("jobs:     0", out)
        self.assertNotIn("default:", out)

    def test_disabled_expiry_and_future_timestamps(self):
        row = job(1)
        row["started"] = 1100.0
        _, out, _ = self.render(self.info(
            started=1100.0, last_activity=1100.0, idle_limit=0,
            jobs=1, job_details=[row]))
        self.assertIn("uptime:   0:00:00\n", out)
        self.assertIn("activity: 0:00:00 ago\n", out)
        self.assertIn("expiry:   disabled\n", out)
        self.assertEqual(out.count("0:00:00"), 2)

    def test_error_reply_has_no_status(self):
        result, out, err = self.render({"error": "mssh: status failed\n"})
        self.assertEqual(result, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "mssh: status failed\n")

    def test_task_details_appear_only_in_jobs(self):
        rows = [job(1, command="echo first\nsecond\r\t\x1b[31m")]
        _, status, _ = self.render(self.info(jobs=1, job_details=rows))
        out = io.StringIO()
        with mock.patch.object(mssh, "session_request", return_value={"jobs": rows}), \
                mock.patch.object(mssh.time, "time", return_value=1000.0), \
                mock.patch.object(sys, "stdout", out):
            self.assertEqual(mssh.run_session_jobs("hop"), 0)
        self.assertNotIn("ELAPSED", status)
        self.assertNotIn("echo first", status)
        self.assertNotIn("\x1b", status)
        self.assertNotIn("\r", status)
        self.assertIn("ELAPSED", out.getvalue())
        self.assertIn("echo first second   [31m\n", out.getvalue())
        self.assertNotIn("\x1b", out.getvalue())
        self.assertNotIn("\r", out.getvalue())


class TestStatusDaemon(SessionTest):
    def status(self):
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            return mssh.session_request(self.name, {"op": "status"}, timeout=3)

    def wait_jobs(self, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            info = self.status()
            if len(info["job_details"]) == count:
                return info
            time.sleep(0.03)
        self.fail("expected %d jobs, got %r" % (count, info))

    def output(self):
        proc = run(self.name, "--status", timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.decode()

    def test_idle_status_does_not_refresh_activity(self):
        self.start("--session-idle", "300")
        first = self.status()
        self.assertEqual(first["job_details"], [])
        self.assertEqual(first["jobs"], 0)
        self.assertEqual(first["idle_limit"], 300)
        self.assertFalse(first["stopping"])
        self.assertIn("state:    idle\n", self.output())
        self.assertIn("link:     up\n", self.output())
        self.assertEqual(self.status()["last_activity"], first["last_activity"])
        self.send("true")
        self.assertGreater(self.status()["last_activity"], first["last_activity"])

    def test_parallel_exec_is_running_while_default_is_idle(self):
        self.start()
        process = subprocess.Popen(
            [sys.executable, FAKE, self.name, "--exec", "sleep 20"],
            env=ENV, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            info = self.wait_jobs(1)
            self.assertFalse(info["running"])
            self.assertFalse(info["job_details"][0]["default"])
            out = self.output()
            self.assertIn("state:    running\n", out)
            self.assertIn("default:  idle\n", out)
            self.assertIn("jobs:     1 (exec=1)\n", out)
            self.assertNotIn("sleep 20", out)
            proc = run(self.name, "--stop", "--job",
                       str(info["job_details"][0]["id"]))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            process.communicate(timeout=10)
            self.wait_jobs(0)
            self.assertIn("state:    idle\n", self.output())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_pending_default_is_not_consumed_by_status(self):
        self.start()
        proc = self.send("sleep 20", "--wait", "0.1")
        self.assertEqual(proc.returncode, 124, proc.stderr)
        info = self.wait_jobs(1)
        row = info["job_details"][0]
        self.assertTrue(row["default"])
        self.assertEqual(row["state"], "pending")
        out = self.output()
        self.assertIn("state:    pending\n", out)
        self.assertIn("default:  job %s (pending)\n" % row["id"], out)
        self.assertEqual(self.status()["job_details"][0]["id"], row["id"])
        proc = run(self.name, "--interrupt")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_jobs(0)

    def test_transfer_snapshot_labels_source_and_destination(self):
        self.start()
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            sftp = mssh.open_session_sftp(
                self.name, 3, {"kind": "get", "source": "/remote/file",
                               "destination": "./local"})
        try:
            row = self.wait_jobs(1)["job_details"][0]
            self.assertFalse(row["default"])
            self.assertEqual(row["kind"], "get")
            out = self.output()
            self.assertIn("state:    running\n", out)
            self.assertIn("jobs:     1 (get=1)\n", out)
            self.assertNotIn("/remote/file", out)
            self.assertNotIn("./local", out)
            proc = run(self.name, "--jobs")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(b"/remote/file", proc.stdout)
            self.assertIn(b"./local", proc.stdout)
        finally:
            sftp.close()
        self.wait_jobs(0)

    def test_local_and_socks_listeners_show_details(self):
        self.start("-L", "127.0.0.1:%d:service.internal:80" % free_port(),
                   "-D", "127.0.0.1:%d" % free_port())
        info = self.wait_jobs(2)
        rows = info["job_details"]
        self.assertEqual(info["jobs"], len(rows))
        self.assertEqual(sorted(row["forward_type"] for row in rows),
                         ["local", "socks"])
        self.assertTrue(all(not row["default"] for row in rows))
        out = self.output()
        self.assertIn("state:    forwarding\n", out)
        self.assertIn("jobs:     2 (forward=1, socks=1)\n", out)
        self.assertNotIn("service.internal:80", out)
        self.assertNotIn("SOCKS4/5", out)
        proc = run(self.name, "--jobs")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"service.internal:80", proc.stdout)
        self.assertIn(b"SOCKS4/5", proc.stdout)

    def test_reconnect_state_takes_precedence_over_surviving_listener(self):
        with tempfile.TemporaryDirectory(prefix="mssh-status-reconnect-") as tmp:
            down = os.path.join(tmp, "down")
            with mock.patch.dict(ENV, MSSH_FAKE_DOWN=down):
                self.start("--reconnect", "10", "-D",
                           "127.0.0.1:%d" % free_port())
                before = self.status()
                with open(down, "w"):
                    pass
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        info = self.status()
                        if info["link"] == "reconnecting":
                            break
                        # Link detection belongs to the idle accept timeout,
                        # not to status. Leave time for that poll to run.
                        time.sleep(1.1)
                    self.assertEqual(info["link"], "reconnecting")
                    self.assertEqual(info["job_details"], before["job_details"])
                    self.assertIn("state:    reconnecting\n", self.output())
                    self.assertIn("default:  idle\n", self.output())
                    self.assertIn("jobs:     1 (socks=1)\n", self.output())
                finally:
                    os.unlink(down)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    info = self.status()
                    if info["link"] == "up":
                        break
                    time.sleep(0.05)
                self.assertEqual(info["link"], "up")
                self.assertIn("state:    forwarding\n", self.output())
                self.assertEqual(info["job_details"], before["job_details"])


if __name__ == "__main__":
    unittest.main()
