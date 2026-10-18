"""Persistent local forwards through the real session daemon, Python 3.6+."""

import os
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import paramiko

from test_session_daemon import ENV, SessionTest, mssh, run


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            self.request.sendall(data)


class ReplyAfterEOF(socketserver.BaseRequestHandler):
    def handle(self):
        chunks = []
        while True:
            data = self.request.recv(65536)
            if not data:
                break
            chunks.append(data)
        self.request.sendall(b"reply:" + b"".join(chunks))


class EOFBeforeInput(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.sendall(b"greeting")
        self.request.shutdown(socket.SHUT_WR)
        data = bytearray()
        while True:
            chunk = self.request.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
        self.server.received = bytes(data)
        self.server.done.set()


class TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class TestForwardParsing(unittest.TestCase):
    def make_session(self):
        return mssh._Session(mock.Mock(), "marker", "shell", None, 0.1,
                             "target", None, transport=mock.Mock(), timeout=5)

    def test_supported_addresses(self):
        cases = [
            ("8080:localhost:80", "localhost", 8080, "localhost", 80),
            ("127.0.0.1:8080:remote:80", "127.0.0.1", 8080, "remote", 80),
            ("*:8080:remote:80", "*", 8080, "remote", 80),
            (":8080:remote:80", "", 8080, "remote", 80),
            ("[::1]:8080:[::1]:80", "::1", 8080, "::1", 80),
            ("8080:[2001:db8::1]:80", "localhost", 8080, "2001:db8::1", 80),
        ]
        for spec, bind, port, host, hostport in cases:
            self.assertEqual(mssh.parse_local_forward(spec),
                             {"bind": bind, "port": port,
                              "host": host, "hostport": hostport})

    def test_invalid_specifications(self):
        for spec in ("", "80:host", "0:host:80", "80:host:0",
                     "65536:host:80", "80:host:65536", "80:*:80",
                     "8080:bad host:80", "[bad]:8080:host:80",
                     "::1:8080:host:80", "/tmp/socket:host", None):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    mssh.parse_local_forward(spec)

    def test_cancel_while_direct_channel_is_opening(self):
        transport = mock.Mock()
        transport.is_active.return_value = True
        entered, release = threading.Event(), threading.Event()
        remote = mock.Mock(closed=False)

        def open_channel(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("channel open was not released")
            return remote

        transport.open_channel.side_effect = open_channel
        session = mssh._Session(mock.Mock(), "marker", "shell", None, 0.1,
                                "target", None, transport=transport, timeout=5)
        client, accepted = socket.socketpair()
        job = None
        worker = None
        try:
            job = session.add_forwards(
                ["127.0.0.1:%d:service.internal:80" % free_port()], start=False)[0]
            with job.lock:
                job.connections[accepted] = None
            worker = threading.Thread(
                target=job._connect, args=(accepted, ("127.0.0.1", 1234)))
            worker.start()
            self.assertTrue(entered.wait(3))
            job.abort()
            self.assertEqual(client.recv(1), b"")
            release.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            remote.close.assert_called_once_with()
            self.assertEqual(session.jobs_snapshot(), [])
        finally:
            release.set()
            if worker is not None:
                worker.join(5)
            if job is not None:
                job.abort()
            client.close()
            accepted.close()

    def test_cancel_before_start_never_starts_acceptor(self):
        session = self.make_session()
        job = session.add_forwards(
            ["127.0.0.1:%d:service.internal:80" % free_port()], start=False)[0]
        job.abort()
        job.start()
        self.assertIsNone(job.worker)
        self.assertTrue(job.closed.is_set())
        self.assertEqual(session.jobs_snapshot(), [])

    def test_concurrent_interrupt_waits_for_resource_close(self):
        session = self.make_session()
        job = session.add_forwards(
            ["127.0.0.1:%d:service.internal:80" % free_port()], start=False)[0]
        client, accepted = socket.socketpair()
        channel = mock.Mock()
        entered, release, finished = (threading.Event() for _ in range(3))
        workers = []

        def close(_channel):
            entered.set()
            release.wait(5)

        def interrupt_again():
            job.abort()
            finished.set()

        try:
            with job.lock:
                job.connections[accepted] = channel
            with mock.patch.object(mssh, "_close_session_channel", side_effect=close):
                workers.append(threading.Thread(target=job.abort))
                workers[0].start()
                self.assertTrue(entered.wait(3))
                workers.append(threading.Thread(target=interrupt_again))
                workers[1].start()
                self.assertFalse(finished.wait(0.1))
                release.set()
                for worker in workers:
                    worker.join(3)
                self.assertTrue(finished.is_set())
                self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(client.recv(1), b"")
            self.assertEqual(session.jobs_snapshot(), [])
        finally:
            release.set()
            for worker in workers:
                worker.join(5)
            job.abort()
            accepted.close()
            client.close()

    def test_channel_pipe_is_created_before_publishing_for_abort(self):
        session = self.make_session()
        job = session.add_forwards(
            ["127.0.0.1:%d:service.internal:80" % free_port()], start=False)[0]
        client, accepted = socket.socketpair()
        channel = mock.Mock()
        session.transport.open_channel.return_value = channel

        def fileno():
            self.assertIsNone(job.connections[accepted])
            return 10

        def relay(conn, chan, transport):
            channel.fileno.assert_called_once_with()
            self.assertIs(job.connections[conn], channel)

        channel.fileno.side_effect = fileno
        try:
            job.connections[accepted] = None
            with mock.patch.object(job, "_relay", side_effect=relay):
                job._connect(accepted, ("127.0.0.1", 1234))
            self.assertEqual(job.connections, {})
            channel.close.assert_called_once_with()
        finally:
            job.abort()
            accepted.close()
            client.close()

    def test_acceptor_start_failure_rolls_back_whole_batch(self):
        session = self.make_session()
        ports = [free_port(), free_port()]
        with mock.patch.object(threading.Thread, "start",
                               side_effect=RuntimeError("cannot start thread")):
            with self.assertRaisesRegex(RuntimeError, "cannot start thread"):
                session.add_forwards(
                    ["127.0.0.1:%d:service.internal:80" % port for port in ports])
        self.assertEqual(session.jobs_snapshot(), [])
        for port in ports:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))


