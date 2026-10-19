"""Remote TCP/Unix listeners, SSH request lifecycle and session-owned jobs."""

import errno
import os
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import paramiko
from paramiko.common import MSG_CHANNEL_OPEN, MSG_REQUEST_SUCCESS, MSG_REQUEST_FAILURE

from test_session_daemon import ENV, SessionTest, mssh, run
from test_session_forward import ReplyAfterEOF, EOFBeforeInput, free_port
from test_session_unix_forward import OpenSSHForwardTest, UnixServers, exchange


class TestRemoteForwardParsing(unittest.TestCase):
    def test_four_forms_and_client_relative_paths(self):
        cases = {
            "8080:host:80": {"bind": "localhost", "port": 8080,
                             "host": "host", "hostport": 80},
            "[::1]:8080:[::1]:80": {"bind": "::1", "port": 8080,
                                    "host": "::1", "hostport": 80},
            "*:8080:host:80": {"bind": "*", "port": 8080,
                               "host": "host", "hostport": 80},
            "8080:./client.sock": {"bind": "localhost", "port": 8080,
                                   "remote_socket": os.path.abspath("./client.sock")},
            "./target.sock:host:80": {"local_socket": "./target.sock",
                                      "host": "host", "hostport": 80},
            "./target.sock:./client.sock": {
                "local_socket": "./target.sock",
                "remote_socket": os.path.abspath("./client.sock")},
            r"[/run/target:1.sock]:./client\:2.sock": {
                "local_socket": "/run/target:1.sock",
                "remote_socket": os.path.abspath("./client:2.sock")},
        }
        for spec, expected in cases.items():
            with self.subTest(spec=spec):
                settings = mssh.parse_remote_forward(spec)
                self.assertEqual(settings, expected)
                self.assertEqual(mssh.parse_remote_forward(
                    mssh._local_forward_spec(settings)), settings)

    def test_invalid_specs_name_correct_option(self):
        for spec in ("", "80:host", "0:host:80", "80:host:65536",
                     "/tmp/target.sock::80", "80:/tmp/\x00sock", "[bad]:80:host:80"):
            with self.subTest(spec=spec):
                with self.assertRaisesRegex(ValueError, "-R"):
                    mssh.parse_remote_forward(spec)
        with self.assertRaisesRegex(
                ValueError, r"-R expects \[bind_address:\]port:host:hostport, "
                r"\[bind_address:\]port:local_socket, remote_socket:host:hostport "
                r"or remote_socket:local_socket"):
            mssh.parse_remote_forward("80:host")

    def test_client_uses_distinct_op_and_normalizes_only_destination(self):
        with mock.patch.object(mssh, "session_request", return_value={
                "ok": True, "forwards": []}) as request:
            self.assertEqual(mssh.run_session_forward(
                "hop", ["8080:host:80"], ["1080"],
                ["./target.sock:./client.sock"]), 0)
        payload = request.call_args[0][1]
        self.assertEqual(payload["op"], "remote_forward")
        self.assertEqual(payload["dynamic_specs"], ["1080"])
        self.assertEqual(mssh.parse_remote_forward(payload["remote_specs"][0]),
                         {"local_socket": "./target.sock",
                          "remote_socket": os.path.abspath("./client.sock")})

    def test_old_daemon_does_not_silently_ignore_remote_forward(self):
        with mock.patch.object(mssh, "session_request",
                               return_value={"error": "unknown op"}):
            with mock.patch.object(mssh.sys, "stderr"):
                self.assertEqual(mssh.run_session_forward(
                    "hop", [], remote_specs=["8080:host:80"]), 1)


