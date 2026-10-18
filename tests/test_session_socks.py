"""Persistent SOCKS4/4a and SOCKS5 proxies over session forward jobs."""

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import paramiko

from test_session_daemon import ENV, SessionTest, mssh, run
from test_session_forward import (
    Echo, EOFBeforeInput, ReplyAfterEOF, TCPServer, free_port,
)
import test_session_unix_forward as unix_tests


def read_exact(conn, size):
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise EOFError("unexpected proxy EOF")
        data.extend(chunk)
    return bytes(data)


def request_packet(host, port, version=5, domain=False):
    if version == 4:
        address = b"\x00\x00\x00\x01" if domain else socket.inet_aton(host)
        return b"\x04\x01" + port.to_bytes(2, "big") + address + b"user\x00" + (
            host.encode("ascii") + b"\x00" if domain else b"")
    if domain:
        encoded = host.encode("ascii")
        address = b"\x03" + bytes((len(encoded),)) + encoded
    else:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        address = bytes((4 if family == socket.AF_INET6 else 1,)) + \
            socket.inet_pton(family, host)
    return b"\x05\x01\x00" + address + port.to_bytes(2, "big")


def connect_proxy(port, host, destination_port, version=5, domain=False,
                  pipeline=b"", fragmented=False, bind="127.0.0.1"):
    conn = socket.create_connection((bind, port), timeout=5)
    try:
        packet = request_packet(host, destination_port, version, domain)
        if version == 5:
            packet = b"\x05\x02\x02\x00" + packet
        packet += pipeline
        if fragmented:
            for byte in packet:
                conn.sendall(bytes((byte,)))
        else:
            conn.sendall(packet)
        if version == 5:
            if read_exact(conn, 2) != b"\x05\x00":
                raise AssertionError("wrong method selection")
            if read_exact(conn, 10) != b"\x05\x00\x00\x01" + b"\x00" * 6:
                raise AssertionError("SOCKS5 CONNECT failed")
        elif read_exact(conn, 8) != b"\x00\x5a" + b"\x00" * 6:
            raise AssertionError("SOCKS4 CONNECT failed")
        return conn
    except BaseException:
        conn.close()
        raise


def exchange_proxy(port, host, destination_port, data=b"forwarded",
                   version=5, domain=False):
    with connect_proxy(port, host, destination_port, version, domain) as conn:
        conn.sendall(data)
        conn.shutdown(socket.SHUT_WR)
        result = bytearray()
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return bytes(result)
            result.extend(chunk)


class TestDynamicParsing(unittest.TestCase):
    def test_supported_binds(self):
        for spec, bind in (("1080", "localhost"), ("127.0.0.1:1080", "127.0.0.1"),
                           ("[::1]:1080", "::1"), ("*:1080", "*"), (":1080", "")):
            with self.subTest(spec=spec):
                self.assertEqual(mssh.parse_dynamic_forward(spec),
                                 {"bind": bind, "port": 1080, "dynamic": True})

    def test_invalid_specs(self):
        for spec in ("", None, 1080, "0", "65536", "-1", "1080\n",
                     "host:80:90", "::1:1080", "[bad]:1080",
                     "/tmp/socket:1080", "[::1]suffix:1080", "bad host:1080"):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    mssh.parse_dynamic_forward(spec)

    def test_client_uses_distinct_wire_op_for_old_daemons(self):
        with mock.patch.object(mssh, "session_request", return_value={
                "ok": True, "forwards": []}) as request:
            self.assertEqual(mssh.run_session_forward("hop", [], ["1080"]), 0)
        self.assertEqual(request.call_args[0][1], {
            "op": "dynamic_forward", "specs": [], "dynamic_specs": ["1080"]})
        with mock.patch.object(mssh, "session_request", return_value={}):
            with self.assertRaisesRegex(SystemExit, "does not support -D"):
                mssh.run_session_forward("hop", [], ["1080"])
        with mock.patch.object(mssh, "session_request", return_value={
                "error": "unknown request 'dynamic_forward'"}):
            self.assertEqual(mssh.run_session_forward("hop", [], ["1080"]), 1)


