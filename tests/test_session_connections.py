"""Per-forward connection inspection/cancellation, including pending opens."""

import io
import os
import shutil
import socket
import threading
import time
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from test_session_daemon import ENV, SessionTest, mssh, run
from test_session_forward import Echo, TCPServer, free_port
from test_session_socks import read_exact, request_packet
import test_session_status as status_tests
from test_session_unix_forward import OpenSSHForwardTest


def wait_for(test, query, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = query()
        if predicate(value):
            return value
        time.sleep(0.02)
    test.fail("timed out waiting for connection state: %r" % value)


class TestSessionConnections(SessionTest):
    def setUp(self):
        super(TestSessionConnections, self).setUp()
        self.server = TCPServer(("127.0.0.1", 0), Echo)
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.daemon = True
        self.worker.start()
        self.sockets = []

    def tearDown(self):
        for conn in self.sockets:
            conn.close()
        super(TestSessionConnections, self).tearDown()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(3)

    def request(self, payload):
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            return mssh.session_request(self.name, payload, timeout=5)

    def jobs(self):
        return self.request({"op": "jobs"})["jobs"]

    def connections(self, job):
        return self.request({"op": "connections", "job": job})["connections"]

    def connect(self, endpoint, dynamic=False):
        if isinstance(endpoint, str):
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(3)
            conn.connect(endpoint)
        else:
            conn = socket.create_connection(("127.0.0.1", endpoint), timeout=3)
        self.sockets.append(conn)
        if dynamic:
            conn.sendall(b"\x05\x01\x00")
            self.assertEqual(read_exact(conn, 2), b"\x05\x00")
            conn.sendall(request_packet("127.0.0.1", self.server.server_address[1]))
            self.assertEqual(read_exact(conn, 10)[0:2], b"\x05\x00")
        return conn

    def active(self, job, count):
        return wait_for(self, lambda: self.connections(job),
                        lambda rows: len(rows) == count and
                        all(row["state"] == "active" for row in rows))

    def test_counts_channels_and_single_connection_stop_all_forward_types(self):
        with TemporaryDirectory(prefix="mssh-connections-") as directory:
            for index, flag in enumerate(("-L", "-R", "-D", "-L", "-R")):
                with self.subTest(flag=flag, unix=index >= 3):
                    endpoint = os.path.join(directory, "%s.sock" % index) \
                        if index >= 3 else free_port()
                    left = endpoint if isinstance(endpoint, str) else "127.0.0.1:%d" % endpoint
                    spec = left if flag == "-D" else "%s:127.0.0.1:%d" % (
                        left, self.server.server_address[1])
                    if index == 0:
                        self.start(flag, spec)
                    else:
                        configured = run(self.name, flag, spec)
                        self.assertEqual(configured.returncode, 0, configured.stderr)
                    job = self.jobs()[-1]["id"]
                    self.assertEqual(self.jobs()[-1]["connections"], 0)
                    empty = run(self.name, "--job", str(job), "--connections")
                    self.assertIn(b"No running connections", empty.stdout)
                    first = self.connect(endpoint, flag == "-D")
                    second = self.connect(endpoint, flag == "-D")
                    for conn in (first, second):
                        conn.sendall(b"echo")
                        self.assertEqual(read_exact(conn, 4), b"echo")
                    rows = self.active(job, 2)
                    self.assertEqual([row["id"] for row in rows], [1, 2])
                    self.assertTrue(all(isinstance(row["channel"], int) for row in rows))
                    self.assertNotEqual(rows[0]["channel"], rows[1]["channel"])
                    self.assertTrue(all(row["listener"] == left for row in rows))
                    self.assertTrue(all(row["destination"] == "127.0.0.1:%d" %
                                        self.server.server_address[1] for row in rows))
                    self.assertEqual(next(row for row in self.jobs()
                                          if row["id"] == job)["connections"], 2)
                    listing = run(self.name, "--job", str(job), "--connections")
                    self.assertEqual(listing.returncode, 0, listing.stderr)
                    self.assertEqual(listing.stdout.decode().splitlines()[0].split(),
                                     ["ID", "CHANNEL", "STATE", "ELAPSED",
                                      "SOURCE", "LISTENER", "DESTINATION"])
                    self.assertIn(str(rows[0]["channel"]).encode(), listing.stdout)
                    table = run(self.name, "--jobs")
                    self.assertIn(b"CONNS", table.stdout)
                    control = "--stop" if index % 2 == 0 else "--interrupt"
                    stopped = run(self.name, "--job", str(job), "--connection", "1", control)
                    self.assertEqual(stopped.returncode, 0, stopped.stderr)
                    self.assertIn(b"interrupted connection 1", stopped.stderr)
                    self.assertEqual(first.recv(1), b"")
                    self.assertEqual([row["id"] for row in self.active(job, 1)], [2])
                    second.sendall(b"alive")
                    self.assertEqual(read_exact(second, 5), b"alive")
                    third = self.connect(endpoint, flag == "-D")
                    third.sendall(b"new")
                    self.assertEqual(read_exact(third, 3), b"new")
                    self.assertEqual([row["id"] for row in self.active(job, 2)], [2, 3])
                    second.close()
                    third.close()
                    self.active(job, 0)
                    failed = run(self.name, "--job", str(job), "--connection", "1", "--stop")
                    self.assertEqual(failed.returncode, 1, failed.stderr)
                    self.assertEqual(run(self.name, "--job", str(job), "--stop").returncode, 0)
            self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_pending_socks_handshake_is_counted_and_can_be_stopped(self):
        port = free_port()
        self.start("-D", "127.0.0.1:%d" % port)
        job = self.jobs()[0]["id"]
        conn = self.connect(port)
        rows = wait_for(self, lambda: self.connections(job), bool)
        self.assertEqual(rows[0]["state"], "handshake")
        self.assertIsNone(rows[0]["channel"])
        self.assertEqual(self.jobs()[0]["connections"], 1)
        stopped = run(self.name, "--job", str(job), "--connection", "1", "--stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(conn.recv(1), b"")
        self.active(job, 0)
        active = self.connect(port, dynamic=True)
        active.sendall(b"new")
        self.assertEqual(read_exact(active, 3), b"new")
        self.assertEqual(self.active(job, 1)[0]["id"], 2)

    def test_wire_selectors_reject_invalid_and_nonforward_jobs(self):
        self.start("-D", "127.0.0.1:%d" % free_port())
        job = self.jobs()[0]["id"]
        for selector in (None, True, 0, -1, "1", [], 9999):
            for op in ("connections", "interrupt_connection"):
                reply = self.request({"op": op, "job": selector, "connection": 1})
                self.assertIn("error", reply)
        for selector in (None, True, 0, -1, "1", [], 9999):
            reply = self.request({"op": "interrupt_connection",
                                  "job": job, "connection": selector})
            self.assertIn("error", reply)
        self.send("sleep 30", "--wait", "0.1")
        shell = next(row for row in self.jobs() if row["kind"] == "shell")
        self.assertNotIn("connections", shell)
        for op in ("connections", "interrupt_connection"):
            reply = self.request({"op": op, "job": shell["id"], "connection": 1})
            self.assertIn("not a forwarding job", reply["error"])
        self.assertEqual(run(self.name, "--interrupt").returncode, 0)
        self.assertEqual([row["id"] for row in self.jobs()], [job])

    def test_reconnect_removes_old_connections_without_reusing_ids(self):
        with TemporaryDirectory(prefix="mssh-connections-down-") as directory:
            down = os.path.join(directory, "down")
            port = free_port()
            with mock.patch.dict(ENV, MSSH_FAKE_DOWN=down):
                self.start("--reconnect", "10", "-R", "127.0.0.1:%d:127.0.0.1:%d" %
                           (port, self.server.server_address[1]))
                job = self.jobs()[0]["id"]
                conn = self.connect(port)
                self.active(job, 1)
                with open(down, "w"):
                    pass
                try:
                    self.assertEqual(self.send("echo disconnected").returncode, 1)
                    wait_for(self, lambda: self.request({"op": "status"}),
                             lambda info: info["link"] == "reconnecting")
                    self.assertEqual(self.connections(job), [])
                finally:
                    os.unlink(down)
                wait_for(self, lambda: self.request({"op": "status"}),
                         lambda info: info["link"] == "up")
                self.assertEqual(conn.recv(1), b"")
                conn = self.connect(port)
                conn.sendall(b"again")
                self.assertEqual(read_exact(conn, 5), b"again")
                self.assertEqual(self.active(job, 1)[0]["id"], 2)
                self.assertEqual(self.jobs()[0]["id"], job)


class TestConnectionOwnership(unittest.TestCase):
    def test_cancel_pending_open_closes_late_channel_without_stopping_job(self):
        session = mssh._Session(mock.Mock(), "marker", "shell", None, 0.1,
                                "target", None, transport=mock.Mock(), timeout=3)
        job = session.add_forwards(
            ["127.0.0.1:%d:remote:80" % free_port()], start=False)[0]
        self.addCleanup(job.abort)
        client, accepted = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(accepted.close)
        connection = mssh._ForwardConnection(job, accepted, ("127.0.0.1", 1234))
        with job.lock:
            job._register_connection(connection)
        entered, release = threading.Event(), threading.Event()
        channel = mock.Mock(closed=False)

        def open_channel(*args, **kwargs):
            entered.set()
            release.wait(5)
            return channel

        session.transport.open_channel.side_effect = open_channel
        worker = threading.Thread(target=job._connect, args=(connection,))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(session.connections_snapshot(job.id)[0]["state"], "connecting")
            session.interrupt_connection(job.id, connection.id)
            self.assertEqual(client.recv(1), b"")
            self.assertFalse(job.aborted)
            self.assertEqual(session.connections_snapshot(job.id), [])
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        channel.close.assert_called_once_with()
        channel.fileno.assert_not_called()
        self.assertEqual(session.jobs_count(), 1)

    def test_jobs_snapshot_releases_registry_lock_before_counting_connections(self):
        session = mssh._Session(mock.Mock(), "marker", "shell", None, 0.1,
                                "target", None, transport=mock.Mock())
        job = session.add_forwards(
            ["127.0.0.1:%d:remote:80" % free_port()], start=False)[0]
        self.addCleanup(job.abort)

        def count():
            acquired = session.execs_lock.acquire(False)
            self.assertTrue(acquired, "connection count holds the registry lock")
            if acquired:
                session.execs_lock.release()
            return 7

        with mock.patch.object(job, "connections_count", side_effect=count):
            self.assertEqual(session.jobs_snapshot()[0]["connections"], 7)

    def test_client_uses_distinct_operation_and_no_session_teardown(self):
        for control in ("--stop", "--interrupt"):
            with mock.patch.object(mssh, "session_alive", return_value=True), \
                    mock.patch.object(mssh, "session_request", return_value={
                        "interrupted": True}) as request, \
                    mock.patch.object(mssh, "session_path") as path, \
                    mock.patch.object(mssh.sys, "stderr", new_callable=io.StringIO):
                self.assertEqual(mssh.main(
                    ["s", "--job", "2", "--connection", "5", control]), 0)
            request.assert_called_once_with(
                "s", {"op": "interrupt_connection", "job": 2, "connection": 5}, timeout=30.0)
            path.assert_not_called()
        with mock.patch.object(mssh, "session_request", return_value={}):
            with self.assertRaisesRegex(SystemExit, "does not support --connections"):
                mssh.run_session_connections("s", 2)
            with self.assertRaisesRegex(SystemExit, "does not support --connection"):
                mssh.run_session_interrupt_connection("s", 2, 5)
        with mock.patch.object(mssh, "session_request", return_value={
                "error": "mssh: unknown request 'interrupt_connection'"}) as request, \
                mock.patch.object(mssh.sys, "stderr", new_callable=io.StringIO):
            self.assertEqual(mssh.run_session_interrupt_connection("s", 2, 5), 1)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args[0][1]["op"], "interrupt_connection")

    def test_queries_and_cancellation_skip_link_resolution(self):
        for op in ("connections", "interrupt_connection"):
            session = mock.Mock()
            session.chain = []
            session.connections_snapshot.return_value = []
            before = session.last
            reply = status_tests.TestStatusViews().dispatch(
                session, {"op": op, "job": 2, "connection": 5})
            if op == "connections":
                self.assertEqual(reply, {"job": 2, "connections": []})
                session.connections_snapshot.assert_called_once_with(2)
            else:
                self.assertEqual(reply, {"interrupted": True})
                session.interrupt_connection.assert_called_once_with(2, 5)
            session.resolve.assert_not_called()
            session.link_state.assert_not_called()
            self.assertIs(session.last, before)

    def test_concurrent_connection_interrupt_waits_for_resource_close(self):
        job = mock.Mock()
        connection = mssh._ForwardConnection(job)
        connection.chan = mock.Mock()
        entered, release, done = (threading.Event() for _ in range(3))

        def close(*args, **kwargs):
            entered.set()
            release.wait(5)

        def cancel_again():
            connection.abort()
            done.set()

        first = threading.Thread(target=connection.abort)
        second = threading.Thread(target=cancel_again)
        with mock.patch.object(mssh, "_close_session_channel", side_effect=close) as closer:
            first.start()
            try:
                self.assertTrue(entered.wait(3))
                # Transport.run must never wait for an ordinary closer which
                # can itself be waiting for Transport.run to finish rekeying.
                connection.abort(transport_thread=True)
                second.start()
                self.assertFalse(done.wait(0.1))
            finally:
                release.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
            closer.assert_called_once()
        self.assertTrue(done.is_set())
        self.assertTrue(connection.closed.is_set())
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())

    def test_cli_rejects_ambiguous_connection_selectors_before_any_request(self):
        cases = [
            ["--connections"], ["--connection", "1", "--stop"],
            ["--job", "1", "--connection", "0", "--stop"],
            ["--job", "1", "--connection", "2"],
            ["--job", "1", "--connections", "--stop"],
            ["--job", "1", "--connections", "--jobs"],
            ["--job", "1", "--connections", "--exec"],
            ["--job", "0", "--connections"],
            ["--job", "1", "--connections", "--sessions"],
            ["--job", "1", "--connections", "--prompt", ">"],
            ["--job", "1", "--connections", "-L", "8080:host:80"],
            ["--job", "1", "--connections", "--put", "a", "b"],
            ["--job", "1", "--connection", "2", "--stop", "-c", "true"],
            ["--job", "1", "--connection", "2", "--stop", "--interrupt"],
        ]
        for args in cases:
            with self.subTest(args=args), \
                    mock.patch.object(mssh, "session_request") as request, \
                    mock.patch.object(mssh.sys, "stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:
                    mssh.main(["--session", "s"] + args)
                self.assertEqual(raised.exception.code, 2)
                request.assert_not_called()

    def test_jobs_table_omits_connection_values_for_nonforwards(self):
        rows = [
            {"id": 1, "kind": "exec", "channel": 3, "state": "running",
             "started": time.time(), "command": "sleep 20"},
            {"id": 2, "kind": "forward", "channel": None, "state": "running",
             "started": time.time(), "command": "1080 -> SOCKS4/5", "connections": 4},
        ]
        with mock.patch.object(mssh, "session_request", return_value={"jobs": rows}), \
                mock.patch.object(mssh.sys, "stdout", new_callable=io.StringIO) as output:
            self.assertEqual(mssh.run_session_jobs("s"), 0)
        lines = output.getvalue().splitlines()
        count_column = lines[0].index("CONNS")
        self.assertEqual(lines[1][count_column:count_column + 6], "      ")
        self.assertEqual(lines[2][count_column:count_column + 6], "4     ")
        with mock.patch.object(mssh, "session_request", return_value={"jobs": rows[:1]}), \
                mock.patch.object(mssh.sys, "stdout", new_callable=io.StringIO) as output:
            mssh.run_session_jobs("s")
        self.assertNotIn("CONNS", output.getvalue())

    def test_connection_table_aligns_paths_and_sanitizes_terminal_controls(self):
        rows = [
            {"id": 1, "channel": None, "started": time.time(), "state": "connecting",
             "origin": "-", "listener": "/tmp/long listener.sock",
             "destination": "host:80"},
            {"id": 2, "channel": 7, "started": time.time(), "state": "active",
             "origin": "127.0.0.1:45678", "listener": "127.0.0.1:8080",
             "destination": "/tmp/service\n\x1b.sock"},
        ]
        with mock.patch.object(mssh, "session_request", return_value={"connections": rows}), \
                mock.patch.object(mssh.sys, "stdout", new_callable=io.StringIO) as output:
            self.assertEqual(mssh.run_session_connections("s", 2), 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertNotIn("\x1b", output.getvalue())
        self.assertEqual(lines[0].index("LISTENER"), lines[1].index("/tmp/long"))
        self.assertEqual(lines[0].index("DESTINATION"), lines[1].index("host:80"))
        self.assertEqual(lines[0].index("DESTINATION"), lines[2].index("/tmp/service"))


@unittest.skipUnless(os.getuid() == 0 and shutil.which("sshd"),
                     "OpenSSH fixture needs root and sshd")
class TestOpenSSHConnections(OpenSSHForwardTest):
    def test_channel_ids_and_targeted_close_on_real_transport(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                port = free_port()
                spec = "127.0.0.1:%d:%s" % (port, self.remote_socket)
                jobs = self.session.add_forwards(
                    [] if reverse else [spec], remote_specs=[spec] if reverse else [])
                job = jobs[0]
                first = socket.create_connection(("127.0.0.1", port), timeout=3)
                second = socket.create_connection(("127.0.0.1", port), timeout=3)
                try:
                    for conn in (first, second):
                        conn.sendall(b"echo")
                        self.assertEqual(read_exact(conn, 4), b"echo")
                    rows = wait_for(self, job.connections_snapshot,
                                    lambda rows: len(rows) == 2 and
                                    all(row["state"] == "active" for row in rows))
                    for row in rows:
                        self.assertIsNotNone(self.transport._channels.get(row["channel"]))
                    self.session.interrupt_connection(job.id, rows[0]["id"])
                    self.assertEqual(first.recv(1), b"")
                    second.sendall(b"alive")
                    self.assertEqual(read_exact(second, 5), b"alive")
                    self.assertEqual(job.connections_count(), 1)
                    self.assertTrue(self.transport.is_active())
                finally:
                    first.close()
                    second.close()
                    job.abort()


if __name__ == "__main__":
    unittest.main()