class TestSessionForward(SessionTest):
    def setUp(self):
        super(TestSessionForward, self).setUp()
        self.server = TCPServer(("127.0.0.1", 0), Echo)
        self.remote_port = self.server.server_address[1]
        self.server.received = None
        self.server.done = threading.Event()
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.daemon = True
        self.worker.start()

    def tearDown(self):
        super(TestSessionForward, self).tearDown()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(3)

    def spec(self, port):
        return "127.0.0.1:%d:127.0.0.1:%d" % (port, self.remote_port)

    def jobs(self):
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            return mssh.session_request(self.name, {"op": "jobs"}, timeout=3)["jobs"]

    def exchange(self, port, data=b"forwarded"):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            conn.sendall(data)
            conn.shutdown(socket.SHUT_WR)
            result = bytearray()
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return bytes(result)
                result.extend(chunk)

    def test_startup_forward_survives_client_and_is_a_job(self):
        port = free_port()
        proc = self.start("-L", self.spec(port))
        self.assertIn(b"forwarding job", proc.stderr)
        self.assertEqual(self.exchange(port), b"forwarded")
        rows = self.jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "forward")
        self.assertIn(str(port), rows[0]["command"])
        self.assertIsNone(rows[0]["channel"])
        proc = run("--sessions")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = next(line.split() for line in proc.stdout.decode().splitlines()[1:]
                   if line.split()[0] == self.name)
        self.assertEqual(row[-1], "1")
        self.assertEqual(run(self.name, "--interrupt").returncode, 0)
        self.assertEqual(self.exchange(port), b"forwarded")
        self.assertIn(b"forward", run(self.name, "--jobs").stdout)
        proc = run(self.name, "--job", str(rows[0]["id"]), "--stop")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.jobs(), [])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_client_add_multiple_and_cancel_one(self):
        self.start()
        first, second = free_port(), free_port()
        proc = run(self.name, "-L", self.spec(first), "-L", self.spec(second))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr.count(b"forwarding job"), 2)
        self.assertEqual(self.exchange(first), b"forwarded")
        self.assertEqual(self.exchange(second), b"forwarded")
        rows = self.jobs()
        self.assertEqual(len(rows), 2)
        proc = run(self.name, "--interrupt", "--job", str(rows[0]["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.exchange(second), b"forwarded")
        self.assertEqual(len(self.jobs()), 1)

    def test_explicit_session_and_attached_option_syntax(self):
        self.start()
        port = free_port()
        proc = run("--session", self.name, "-L" + self.spec(port))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.exchange(port), b"forwarded")

    def test_wire_validation_and_older_daemon_error(self):
        self.start()
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            for specs in (None, [], "8080:host:80", [1], ["invalid"]):
                reply = mssh.session_request(
                    self.name, {"op": "forward", "specs": specs}, timeout=3)
                self.assertIn("error", reply)
            self.assertEqual(self.jobs(), [])
        with mock.patch.object(mssh, "session_request", return_value={}):
            with self.assertRaisesRegex(SystemExit, "does not support -L"):
                mssh.run_session_forward(self.name, ["8080:host:80"])

    def test_large_binary_parallel_connections(self):
        self.start()
        port = free_port()
        self.assertEqual(run(self.name, "-L", self.spec(port)).returncode, 0)
        data = bytes(range(256)) * 16384
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.exchange(port, data), range(4)))
        self.assertEqual(results, [data] * 4)
        self.assertEqual(len(self.jobs()), 1)

    def test_loopback_default_and_ipv6(self):
        self.start()
        port = free_port()
        proc = run(self.name, "-L", "%d:127.0.0.1:%d" % (port, self.remote_port))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.exchange(port), b"forwarded")
        with socket.socket(socket.AF_INET6) as listener:
            try:
                listener.bind(("::1", 0))
            except OSError:
                self.skipTest("IPv6 loopback unavailable")
            ipv6_port = listener.getsockname()[1]
        proc = run(self.name, "-L",
                   "[::1]:%d:127.0.0.1:%d" % (ipv6_port, self.remote_port))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with socket.create_connection(("::1", ipv6_port), timeout=5) as conn:
            conn.sendall(b"ipv6")
            self.assertEqual(conn.recv(4), b"ipv6")

    def test_reconnect_keeps_listener_and_job_identity(self):
        with tempfile.TemporaryDirectory(prefix="mssh-forward-reconnect-") as tmp:
            down = os.path.join(tmp, "down")
            with mock.patch.dict(ENV, MSSH_FAKE_DOWN=down):
                port = free_port()
                self.start("-L", self.spec(port), "--reconnect", "10")
                original = self.jobs()[0]["id"]
                self.assertEqual(self.exchange(port), b"forwarded")
                with open(down, "w"):
                    pass
                proc = self.send("echo disconnected")
                self.assertEqual(proc.returncode, 1, proc.stderr)
                with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        info = mssh.session_request(
                            self.name, {"op": "status"}, timeout=3)
                        if info["link"] != "up":
                            break
                        time.sleep(0.05)
                    self.assertNotEqual(info["link"], "up")
                    self.assertEqual([row["id"] for row in self.jobs()], [original])
                    # A listener can be added while disconnected, and then
                    # stopped without resolve() waiting for an SSH reconnect.
                    added = free_port()
                    proc = run(self.name, "-L", self.spec(added))
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    extra = next(row for row in self.jobs()
                                 if row["kind"] == "forward" and
                                 row["id"] != original)
                    proc = run(self.name, "--job", str(extra["id"]), "--stop")
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    os.unlink(down)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        info = mssh.session_request(
                            self.name, {"op": "status"}, timeout=3)
                        if info["link"] == "up":
                            break
                        time.sleep(0.05)
                    self.assertEqual(info["link"], "up")
                self.assertEqual([row["id"] for row in self.jobs()], [original])
                self.assertEqual(self.exchange(port), b"forwarded")

    def test_closed_connections_do_not_leak_daemon_descriptors(self):
        self.start()
        port = free_port()
        proc = run(self.name, "-L", self.spec(port))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            info = mssh.session_request(self.name, {"op": "status"}, timeout=3)
        fd_dir = "/proc/%s/fd" % info["pid"]
        baseline = len(os.listdir(fd_dir))
        for _ in range(20):
            self.assertEqual(self.exchange(port), b"forwarded")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if len(os.listdir(fd_dir)) <= baseline:
                break
            time.sleep(0.05)
        self.assertLessEqual(len(os.listdir(fd_dir)), baseline)

    def test_idle_expiry_releases_listener(self):
        port = free_port()
        self.start("-L", self.spec(port), "--session-idle", "0.3")
        deadline = time.monotonic() + 5
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            while time.monotonic() < deadline:
                if not os.path.exists(mssh.session_path(self.name)):
                    break
                time.sleep(0.05)
            self.assertFalse(os.path.exists(mssh.session_path(self.name)))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))

    def test_interactive_session_supports_forward(self):
        self.start("-c", "cat", "--idle", "0.1")
        port = free_port()
        proc = run(self.name, "-L", self.spec(port))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.exchange(port), b"forwarded")
        self.assertEqual(self.send("still alive").stdout, b"still alive\n")

    def test_local_half_close_keeps_remote_response(self):
        self.server.RequestHandlerClass = ReplyAfterEOF
        self.start()
        port = free_port()
        self.assertEqual(run(self.name, "-L", self.spec(port)).returncode, 0)
        self.assertEqual(self.exchange(port, b"request"), b"reply:request")

    def test_remote_half_close_keeps_local_input(self):
        self.server.RequestHandlerClass = EOFBeforeInput
        self.start()
        port = free_port()
        self.assertEqual(run(self.name, "-L", self.spec(port)).returncode, 0)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            received = bytearray()
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                received.extend(data)
            self.assertEqual(received, b"greeting")
            conn.sendall(b"after-remote-eof")
            conn.shutdown(socket.SHUT_WR)
        self.assertTrue(self.server.done.wait(3))
        self.assertEqual(self.server.received, b"after-remote-eof")

    def test_cancel_and_stop_close_existing_connections(self):
        self.start()
        for cancel in ("interrupt", "stop_job", "stop_session"):
            port = free_port()
            proc = run(self.name, "-L", self.spec(port))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
                conn.sendall(b"open")
                self.assertEqual(conn.recv(4), b"open")
                rows = self.jobs()
                options = ["--stop"] if cancel == "stop_session" else [
                    "--job", str(rows[0]["id"]),
                    "--interrupt" if cancel == "interrupt" else "--stop"]
                proc = run(self.name, *options)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(conn.recv(1), b"")
                if cancel != "stop_session":
                    self.assertEqual(self.send("echo alive").stdout, b"alive\n")
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))

    def test_failed_batch_rolls_back_and_keeps_existing_job(self):
        self.start()
        existing, added = free_port(), free_port()
        self.assertEqual(run(self.name, "-L", self.spec(existing)).returncode, 0)
        proc = run(self.name, "-L", self.spec(added), "-L", self.spec(existing))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(self.jobs()), 1)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", added))
        self.assertEqual(self.exchange(existing), b"forwarded")

    def test_startup_bind_failure_is_reported_without_session(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            proc = run("--session", self.name, "root@10.0.0.1",
                       "-L", self.spec(busy.getsockname()[1]))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(run(self.name, "--status").returncode, 2)

    def test_refused_destination_does_not_remove_listener(self):
        self.start()
        port = free_port()
        proc = run(self.name, "-L",
                   "127.0.0.1:%d:127.0.0.1:%d" % (port, free_port()))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            self.assertEqual(conn.recv(1), b"")
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_wildcard_bind_is_explicit(self):
        self.start()
        for bind in ("*", ""):
            port = free_port()
            proc = run(self.name, "-L", "%s:%d:127.0.0.1:%d" %
                       (bind, port, self.remote_port))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(self.exchange(port), b"forwarded")
        self.assertEqual(len(self.jobs()), 2)

    def test_startup_failed_batch_releases_all_listeners(self):
        added = free_port()
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            proc = run("--session", self.name, "root@10.0.0.1",
                       "-L", self.spec(added),
                       "-L", self.spec(busy.getsockname()[1]))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", added))
        self.assertEqual(run(self.name, "--status").returncode, 2)

    def test_warning_log_records_forward_failure_with_job_and_traceback(self):
        self.start("-v")
        port = free_port()
        proc = run(self.name, "-L",
                   "127.0.0.1:%d:127.0.0.1:%d" % (port, free_port()))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job_id = self.jobs()[0]["id"]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            self.assertEqual(conn.recv(1), b"")
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")
        path = os.path.join(ENV["HOME"], ".mssh", "sessions", self.name + ".log")
        with open(path, "rb") as handle:
            text = handle.read()
        self.assertIn(b"forward.connection_failed", text)
        self.assertIn(("job=%d" % job_id).encode(), text)
        self.assertIn(b"Traceback", text)
        self.assertNotIn(b"forward.connected", text)

    def test_reject_conflicting_and_non_session_options(self):
        self.start()
        spec = self.spec(free_port())
        for options in (["--jobs"], ["--status"], ["--interrupt"],
                        ["--stop"], ["--exec"], ["-t"],
                        ["--put", "a", "b"], ["echo", "hi"]):
            self.assertEqual(run(self.name, "-L", spec, *options).returncode, 2)
        self.assertEqual(run("--sessions", "-L", spec).returncode, 2)
        self.assertEqual(run("root@10.0.0.1", "-L", spec).returncode, 2)
        self.assertEqual(run("--session", "no-such-session", "-L", spec).returncode, 1)


class TestForwardSSH(unittest.TestCase):
    def test_real_direct_tcpip_channel_and_half_close(self):
        requested = []

        class Server(paramiko.ServerInterface):
            def check_auth_none(self, username):
                return paramiko.AUTH_SUCCESSFUL

            def check_channel_direct_tcpip_request(self, chanid, origin, destination):
                requested.append((origin, destination))
                return paramiko.OPEN_SUCCEEDED

        left, right = socket.socketpair()
        client = paramiko.Transport(left)
        remote = paramiko.Transport(right)
        session = mssh._Session(mock.Mock(), "marker", "shell", None, 0.1,
                                "target", None, transport=client, timeout=5)
        errors = []
        server_worker = None
        try:
            ready = threading.Event()
            remote.add_server_key(paramiko.RSAKey.generate(1024))
            remote.start_server(event=ready, server=Server())
            client.start_client(timeout=5)
            client.auth_none("test")
            self.assertTrue(ready.wait(5))
            port = free_port()
            jobs = session.add_forwards(["127.0.0.1:%d:service.internal:1234" % port])

            def reply():
                chan = None
                try:
                    chan = remote.accept(5)
                    if chan is None:
                        raise RuntimeError("missing direct-tcpip channel")
                    data = bytearray()
                    while True:
                        chunk = chan.recv(65536)
                        if not chunk:
                            break
                        data.extend(chunk)
                    chan.sendall(b"reply:" + bytes(data))
                    chan.shutdown_write()
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if chan is not None:
                        chan.close()

            server_worker = threading.Thread(target=reply)
            server_worker.start()
            with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
                payload = b"\x00binary\xff" * 20000
                conn.sendall(payload)
                conn.shutdown(socket.SHUT_WR)
                data = bytearray()
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data.extend(chunk)
            self.assertEqual(data, b"reply:" + payload)
            self.assertEqual(requested[0][1], ("service.internal", 1234))
            self.assertEqual(requested[0][0][0], "127.0.0.1")
            session.interrupt_job(jobs[0].id)
            self.assertTrue(client.is_active())
            self.assertEqual(session.jobs_snapshot(), [])
            server_worker.join(5)
            self.assertFalse(server_worker.is_alive())
            self.assertEqual(errors, [])
        finally:
            session.close_channels()
            client.close()
            remote.close()
            if server_worker is not None:
                server_worker.join(5)