class TestSocksChannelOwnership(unittest.TestCase):
    def test_remote_dns_and_cancel_during_channel_open(self):
        session = mssh._Session(mock.Mock(), "marker", "shell", None, 0.1,
                                "target", None, transport=mock.Mock(), timeout=3)
        entered, release = threading.Event(), threading.Event()
        channel = mock.Mock()
        destination = []

        def open_channel(kind, target, origin, timeout=None):
            destination.append((kind, target, origin))
            entered.set()
            release.wait(5)
            return channel

        session.transport.open_channel.side_effect = open_channel
        job = session.add_forwards([], start=False, dynamic_specs=[
            "127.0.0.1:%d" % free_port()])[0]
        self.addCleanup(job.abort)
        client, accepted = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(accepted.close)
        client.settimeout(3)
        job.connections[accepted] = None
        worker = threading.Thread(target=job._connect,
                                  args=(accepted, ("127.0.0.1", 1234)))
        worker.start()
        try:
            with mock.patch.object(mssh.socket, "getaddrinfo",
                                   side_effect=AssertionError("local DNS lookup")):
                client.sendall(b"\x05\x01\x00" +
                               request_packet("only.remote.internal", 8080, domain=True))
                self.assertEqual(read_exact(client, 2), b"\x05\x00")
                self.assertTrue(entered.wait(3))
                self.assertEqual(destination, [
                    ("direct-tcpip", ("only.remote.internal", 8080), ("127.0.0.1", 1234))])
                # No CONNECT success before the SSH server has accepted it.
                client.settimeout(0.1)
                with self.assertRaises(socket.timeout):
                    client.recv(1)
                job.abort()
                self.assertEqual(client.recv(1), b"")
                release.set()
                worker.join(3)
                self.assertFalse(worker.is_alive())
                channel.close.assert_called_once_with()
                self.assertEqual(session.jobs_snapshot(), [])
        finally:
            release.set()
            worker.join(5)

    def test_channel_denial_maps_to_socks_failure(self):
        for version in (4, 5):
            session = mssh._Session(mock.Mock(), "marker", "shell", None, 0.1,
                                    "target", None, transport=mock.Mock(), timeout=3)
            session.transport.open_channel.side_effect = paramiko.ChannelException(
                paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED, "denied")
            job = session.add_forwards([], start=False, dynamic_specs=[
                "127.0.0.1:%d" % free_port()])[0]
            client, accepted = socket.socketpair()
            client.settimeout(3)
            job.connections[accepted] = None
            try:
                client.sendall((b"\x05\x01\x00" if version == 5 else b"") +
                               request_packet("127.0.0.1", 8080, version))
                job._connect(accepted, ("127.0.0.1", 1234))
                if version == 5:
                    self.assertEqual(read_exact(client, 2), b"\x05\x00")
                    self.assertEqual(read_exact(client, 10),
                                     b"\x05\x02\x00\x01" + b"\x00" * 6)
                else:
                    self.assertEqual(read_exact(client, 8), b"\x00\x5b" + b"\x00" * 6)
                self.assertEqual(client.recv(1), b"")
            finally:
                job.abort()
                client.close()
                accepted.close()

    def test_handshake_total_deadline_not_reset_by_reads(self):
        client, accepted = socket.socketpair()
        try:
            client.sendall(b"\x05")
            request = mssh._SocksRequest(accepted, 3)
            request.deadline = time.monotonic() + 0.05
            with self.assertRaises(socket.timeout):
                request.destination()
        finally:
            client.close()
            accepted.close()