class TestRemoteForwardProtocol(unittest.TestCase):
    def setUp(self):
        local, peer = socket.socketpair()
        self.addCleanup(local.close)
        self.addCleanup(peer.close)
        self.transport = paramiko.Transport(local)
        self.addCleanup(self.transport.close)
        self.transport.active = True
        self.messages = []
        self.transport._send_message = mock.Mock(side_effect=self.messages.append)
        self.transport._send_user_message = mock.Mock(side_effect=self.messages.append)
        self.manager = mssh._RemoteForwardManager(self.transport, 0.01)
        self.manager.timeout = 0.05
        self.session = mssh._Session(mock.Mock(), "mark", "shell", None, 0,
                                     "target", None, transport=self.transport)

    def job(self, spec="127.0.0.1:8080:localhost:80"):
        job = mssh._RemoteForwardJob(self.session, mssh.parse_remote_forward(spec))
        job.manager, job.enabled = self.manager, True
        return job

    def respond(self, success=True):
        self.transport._handler_table[
            MSG_REQUEST_SUCCESS if success else MSG_REQUEST_FAILURE](paramiko.Message())

    def decode_request(self, message):
        msg = paramiko.Message(message.asbytes())
        self.assertEqual(msg.get_byte(), b"\x50")
        return msg, msg.get_text(), msg.get_boolean()

    def test_bind_cancel_packet_formats_and_handler_ownership(self):
        self.transport._send_user_message.side_effect = lambda msg: (
            self.messages.append(msg), self.respond())
        for spec, kind, endpoint in (
                ("127.0.0.1:8080:host:80", "tcpip-forward", ("127.0.0.1", 8080)),
                ("/run/target.sock:host:80", "streamlocal-forward@openssh.com",
                 ("/run/target.sock",))):
            job = self.job(spec)
            self.manager.bind(job)
            msg, actual, reply = self.decode_request(self.messages[-1])
            self.assertEqual(actual, kind)
            self.assertTrue(reply)
            self.assertEqual(msg.get_text(), endpoint[0])
            if len(endpoint) == 2:
                self.assertEqual(msg.get_int(), endpoint[1])
            self.assertEqual(msg.get_remainder(), b"")
            self.manager.cancel(job)
            msg, actual, reply = self.decode_request(self.messages[-1])
            self.assertEqual(actual, "cancel-" + kind)
            self.assertFalse(reply)
        self.assertEqual(self.manager.entries, {})
        self.assertIs(self.transport._handler_table[MSG_CHANNEL_OPEN].__self__,
                      self.manager)

    def test_denial_removes_registration(self):
        self.transport._send_user_message.side_effect = lambda msg: self.respond(False)
        with self.assertRaisesRegex(paramiko.SSHException, "denied"):
            self.manager.bind(self.job())
        self.assertEqual(self.manager.entries, {})
        self.assertIsNone(self.manager.pending)

    def test_unix_denial_names_remote_listener_and_possible_checks(self):
        self.transport._send_user_message.side_effect = lambda msg: self.respond(False)
        with self.assertRaises(paramiko.SSHException) as caught:
            self.manager.bind(self.job("/tmp/target.sock:/tmp/client.sock"))
        message = str(caught.exception)
        self.assertIn("Unix listener '/tmp/target.sock'", message)
        self.assertNotIn("/tmp/client.sock", message)
        self.assertIn("remote path already exists", message)
        self.assertIn("parent directory permissions", message)
        self.assertIn("AllowStreamLocalForwarding/StreamLocalBindUnlink", message)
        self.assertIn("SSH server did not provide a reason", message)
        self.assertEqual(self.manager.entries, {})

    def test_tcp_denial_names_listener_not_destination(self):
        self.transport._send_user_message.side_effect = lambda msg: self.respond(False)
        with self.assertRaises(paramiko.SSHException) as caught:
            self.manager.bind(self.job("[::1]:8080:destination.internal:80"))
        message = str(caught.exception)
        self.assertIn("TCP listener [::1]:8080", message)
        self.assertNotIn("destination.internal", message)
        self.assertIn("port occupancy", message)
        self.assertIn("sshd remote forwarding policy", message)
        self.assertIn("SSH server did not provide a reason", message)
        self.assertEqual(self.manager.entries, {})

    def test_late_success_cancels_abandoned_listener_and_allows_next_request(self):
        with self.assertRaisesRegex(paramiko.SSHException, "timed out"):
            self.manager.bind(self.job())
        self.assertEqual(self.manager.entries, {})
        with self.assertRaisesRegex(paramiko.SSHException, "still awaiting"):
            self.manager.bind(self.job("127.0.0.1:8081:host:80"))
        self.respond()
        msg, kind, reply = self.decode_request(self.messages[-1])
        self.assertEqual(kind, "cancel-tcpip-forward")
        self.assertFalse(reply)
        self.assertEqual((msg.get_text(), msg.get_int()), ("127.0.0.1", 8080))
        self.transport._send_message.assert_called_once()
        self.transport._send_user_message.side_effect = lambda msg: self.respond()
        self.manager.bind(self.job())

    def open_message(self, kind, endpoint):
        msg = paramiko.Message()
        msg.add_string(kind)
        for value in (42, 2097152, 32768):
            msg.add_int(value)
        msg.add_string(endpoint[0])
        if kind == "forwarded-tcpip":
            msg.add_int(endpoint[1])
            msg.add_string("127.0.0.1")
            msg.add_int(12345)
        else:
            msg.add_string("")
        return paramiko.Message(msg.asbytes())

    def test_unknown_or_disabled_incoming_is_rejected(self):
        for registered in (False, True):
            job = self.job()
            job.enabled = False
            if registered:
                self.manager.entries[("tcp", "127.0.0.1", 8080)] = job
            self.manager._incoming(self.open_message(
                "forwarded-tcpip", ("127.0.0.1", 8080)))
            self.assertEqual(self.messages[-1].asbytes()[0:1], b"\x5c")
        self.assertEqual(len(self.transport._channels), 0)

    def test_incoming_channel_registration_and_worker_start_failure(self):
        job = self.job("/run/target.sock:host:80")
        self.manager.entries[("unix", "/run/target.sock")] = job
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("full")):
            self.manager._incoming(self.open_message(
                "forwarded-streamlocal@openssh.com", ("/run/target.sock",)))
        self.assertTrue(self.transport.is_active())
        self.assertEqual(job.connections, {})
        self.assertEqual(self.messages[0].asbytes()[0:1], b"\x5b")
        chan = self.transport._channels.get(0)
        self.assertTrue(chan.closed)
        self.assertIsNone(chan._pipe)
        self.transport._send_user_message.assert_not_called()
        self.assertEqual(self.messages[-1].asbytes()[0:1], b"\x61")

    def test_abort_racing_incoming_never_creates_select_pipe(self):
        job = self.job()
        job.aborted = True
        chan = paramiko.Channel(1)
        chan._set_transport(self.transport)
        chan._set_remote_channel(42, 2097152, 32768)
        with mock.patch.object(chan, "fileno") as fileno:
            job.incoming(chan, self.transport)
        fileno.assert_not_called()
        self.assertTrue(chan.closed)
        self.assertEqual(job.connections, {})

    def test_unrelated_requests_retain_original_paramiko_handlers(self):
        self.manager.original_open = mock.Mock()
        self.manager.original_success = mock.Mock()
        self.manager.original_failure = mock.Mock()
        msg = paramiko.Message()
        msg.add_string("x11")
        msg = paramiko.Message(msg.asbytes())
        self.manager._incoming(msg)
        self.manager.original_open.assert_called_once_with(msg)
        # Peeking must not consume the original forwarded message.
        self.assertEqual(msg.get_text(), "x11")
        self.respond()
        self.respond(False)
        self.manager.original_success.assert_called_once()
        self.manager.original_failure.assert_called_once()

    def test_abort_during_bind_cancels_acknowledged_listener(self):
        entered, release = threading.Event(), threading.Event()
        job = self.job()
        job.manager = None
        self.session.execs.add(job)

        def respond_after_wait(msg):
            entered.set()
            release.wait(3)
            self.respond()

        self.manager.timeout = 3
        self.transport._send_user_message.side_effect = respond_after_wait
        binding = threading.Thread(target=job.rebind, args=(self.manager,))
        stopping = threading.Thread(target=job.abort)
        try:
            binding.start()
            self.assertTrue(entered.wait(2))
            stopping.start()
            for _ in range(100):
                if job.aborted:
                    break
                time.sleep(0.01)
            release.set()
            binding.join(3)
            stopping.join(3)
            self.assertFalse(binding.is_alive())
            self.assertFalse(stopping.is_alive())
            self.assertEqual(self.manager.entries, {})
            self.assertFalse(job.enabled)
            self.assertTrue(job.closed.is_set())
        finally:
            release.set()
            binding.join(3)
            stopping.join(3)

    def test_late_cancel_is_sent_before_allowing_another_bind(self):
        with self.assertRaises(paramiko.SSHException):
            self.manager.bind(self.job())

        def cancel(message):
            with self.assertRaisesRegex(paramiko.SSHException, "still awaiting"):
                self.manager.bind(self.job())

        self.transport._send_message.side_effect = cancel
        self.respond()
        self.assertIsNone(self.manager.pending)
        self.assertEqual(self.manager.entries, {})

    def test_replacement_bind_cannot_overtake_old_listener_cancel(self):
        old, replacement = self.job(), self.job()
        self.manager.entries[self.manager.endpoint(old.settings)] = old
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        packets, errors = [], []

        def send(message):
            _msg, kind, reply = self.decode_request(message)
            if kind == "cancel-tcpip-forward":
                entered.set()
                release.wait(3)
            packets.append(kind)
            if reply:
                self.respond()

        def bind():
            try:
                self.manager.bind(replacement)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        self.transport._send_user_message.side_effect = send
        cancel_worker = threading.Thread(target=self.manager.cancel, args=(old,))
        bind_worker = threading.Thread(target=bind)
        try:
            cancel_worker.start()
            self.assertTrue(entered.wait(2))
            bind_worker.start()
            self.assertFalse(done.wait(0.1))
            release.set()
            cancel_worker.join(3)
            bind_worker.join(3)
            self.assertTrue(done.is_set())
            self.assertEqual(errors, [])
            self.assertEqual(packets, ["cancel-tcpip-forward", "tcpip-forward"])
            self.assertIs(self.manager.entries[self.manager.endpoint(old.settings)],
                          replacement)
        finally:
            release.set()
            cancel_worker.join(3)
            if bind_worker.ident is not None:
                bind_worker.join(3)

    def test_delayed_dns_cannot_attach_to_replaced_listener(self):
        job = self.job()
        chan = paramiko.Channel(1)
        chan._set_transport(self.transport)
        chan._set_remote_channel(42, 2097152, 32768)
        connection = mssh._ForwardConnection(job)
        self.assertTrue(connection.set_channel(chan))
        with job.lock:
            job._register_connection(connection)

        def resolve(*args):
            job.unbind()
            job.manager = mock.Mock(transport=mock.Mock())
            job.enabled = True
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 80))]

        with mock.patch.object(socket, "getaddrinfo", side_effect=resolve), \
                mock.patch.object(socket, "socket") as create_socket, \
                mock.patch.object(job, "_relay") as relay:
            job._connect_local(connection, self.transport)
        create_socket.return_value.connect_ex.assert_not_called()
        create_socket.return_value.close.assert_called_once()
        relay.assert_not_called()
        self.assertTrue(chan.closed)
        self.assertEqual(job.connections, {})

    def test_single_connection_cancel_during_dns_keeps_listener(self):
        job = self.job()
        chan = paramiko.Channel(1)
        chan._set_transport(self.transport)
        chan._set_remote_channel(42, 2097152, 32768)
        connection = mssh._ForwardConnection(job)
        self.assertTrue(connection.set_channel(chan))
        with job.lock:
            job._register_connection(connection)

        def resolve(*args):
            self.assertEqual(job.connections_snapshot()[0]["channel"], 1)
            job.interrupt_connection(connection.id)
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 80))]

        with mock.patch.object(socket, "getaddrinfo", side_effect=resolve), \
                mock.patch.object(socket, "socket") as create_socket, \
                mock.patch.object(job, "_relay") as relay:
            job._connect_local(connection, self.transport)
        create_socket.return_value.connect_ex.assert_not_called()
        create_socket.return_value.close.assert_called_once()
        relay.assert_not_called()
        self.assertTrue(chan.closed)
        self.assertFalse(job.aborted)
        self.assertTrue(job.enabled)
        self.assertIs(job.manager, self.manager)
        self.assertEqual(job.connections, {})
        self.assertTrue(self.transport.is_active())

    def test_single_connection_cancel_during_local_connect_keeps_listener(self):
        job = self.job()
        chan = paramiko.Channel(1)
        chan._set_transport(self.transport)
        chan._set_remote_channel(42, 2097152, 32768)
        connection = mssh._ForwardConnection(job)
        self.assertTrue(connection.set_channel(chan))
        with job.lock:
            job._register_connection(connection)
        conn = mock.Mock()
        conn.connect_ex.return_value = errno.EINPROGRESS

        def wait(*args):
            self.assertEqual(job.connections_count(), 1)
            job.interrupt_connection(connection.id)
            return [], [], []

        with mock.patch.object(socket, "getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 80))]), \
                mock.patch.object(socket, "socket", return_value=conn), \
                mock.patch.object(mssh.select, "select", side_effect=wait), \
                mock.patch.object(job, "_relay") as relay:
            job._connect_local(connection, self.transport)
        conn.close.assert_called_once_with()
        relay.assert_not_called()
        self.assertTrue(chan.closed)
        self.assertFalse(job.aborted)
        self.assertTrue(job.enabled)
        self.assertEqual(job.connections_count(), 0)
        self.assertTrue(self.transport.is_active())

    def test_failed_rebuild_cancels_all_attempted_remote_binds_and_can_retry(self):
        session = self.session
        session.broken = "connection dropped"
        original_transport = session.transport
        original_chan = session.chan
        session.remote_manager = self.manager
        jobs = [self.job("127.0.0.1:%d:localhost:80" % port) for port in (8080, 8081)]
        for index, job in enumerate(jobs):
            job.id = index + 1
            session.execs.add(job)
        manager = mock.Mock(transport=mock.Mock())
        manager.bind.side_effect = [None, paramiko.SSHException("bind denied")]
        replacement = mock.Mock(closed=False)
        with mock.patch.object(mssh, "_open_shell", return_value=(
                [], manager.transport, replacement, "new-mark")), \
                mock.patch.object(session, "setup"), \
                mock.patch.object(session, "remote_forward_manager", return_value=manager):
            self.assertFalse(session.rebuild())
            self.assertIs(session.transport, original_transport)
            self.assertIs(session.chan, original_chan)
            self.assertIs(session.remote_manager, self.manager)
            self.assertEqual(manager.cancel.call_count, 2)
            self.assertTrue(all(job.manager is None and not job.enabled for job in jobs))
            self.assertIn("bind denied", session.last_error)
            self.assertEqual(session.jobs_count(), 2)
            manager.bind.side_effect = None
            self.assertTrue(session.rebuild())
            self.assertEqual(session.link_state()[0], "up")
            self.assertTrue(all(job.manager is manager and job.enabled for job in jobs))
        for job in jobs:
            job.abort()


class TestOpenSSHRemoteForward(OpenSSHForwardTest):
    def add_remote(self, specs):
        return self.session.add_forwards([], remote_specs=specs)

    def test_all_four_forms_binary_parallel_traffic_and_half_close(self):
        self.unix.RequestHandlerClass = ReplyAfterEOF
        self.tcp.RequestHandlerClass = ReplyAfterEOF
        ports = [free_port(), free_port()]
        paths = [os.path.join(self.temp.name, "target-%d.sock" % n) for n in range(2)]
        jobs = self.add_remote([
            "127.0.0.1:%d:127.0.0.1:%d" % (ports[0], self.tcp_port),
            "127.0.0.1:%d:%s" % (ports[1], self.remote_socket),
            "%s:127.0.0.1:%d" % (paths[0], self.tcp_port),
            "%s:%s" % (paths[1], self.remote_socket)])
        payload = bytes(range(256)) * 4096
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda ep: exchange(ep, payload), ports + paths))
        self.assertEqual(results, [b"reply:" + payload] * 4)
        self.assertEqual([row["forward_type"] for row in self.session.jobs_snapshot()],
                         ["remote"] * 4)
        for job in jobs:
            self.session.interrupt_job(job.id)
        self.assertTrue(self.transport.is_active())
        self.assertEqual(self.session.jobs_count(), 0)

    def test_default_and_wildcard_listener_routing_and_independent_cancel(self):
        ports = [free_port() for _ in range(3)]
        jobs = self.add_remote([
            "%s%d:127.0.0.1:%d" % (prefix, port, self.tcp_port)
            for prefix, port in zip(("", "*:", ":"), ports)])
        for port in ports:
            self.assertEqual(exchange(port), b"forwarded")
        self.session.interrupt_job(jobs[1].id)
        self.assertEqual(exchange(ports[0]), b"forwarded")
        self.assertEqual(exchange(ports[2]), b"forwarded")

    def test_failed_mixed_batch_rolls_back_new_jobs_only(self):
        keep, rollback = free_port(), free_port()
        self.add_remote(["127.0.0.1:%d:localhost:%d" % (keep, self.tcp_port)])
        with self.assertRaises((ValueError, paramiko.SSHException)):
            self.session.add_forwards(
                ["127.0.0.1:%d:localhost:%d" % (rollback, self.tcp_port)],
                dynamic_specs=["127.0.0.1:%d" % free_port()],
                remote_specs=["127.0.0.1:%d:localhost:%d" % (keep, self.tcp_port)])
        self.assertEqual(self.session.jobs_count(), 1)
        self.assertEqual(exchange(keep), b"forwarded")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", rollback))

    def test_local_destination_failure_does_not_end_transport(self):
        port = free_port()
        self.add_remote(["127.0.0.1:%d:%s.missing" % (port, self.remote_socket)])
        with socket.create_connection(("127.0.0.1", port), timeout=3) as conn:
            self.assertEqual(conn.recv(1), b"")
        self.assertTrue(self.transport.is_active())
        port = free_port()
        self.add_remote(["127.0.0.1:%d:localhost:%d" % (port, self.tcp_port)])
        self.assertEqual(exchange(port), b"forwarded")

    def test_destination_can_half_close_before_receiving_input(self):
        self.tcp.RequestHandlerClass = EOFBeforeInput
        port = free_port()
        self.add_remote(["127.0.0.1:%d:localhost:%d" % (port, self.tcp_port)])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            self.assertEqual(conn.recv(8), b"greeting")
            self.assertEqual(conn.recv(1), b"")
            conn.sendall(b"after remote EOF")
            conn.shutdown(socket.SHUT_WR)
            self.assertTrue(self.tcp.done.wait(3))
        self.assertEqual(self.tcp.received, b"after remote EOF")

    def test_cancel_closes_live_channels_but_preserves_other_job(self):
        ports = [free_port(), free_port()]
        jobs = self.add_remote([
            "127.0.0.1:%d:localhost:%d" % (port, self.tcp_port) for port in ports])
        with socket.create_connection(("127.0.0.1", ports[0]), timeout=3) as conn:
            conn.sendall(b"echo")
            self.assertEqual(conn.recv(4), b"echo")
            self.session.interrupt_job(jobs[0].id)
            self.assertEqual(conn.recv(1), b"")
        self.assertEqual(exchange(ports[1]), b"forwarded")
        self.assertTrue(self.transport.is_active())

    def test_existing_remote_unix_path_is_never_deleted(self):
        with self.assertRaises(paramiko.SSHException):
            self.add_remote(["%s:localhost:%d" % (self.remote_socket, self.tcp_port)])
        self.assertEqual(exchange(self.remote_socket), b"forwarded")
        self.assertEqual(self.session.jobs_count(), 0)

    def test_sshd_denied_bind_rolls_back_previously_bound_remote_listener(self):
        port = free_port()
        with self.assertRaises(paramiko.SSHException):
            self.add_remote([
                "127.0.0.1:%d:localhost:%d" % (port, self.tcp_port),
                "127.0.0.1:%d:localhost:%d" % (self.tcp_port, self.tcp_port)])
        self.assertEqual(self.session.jobs_count(), 0)
        # Cancel has no reply. Another acknowledged global request is an
        # ordering barrier before checking that the old port can be rebound.
        self.add_remote(["127.0.0.1:%d:localhost:%d" % (port, self.tcp_port)])
        self.assertEqual(exchange(port), b"forwarded")
        self.assertTrue(self.transport.is_active())

    def test_explicit_ipv6_remote_listener(self):
        with socket.socket(socket.AF_INET6) as probe:
            try:
                probe.bind(("::1", 0))
            except OSError:
                self.skipTest("IPv6 loopback unavailable")
            port = probe.getsockname()[1]
        self.add_remote(["[::1]:%d:localhost:%d" % (port, self.tcp_port)])
        with socket.create_connection(("::1", port), timeout=3) as conn:
            conn.sendall(b"ipv6")
            self.assertEqual(conn.recv(4), b"ipv6")