class TestSessionSocks(SessionTest):
    def setUp(self):
        super(TestSessionSocks, self).setUp()
        self.server = TCPServer(("127.0.0.1", 0), Echo)
        self.remote_port = self.server.server_address[1]
        self.server.received = None
        self.server.done = threading.Event()
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.daemon = True
        self.worker.start()
        self.port = free_port()
        self.spec = "127.0.0.1:%d" % self.port

    def tearDown(self):
        try:
            super(TestSessionSocks, self).tearDown()
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.worker.join(3)

    def jobs(self):
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            return mssh.session_request(self.name, {"op": "jobs"}, timeout=3)["jobs"]

    def add(self):
        proc = run(self.name, "-D", self.spec)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.jobs()[-1]["id"]

    def exchange(self, version=5, domain=False, data=b"forwarded"):
        return exchange_proxy(self.port, "localhost" if domain else "127.0.0.1",
                              self.remote_port, data, version, domain)

    def test_startup_proxy_survives_client_and_job_stop(self):
        proc = self.start("-D", self.spec)
        self.assertIn(b"SOCKS4/5", proc.stderr)
        self.assertEqual(self.exchange(), b"forwarded")
        rows = self.jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "forward")
        self.assertIsNone(rows[0]["channel"])
        self.assertIn("SOCKS4/5", rows[0]["command"])
        self.assertEqual(run(self.name, "--interrupt").returncode, 0)
        self.assertEqual(self.exchange(), b"forwarded")
        proc = run(self.name, "--stop", "--job", str(rows[0]["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.jobs(), [])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=1)
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_add_after_start_all_protocols_and_attached_syntax(self):
        self.start()
        proc = run("--session", self.name, "-D" + self.spec)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for version, domain in ((4, False), (4, True), (5, False), (5, True)):
            with self.subTest(version=version, domain=domain):
                self.assertEqual(self.exchange(version, domain), b"forwarded")
        self.assertEqual(len(self.jobs()), 1)

    def test_mixed_batch_rolls_back_and_preserves_existing_jobs(self):
        self.start("-D", self.spec)
        extra = free_port()
        with tempfile.TemporaryDirectory(prefix="mssh-socks-") as directory:
            path = os.path.join(directory, "local.sock")
            proc = run(self.name, "-L", "%s:127.0.0.1:%d" % (path, self.remote_port),
                       "-D", "127.0.0.1:%d" % extra, "-D", self.spec)
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(os.path.lexists(path))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", extra))
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.exchange(), b"forwarded")

    def test_startup_mixed_batch_and_targeted_cancel(self):
        local = free_port()
        self.start("-L", "127.0.0.1:%d:127.0.0.1:%d" % (local, self.remote_port),
                   "-D", self.spec)
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(self.exchange(), b"forwarded")
        proxy = next(row for row in self.jobs() if "SOCKS" in row["command"])
        self.assertEqual(run(self.name, "--interrupt", "--job",
                             str(proxy["id"])).returncode, 0)
        with socket.create_connection(("127.0.0.1", local), timeout=3) as conn:
            conn.sendall(b"local")
            self.assertEqual(conn.recv(5), b"local")
        self.assertEqual(len(self.jobs()), 1)

    def test_multiple_proxies_and_per_request_destinations(self):
        class SecondService(Echo):
            def handle(handler):
                data = handler.request.recv(65536)
                if data:
                    handler.request.sendall(b"second:" + data)

        second = TCPServer(("127.0.0.1", 0), SecondService)
        worker = threading.Thread(target=second.serve_forever)
        worker.daemon = True
        worker.start()
        try:
            self.start()
            extra = free_port()
            proc = run(self.name, "-D", self.spec, "-D", "127.0.0.1:%d" % extra)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for proxy in (self.port, extra):
                self.assertEqual(exchange_proxy(
                    proxy, "127.0.0.1", self.remote_port), b"forwarded")
                self.assertEqual(exchange_proxy(
                    proxy, "127.0.0.1", second.server_address[1]),
                    b"second:forwarded")
            self.assertEqual(len(self.jobs()), 2)
        finally:
            second.shutdown()
            second.server_close()
            worker.join(3)

    def test_explicit_wildcard_and_empty_binds(self):
        self.start()
        extra = free_port()
        proc = run(self.name, "-D", "*:%d" % self.port, "-D", ":%d" % extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for proxy in (self.port, extra):
            self.assertEqual(exchange_proxy(
                proxy, "127.0.0.1", self.remote_port), b"forwarded")

    def test_startup_bind_failure_releases_all_listeners(self):
        extra = free_port()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", self.port))
            occupied.listen()
            proc = run("--session", self.name, "root@10.0.0.1",
                       "-D", "127.0.0.1:%d" % extra, "-D", self.spec)
            self.assertEqual(proc.returncode, 1)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", extra))
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            self.assertFalse(mssh.session_alive(self.name))

    def test_fragmented_handshake_and_pipelined_payload(self):
        self.start("-D", self.spec)
        for version in (4, 5):
            with connect_proxy(self.port, "127.0.0.1", self.remote_port, version,
                               pipeline=b"already sent", fragmented=True) as conn:
                self.assertEqual(read_exact(conn, 12), b"already sent")

    def test_large_parallel_binary_and_local_half_close(self):
        self.server.RequestHandlerClass = ReplyAfterEOF
        self.start("-D", self.spec)
        payload = bytes(range(256)) * 8192
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda _: self.exchange(data=payload), range(4)))
        self.assertEqual(results, [b"reply:" + payload] * 4)
        self.assertEqual(len(self.jobs()), 1)

    def test_remote_half_close_preserves_later_local_input(self):
        self.server.RequestHandlerClass = EOFBeforeInput
        self.start("-D", self.spec)
        with connect_proxy(self.port, "127.0.0.1", self.remote_port) as conn:
            self.assertEqual(read_exact(conn, 8), b"greeting")
            self.assertEqual(conn.recv(1), b"")
            conn.sendall(b"after-eof")
            conn.shutdown(socket.SHUT_WR)
        self.assertTrue(self.server.done.wait(3))
        self.assertEqual(self.server.received, b"after-eof")

    def test_authentication_methods_rejected_without_channel(self):
        self.start("-D", self.spec)
        for packet in (b"\x05\x01\x02", b"\x05\x00"):
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
                conn.sendall(packet)
                self.assertEqual(read_exact(conn, 2), b"\x05\xff")
                self.assertEqual(conn.recv(1), b"")
        self.assertEqual(self.exchange(), b"forwarded")

    def test_bad_requests_have_failure_replies_and_keep_session_alive(self):
        self.start("-D", self.spec)
        packets = [
            (b"\x05\x02\x00\x01", 7),  # BIND
            (b"\x05\x03\x00\x01", 7),  # UDP ASSOCIATE
            (b"\x05\x01\x00\x02", 8),  # Unknown address type
            (b"\x05\x01\x01\x01", 1),  # Invalid reserved byte
            (b"\x04\x01\x00\x01", 1),  # Wrong request version
            (b"\x05\x01\x00\x03\x00", 4),  # Empty name
            (b"\x05\x01\x00\x03\x03a b", 4),
            (request_packet("127.0.0.1", 0), 1),
        ]
        for packet, code in packets:
            with self.subTest(packet=packet):
                with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
                    conn.sendall(b"\x05\x01\x00" + packet)
                    self.assertEqual(read_exact(conn, 2), b"\x05\x00")
                    self.assertEqual(read_exact(conn, 10),
                                     bytes((5, code, 0, 1)) + b"\x00" * 6)
                    self.assertEqual(conn.recv(1), b"")
        for packet in (b"\x04\x02", b"\x04\x01\x00\x50\x7f\x00\x00\x01" +
                       b"x" * 1025, request_packet("127.0.0.1", 0, 4),
                       b"\x04\x01\x00\x50\x00\x00\x00\x01\x00" + b"x" * 256):
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
                conn.sendall(packet)
                self.assertEqual(read_exact(conn, 8), b"\x00\x5b" + b"\x00" * 6)
        self.assertEqual(self.exchange(), b"forwarded")
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_unknown_versions_and_invalid_domain_encoding(self):
        self.start("-D", self.spec)
        for version in (0, 3, 6, 255):
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
                conn.sendall(bytes((version,)))
                self.assertEqual(conn.recv(1), b"")
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
            conn.sendall(b"\x05\x01\x00\x05\x01\x00\x03\x01\xff")
            self.assertEqual(read_exact(conn, 2), b"\x05\x00")
            self.assertEqual(read_exact(conn, 10), b"\x05\x01\x00\x01" + b"\x00" * 6)
        self.assertEqual(self.exchange(), b"forwarded")

    def test_warning_log_has_job_and_traceback_without_userid(self):
        self.start("-D", self.spec, "-v")
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
            packet = request_packet("127.0.0.1", free_port(), 4)
            conn.sendall(packet.replace(b"user\x00", b"private-userid\x00"))
            self.assertEqual(read_exact(conn, 8), b"\x00\x5b" + b"\x00" * 6)
            self.assertEqual(conn.recv(1), b"")
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")
        path = os.path.join(ENV["HOME"], ".mssh", "sessions", self.name + ".log")
        with open(path, "rb") as handle:
            text = handle.read()
        self.assertIn(b"forward.connection_failed", text)
        self.assertIn(("job=%d" % self.jobs()[0]["id"]).encode(), text)
        self.assertIn(b"Traceback", text)
        self.assertNotIn(b"private-userid", text)

    def test_refused_destination_returns_failure_keeps_listener(self):
        self.start("-D", self.spec)
        for version in (4, 5):
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
                conn.sendall((b"\x05\x01\x00" if version == 5 else b"") +
                             request_packet("127.0.0.1", free_port(), version))
                if version == 5:
                    self.assertEqual(read_exact(conn, 2), b"\x05\x00")
                    self.assertEqual(read_exact(conn, 10),
                                     b"\x05\x01\x00\x01" + b"\x00" * 6)
                else:
                    self.assertEqual(read_exact(conn, 8), b"\x00\x5b" + b"\x00" * 6)
        self.assertEqual(self.exchange(), b"forwarded")
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_cancel_and_stop_close_handshakes_and_active_connections(self):
        self.start("-D", self.spec)
        active = connect_proxy(self.port, "127.0.0.1", self.remote_port)
        pending = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        try:
            active.sendall(b"open")
            self.assertEqual(read_exact(active, 4), b"open")
            pending.sendall(b"\x05")
            job = self.jobs()[0]["id"]
            self.assertEqual(run(self.name, "--stop", "--job", str(job)).returncode, 0)
            self.assertEqual(active.recv(1), b"")
            self.assertEqual(pending.recv(1), b"")
            self.add()
            self.assertEqual(self.exchange(), b"forwarded")
            self.assertEqual(run(self.name, "--stop").returncode, 0)
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", self.port))
        finally:
            active.close()
            pending.close()

    def test_handshake_timeout_keeps_listener_alive(self):
        self.start("-D", self.spec, "-o", "1")
        with socket.create_connection(("127.0.0.1", self.port), timeout=4) as conn:
            conn.sendall(b"\x05")
            self.assertEqual(conn.recv(1), b"")
        self.assertEqual(self.exchange(), b"forwarded")

    def test_idle_expiry_releases_proxy(self):
        self.start("-D", self.spec, "--session-idle", "0.3")
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and os.path.exists(mssh.session_path(self.name)):
                time.sleep(0.05)
            self.assertFalse(os.path.exists(mssh.session_path(self.name)))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.port))

    def test_closed_and_malformed_clients_do_not_leak_descriptors(self):
        self.start("-D", self.spec)
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            pid = mssh.session_request(self.name, {"op": "status"}, timeout=3)["pid"]
        directory = "/proc/%d/fd" % pid
        baseline = len(os.listdir(directory))
        for _ in range(15):
            self.assertEqual(self.exchange(), b"forwarded")
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
                conn.sendall(b"\x05\x01")
                conn.shutdown(socket.SHUT_WR)
                self.assertEqual(conn.recv(1), b"")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(os.listdir(directory)) > baseline:
            time.sleep(0.05)
        self.assertLessEqual(len(os.listdir(directory)), baseline)

    def test_interactive_session_supports_proxy(self):
        self.start("-c", "cat", "--idle", "0.1")
        self.add()
        self.assertEqual(self.exchange(), b"forwarded")
        self.assertEqual(self.send("still alive").stdout, b"still alive\n")

    def test_default_bind_and_ipv6_listener_and_destination(self):
        self.start("-D", str(self.port))
        self.assertEqual(self.exchange(), b"forwarded")
        class IPv6Server(TCPServer):
            address_family = socket.AF_INET6
        try:
            server = IPv6Server(("::1", 0), Echo)
        except OSError:
            self.skipTest("IPv6 loopback unavailable")
        worker = threading.Thread(target=server.serve_forever)
        worker.daemon = True
        worker.start()
        try:
            extra = free_port()
            proc = run(self.name, "-D", "[::1]:%d" % extra)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with connect_proxy(extra, "::1", server.server_address[1], bind="::1") as conn:
                conn.sendall(b"ipv6")
                self.assertEqual(read_exact(conn, 4), b"ipv6")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)

    def test_wire_validation_does_not_create_partial_jobs(self):
        self.start()
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            for specs in (None, [], "1080", [1], ["invalid"]):
                reply = mssh.session_request(self.name, {
                    "op": "dynamic_forward", "specs": [
                        "127.0.0.1:%d:localhost:80" % self.port],
                    "dynamic_specs": specs}, timeout=3)
                self.assertIn("error", reply)
                self.assertEqual(self.jobs(), [])
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", self.port))

    def test_reconnect_keeps_proxy_job_and_configuration_while_down(self):
        with tempfile.TemporaryDirectory(prefix="mssh-socks-down-") as directory:
            down = os.path.join(directory, "down")
            with mock.patch.dict(ENV, MSSH_FAKE_DOWN=down):
                self.start("-D", self.spec, "--reconnect", "10")
                original = self.jobs()[0]["id"]
                self.assertEqual(self.exchange(), b"forwarded")
                with open(down, "w"):
                    pass
                self.assertEqual(self.send("echo disconnected").returncode, 1)
                extra = free_port()
                proc = run(self.name, "-D", "127.0.0.1:%d" % extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                job = self.jobs()[-1]["id"]
                self.assertEqual(run(self.name, "--stop", "--job", str(job)).returncode, 0)
                with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
                    conn.sendall(b"\x05\x01\x00" +
                                 request_packet("127.0.0.1", self.remote_port))
                    self.assertEqual(read_exact(conn, 2), b"\x05\x00")
                    self.assertEqual(read_exact(conn, 10),
                                     b"\x05\x01\x00\x01" + b"\x00" * 6)
                os.unlink(down)
                with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        info = mssh.session_request(self.name, {"op": "status"}, timeout=3)
                        if info["link"] == "up":
                            break
                        time.sleep(0.05)
                    self.assertEqual(info["link"], "up")
                self.assertEqual([row["id"] for row in self.jobs()], [original])
                self.assertEqual(self.exchange(), b"forwarded")

    def test_conflicting_options_and_non_session_usage(self):
        self.start()
        cases = [("host", "-D", self.spec), ("--sessions", "-D", self.spec)]
        cases.extend((self.name, "-D", self.spec, flag)
                     for flag in ("--status", "--stop", "--interrupt", "--jobs",
                                  "--exec", "-t", "--stdin", "-n"))
        cases.extend(((self.name, "-D", self.spec, "echo hello"),
                      (self.name, "-D", self.spec, "--get", "/tmp/a", "./a")))
        for args in cases:
            with self.subTest(args=args):
                proc = run(*args)
                self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.jobs(), [])


class TestOpenSSHSocks(unix_tests.OpenSSHForwardTest):
    """Reuse the ephemeral OpenSSH fixture for actual dynamic TCP channels."""

    def test_all_socks_protocols_over_real_direct_tcpip(self):
        port = free_port()
        job = self.session.add_forwards([], dynamic_specs=["127.0.0.1:%d" % port])[0]
        for version, domain in ((4, False), (4, True), (5, False), (5, True)):
            self.assertEqual(exchange_proxy(
                port, "localhost" if domain else "127.0.0.1",
                self.tcp_port, b"real SSH", version, domain), b"real SSH")
        self.session.interrupt_job(job.id)
        self.assertTrue(self.transport.is_active())
        self.assertEqual(self.session.jobs_snapshot(), [])

    @unittest.skipUnless(shutil.which("curl"), "curl is unavailable")
    def test_real_curl_socks5_hostname_client(self):
        class HTTP(Echo):
            def handle(handler):
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    chunk = handler.request.recv(4096)
                    if not chunk:
                        return
                    request.extend(chunk)
                handler.request.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")

        self.tcp.RequestHandlerClass = HTTP
        port = free_port()
        self.session.add_forwards([], dynamic_specs=["127.0.0.1:%d" % port])
        proc = subprocess.run([
            shutil.which("curl"), "--silent", "--show-error", "--max-time", "5",
            "--noproxy", "", "--socks5-hostname", "127.0.0.1:%d" % port,
            "http://localhost:%d/" % self.tcp_port], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, b"hello")