class TestSessionRemoteForward(SessionTest, UnixServers):
    def setUp(self):
        super(TestSessionRemoteForward, self).setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="mssh-session-reverse-")
        self.start_servers(self.temp.name)

    def tearDown(self):
        super(TestSessionRemoteForward, self).tearDown()
        self.stop_servers()
        self.temp.cleanup()

    def spec(self, port):
        return "127.0.0.1:%d:127.0.0.1:%d" % (port, self.tcp_port)

    def jobs(self):
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            return mssh.session_request(self.name, {"op": "jobs"}, timeout=3)["jobs"]

    def test_initial_and_later_forward_configurations_and_targeted_stop(self):
        ports = [free_port(), free_port(), free_port(), free_port()]
        started = self.start("-R", self.spec(ports[0]))
        self.assertIn(b"remote 127.0.0.1", started.stderr)
        self.assertEqual(exchange(ports[0]), b"forwarded")
        configured = run(self.name, "-L", self.spec(ports[1]),
                         "-R", self.spec(ports[2]), "-D", str(ports[3]))
        self.assertEqual(configured.returncode, 0, configured.stderr)
        jobs = self.jobs()
        self.assertEqual([row["forward_type"] for row in jobs],
                         ["remote", "local", "socks", "remote"])
        self.assertEqual(exchange(ports[1]), b"forwarded")
        self.assertEqual(exchange(ports[2]), b"forwarded")
        status = run(self.name, "--status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn(b"reverse=2", status.stdout)
        self.assertNotIn(b"remote 127.0.0.1", status.stdout)
        listing = run("--sessions")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            self.assertEqual(mssh.session_request(
                self.name, {"op": "status", "view": "list"})["jobs"], 4)
        for selector, row in zip(("--stop", "--interrupt"), (jobs[0], jobs[3])):
            proc = run(self.name, selector, "--job", str(row["id"]))
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([row["forward_type"] for row in self.jobs()], ["local", "socks"])
        self.assertEqual(exchange(ports[1]), b"forwarded")
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_all_unix_combinations_through_cli(self):
        port = free_port()
        paths = [os.path.join(self.temp.name, "target-%d.sock" % n) for n in range(2)]
        self.start()
        configured = run(self.name, "-R", "127.0.0.1:%d:%s" % (port, self.remote_socket),
                         "-R", "%s:127.0.0.1:%d" % (paths[0], self.tcp_port),
                         "-R", "%s:%s" % (paths[1], self.remote_socket))
        self.assertEqual(configured.returncode, 0, configured.stderr)
        for endpoint in [port] + paths:
            self.assertEqual(exchange(endpoint), b"forwarded")

    def test_failed_startup_or_later_batch_releases_new_listeners(self):
        keep, added = free_port(), free_port()
        self.start("-R", self.spec(keep))
        proc = run(self.name, "-R", self.spec(added), "-R", self.spec(keep))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(exchange(keep), b"forwarded")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", added))
        run(self.name, "--stop")
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", keep))
            reserved.listen(1)
            proc = run("--session", self.name, "root@10.0.0.1",
                       "-R", self.spec(added), "-R", self.spec(keep))
            self.assertEqual(proc.returncode, 1)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", added))

    def test_session_shutdown_closes_remote_listener_and_live_connection(self):
        port = free_port()
        self.start("-R", self.spec(port))
        with socket.create_connection(("127.0.0.1", port), timeout=3) as conn:
            conn.sendall(b"open")
            self.assertEqual(conn.recv(4), b"open")
            self.assertEqual(run(self.name, "--stop").returncode, 0)
            self.assertEqual(conn.recv(1), b"")
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))

    def test_reconnect_restores_remote_listener_and_preserves_job_id(self):
        down = os.path.join(self.temp.name, "down")
        with mock.patch.dict(ENV, MSSH_FAKE_DOWN=down):
            port, stopped = free_port(), free_port()
            self.start("-R", self.spec(port), "-R", self.spec(stopped),
                       "--reconnect", "10")
            original, cancelled = self.jobs()
            self.assertEqual(exchange(port), b"forwarded")
            with open(down, "w"):
                pass
            self.assertEqual(self.send("echo disconnected").returncode, 1)
            self.assertEqual(run(self.name, "--stop", "--job",
                                 str(cancelled["id"])).returncode, 0)
            self.assertEqual(run(self.name, "-R", self.spec(free_port())).returncode, 1)
            os.unlink(down)
            with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    info = mssh.session_request(self.name, {"op": "status"}, timeout=3)
                    if info["link"] == "up":
                        break
                    time.sleep(0.05)
            self.assertEqual(info["link"], "up", info)
            self.assertEqual([row["id"] for row in self.jobs()], [original["id"]])
            self.assertEqual(exchange(port), b"forwarded")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", stopped))

    def test_interactive_session_can_own_remote_forward(self):
        port = free_port()
        self.start("-c", "cat", "--idle", "0.1", "-R", self.spec(port))
        self.assertEqual(exchange(port), b"forwarded")
        self.assertEqual(self.send("alive").stdout, b"alive\n")

    def test_cli_rejects_conflicting_options(self):
        self.start()
        for args in (("root@10.0.0.1", "-R", "8080:host:80"),
                     ("--sessions", "-R", "8080:host:80"),
                     (self.name, "-R", "8080:host:80", "echo hello"),
                     (self.name, "-R", "8080:host:80", "--jobs"),
                     (self.name, "-R", "8080:host:80", "--stop"),
                     (self.name, "-R", "8080:host:80", "--exec"),
                     (self.name, "-R", "8080:host:80", "--put", "src", "dst")):
            with self.subTest(args=args):
                self.assertEqual(run(*args).returncode, 2)
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            for payload in ({"op": "remote_forward", "remote_specs": []},
                            {"op": "remote_forward", "remote_specs": "8080:host:80"},
                            {"op": "remote_forward", "remote_specs": ["8080:host:80"],
                             "dynamic_specs": "1080"}):
                self.assertIn("error", mssh.session_request(self.name, payload, timeout=3))
        self.assertEqual(self.jobs(), [])


if __name__ == "__main__":
    unittest.main()
