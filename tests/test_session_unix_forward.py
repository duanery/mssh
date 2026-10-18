"""SSH-compatible TCP/Unix forwards, socket lifecycle and OpenSSH streamlocal."""

import getpass
import os
import shutil
import socket
import socketserver
import stat
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from unittest import mock

import paramiko

from test_session_daemon import ENV, SessionTest, mssh, run
from test_session_forward import (
    Echo, EOFBeforeInput, ReplyAfterEOF, TCPServer, free_port,
)


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def exchange(address, data=b"forwarded"):
    if isinstance(address, str):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(5)
        conn.connect(address)
    else:
        conn = socket.create_connection(("127.0.0.1", address), timeout=5)
    with conn:
        conn.sendall(data)
        conn.shutdown(socket.SHUT_WR)
        result = bytearray()
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return bytes(result)
            result.extend(chunk)


class TestUnixForwardParsing(unittest.TestCase):
    def test_ssh_four_forms_and_paths(self):
        cases = {
            "8080:host:80": {"bind": "localhost", "port": 8080,
                             "host": "host", "hostport": 80},
            "8080:/run/service.sock": {"bind": "localhost", "port": 8080,
                                      "remote_socket": "/run/service.sock"},
            "[::1]:8080:/run/service.sock": {
                "bind": "::1", "port": 8080, "remote_socket": "/run/service.sock"},
            ":8080:/run/service.sock": {
                "bind": "", "port": 8080, "remote_socket": "/run/service.sock"},
            "/tmp/client.sock:host:80": {
                "local_socket": "/tmp/client.sock", "host": "host", "hostport": 80},
            "/tmp/client.sock:[::1]:80": {
                "local_socket": "/tmp/client.sock", "host": "::1", "hostport": 80},
            "/tmp/client.sock:/run/service.sock": {
                "local_socket": "/tmp/client.sock", "remote_socket": "/run/service.sock"},
            "./client.sock:./service.sock": {
                "local_socket": os.path.abspath("./client.sock"),
                "remote_socket": "./service.sock"},
            r"/tmp/client\:name.sock:/run/server\\name.sock": {
                "local_socket": "/tmp/client:name.sock",
                "remote_socket": "/run/server\\name.sock"},
            "/tmp/client space.sock:/run/server space.sock": {
                "local_socket": "/tmp/client space.sock",
                "remote_socket": "/run/server space.sock"},
            r"./\[client\].sock:/run/\[server\].sock": {
                "local_socket": os.path.abspath("./[client].sock"),
                "remote_socket": "/run/[server].sock"},
            "[/tmp/client:1.sock]:[/run/server:1.sock]": {
                "local_socket": "/tmp/client:1.sock",
                "remote_socket": "/run/server:1.sock"},
        }
        for spec, expected in cases.items():
            with self.subTest(spec=spec):
                settings = mssh.parse_local_forward(spec)
                self.assertEqual(settings, expected)
                self.assertEqual(mssh.parse_local_forward(
                    mssh._local_forward_spec(settings)), settings)

    def test_reject_invalid_unix_forms(self):
        for spec in ("8080:socket", "socket:socket", "/tmp/local.sock:socket",
                     "/tmp/local.sock::80", "/tmp/local.sock:host:0",
                     "0:/run/remote.sock", "/tmp/local.sock:/run/\x00remote",
                     "8080:/run/remote\nsock", "/tmp/local.sock:",
                     r"8080:/run/remote.sock" + "\\", "[::1:8080:/run/sock",
                     "[::1]suffix:8080:/run/sock", "[/tmp/sock]suffix:host:80"):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    mssh.parse_local_forward(spec)

    def test_client_normalizes_relative_local_path_before_daemon_request(self):
        spec = "./client.sock:/run/service.sock"
        with mock.patch.object(mssh, "session_request", return_value={
                "ok": True, "forwards": []}) as request:
            self.assertEqual(mssh.run_session_forward("hop", [spec]), 0)
        forwarded = request.call_args[0][1]["specs"][0]
        self.assertEqual(mssh.parse_local_forward(forwarded)["local_socket"],
                         os.path.abspath("./client.sock"))


class TestStreamLocalProtocol(unittest.TestCase):
    """Drive Paramiko's actual response handlers without SSH timing dependencies."""

    def setUp(self):
        local, remote = socket.socketpair()
        self.addCleanup(local.close)
        self.addCleanup(remote.close)
        self.transport = paramiko.Transport(local)
        self.addCleanup(self.transport.close)
        self.transport.active = True
        self.messages = []
        self.transport._send_user_message = mock.Mock(side_effect=self.messages.append)
        self.transport._send_message = mock.Mock(side_effect=self.messages.append)

    def open_id(self, message):
        message = paramiko.Message(message.asbytes())
        self.assertEqual(message.get_byte(), b"\x5a")
        self.assertEqual(message.get_text(), "direct-streamlocal@openssh.com")
        chanid = message.get_int()
        self.assertEqual(message.get_int(), self.transport.default_window_size)
        self.assertEqual(message.get_int(), self.transport.default_max_packet_size)
        self.assertEqual(message.get_text(), "/run/service.sock")
        self.assertEqual(message.get_text(), "")
        self.assertEqual(message.get_int(), 0)
        self.assertEqual(message.get_remainder(), b"")
        return chanid

    def success(self, chanid):
        response = paramiko.Message()
        for value in (chanid, 42, 2097152, 32768):
            response.add_int(value)
        self.transport._parse_channel_open_success(paramiko.Message(response.asbytes()))

    def failure(self, chanid):
        response = paramiko.Message()
        response.add_int(chanid)
        response.add_int(paramiko.OPEN_FAILED_CONNECT_FAILED)
        response.add_string("connect failed")
        response.add_string("")
        with self.assertLogs("paramiko.transport", level="ERROR"):
            self.transport._parse_channel_open_failure(
                paramiko.Message(response.asbytes()))

    def test_packet_fields_and_success_use_paramiko_channel_registry(self):
        def respond(message):
            self.success(self.open_id(message))

        self.transport._send_user_message.side_effect = respond
        chan = mssh._open_streamlocal_channel(self.transport, "/run/service.sock", 1)
        self.assertIs(self.transport._channels.get(chan.chanid), chan)
        self.assertNotIn(chan.chanid, self.transport.channel_events)
        self.assertEqual(chan.remote_chanid, 42)
        self.assertTrue(chan.active)
        self.transport._send_user_message.side_effect = self.messages.append
        mssh._close_session_channel(chan)

    def test_refused_open_cleans_event_and_channel(self):
        self.transport._send_user_message.side_effect = lambda message: \
            self.failure(self.open_id(message))
        with self.assertRaises(paramiko.ChannelException):
            mssh._open_streamlocal_channel(self.transport, "/run/service.sock", 1)
        self.assertEqual(self.transport.channel_events, {})
        self.assertEqual(len(self.transport._channels), 0)

    def test_timeout_keeps_pending_open_then_closes_late_success(self):
        with self.assertRaisesRegex(paramiko.SSHException, "Timeout"):
            mssh._open_streamlocal_channel(self.transport, "/run/service.sock", 0)
        chanid = self.open_id(self.messages[0])
        chan = self.transport._channels.get(chanid)
        self.assertTrue(chan.closed)
        self.assertTrue(chan.open_abandoned)
        self.assertIs(self.transport.channel_events[chanid].channel, chan)
        self.success(chanid)
        self.assertFalse(chan.active)
        self.assertEqual(self.transport.channel_events, {})
        self.assertEqual(len(self.transport._channels), 0)
        close = paramiko.Message(self.messages[-1].asbytes())
        self.assertEqual(close.get_byte(), b"\x61")
        self.assertEqual(close.get_int(), 42)
        self.assertEqual(close.get_remainder(), b"")
        # Transport-thread cancellation must not wait on rekeying.
        self.transport._send_message.assert_called_once()
        self.assertEqual(self.transport._send_user_message.call_count, 1)
        self.assertTrue(self.transport.is_active())

    def test_timeout_then_late_refusal_cleans_registry(self):
        with self.assertRaises(paramiko.SSHException):
            mssh._open_streamlocal_channel(self.transport, "/run/service.sock", 0)
        self.failure(self.open_id(self.messages[0]))
        self.assertEqual(self.transport.channel_events, {})
        self.assertEqual(len(self.transport._channels), 0)

    def test_transport_shutdown_releases_abandoned_pending_open(self):
        with self.assertRaises(paramiko.SSHException):
            mssh._open_streamlocal_channel(self.transport, "/run/service.sock", 0)
        chan = self.transport._channels.get(self.open_id(self.messages[0]))
        self.transport.close()
        self.assertTrue(chan.closed)
        self.assertEqual(self.transport.channel_events, {})
        self.assertEqual(len(self.transport._channels), 0)

    def test_send_failure_cleans_unpublished_open(self):
        self.transport._send_user_message.side_effect = OSError("send failed")
        with self.assertRaisesRegex(OSError, "send failed"):
            mssh._open_streamlocal_channel(self.transport, "/run/service.sock", 1)
        self.assertEqual(self.transport.channel_events, {})
        self.assertEqual(len(self.transport._channels), 0)

    def test_inactive_transport_does_not_allocate_channel(self):
        self.transport.active = False
        with self.assertRaisesRegex(paramiko.SSHException, "not active"):
            mssh._open_streamlocal_channel(self.transport, "/run/service.sock", 1)
        self.assertEqual(self.transport.channel_events, {})
        self.assertEqual(len(self.transport._channels), 0)
        self.transport._send_user_message.assert_not_called()

    def test_simultaneous_tcp_and_streamlocal_use_unique_channel_ids(self):
        channel_ids = []
        lock = threading.Lock()

        def respond(message):
            message = paramiko.Message(message.asbytes())
            self.assertEqual(message.get_byte(), b"\x5a")
            kind = message.get_text()
            self.assertIn(kind, ("direct-tcpip", "direct-streamlocal@openssh.com"))
            chanid = message.get_int()
            with lock:
                channel_ids.append(chanid)
            self.success(chanid)

        def open_channel(index):
            if index % 2:
                return self.transport.open_channel(
                    "direct-tcpip", ("localhost", 80), ("localhost", 1234), timeout=1)
            return mssh._open_streamlocal_channel(
                self.transport, "/run/service.sock", 1)

        self.transport._send_user_message.side_effect = respond
        with ThreadPoolExecutor(max_workers=8) as pool:
            channels = list(pool.map(open_channel, range(20)))
        self.assertEqual(len(set(channel_ids)), 20)
        self.assertEqual(len(self.transport._channels), 20)
        self.assertEqual(self.transport.channel_events, {})
        self.transport._send_user_message.side_effect = None
        for chan in channels:
            mssh._close_session_channel(chan)


class TestUnixForwardLifecycle(unittest.TestCase):
    def test_listener_setup_failure_removes_created_socket(self):
        with TemporaryDirectory(prefix="mssh-unix-failure-") as directory:
            path = os.path.join(directory, "local.sock")
            session = mssh._Session(mock.Mock(), "marker", "shell", None, 0.1,
                                    "target", None, transport=mock.Mock(), timeout=1)
            with mock.patch.object(mssh.os, "chmod", side_effect=OSError("chmod failed")):
                with self.assertRaisesRegex(OSError, "chmod failed"):
                    session.add_forwards(["%s:/run/service.sock" % path])
            self.assertFalse(os.path.lexists(path))
            self.assertEqual(session.jobs_snapshot(), [])

    def test_acceptor_start_failure_removes_all_created_sockets(self):
        with TemporaryDirectory(prefix="mssh-unix-failure-") as directory:
            paths = [os.path.join(directory, "local%d.sock" % i) for i in range(2)]
            session = mssh._Session(mock.Mock(), "marker", "shell", None, 0.1,
                                    "target", None, transport=mock.Mock(), timeout=1)
            with mock.patch.object(threading.Thread, "start",
                                   side_effect=RuntimeError("cannot start thread")):
                with self.assertRaisesRegex(RuntimeError, "cannot start thread"):
                    session.add_forwards(["%s:/run/service.sock" % path for path in paths])
            for path in paths:
                self.assertFalse(os.path.lexists(path))
            self.assertEqual(session.jobs_snapshot(), [])


class UnixServers(object):
    def start_servers(self, directory):
        self.remote_socket = os.path.join(directory, "remote.sock")
        self.tcp = TCPServer(("127.0.0.1", 0), Echo)
        self.tcp_port = self.tcp.server_address[1]
        self.unix = UnixServer(self.remote_socket, Echo)
        self.workers = []
        for server in (self.tcp, self.unix):
            server.received = None
            server.done = threading.Event()
            worker = threading.Thread(target=server.serve_forever)
            worker.daemon = True
            worker.start()
            self.workers.append(worker)

    def stop_servers(self):
        for server in (self.tcp, self.unix):
            server.shutdown()
            server.server_close()
        for worker in self.workers:
            worker.join(3)


class TestSessionUnixForward(SessionTest, UnixServers):
    def setUp(self):
        super(TestSessionUnixForward, self).setUp()
        self.temp = TemporaryDirectory(prefix="mssh-unix-forward-")
        self.start_servers(self.temp.name)
        self.local_socket = os.path.join(self.temp.name, "local.sock")

    def tearDown(self):
        try:
            super(TestSessionUnixForward, self).tearDown()
        finally:
            self.stop_servers()
            self.temp.cleanup()

    def jobs(self):
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            return mssh.session_request(self.name, {"op": "jobs"}, timeout=3)["jobs"]

    def add(self, spec):
        proc = run(self.name, "-L", spec)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.jobs()[-1]["id"]

    def test_startup_unix_to_unix_and_job_stop(self):
        self.start("-L", "%s:%s" % (self.local_socket, self.remote_socket))
        self.assertEqual(exchange(self.local_socket), b"forwarded")
        self.assertEqual(stat.S_IMODE(os.stat(self.local_socket).st_mode), 0o600)
        rows = self.jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "forward")
        self.assertIn(self.local_socket, rows[0]["command"])
        self.assertIn(self.remote_socket, rows[0]["command"])
        self.assertIsNone(rows[0]["channel"])
        self.assertEqual(run(self.name, "--interrupt").returncode, 0)
        self.assertEqual(exchange(self.local_socket), b"forwarded")
        proc = run(self.name, "--job", str(rows[0]["id"]), "--stop")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.lexists(self.local_socket))
        self.assertTrue(os.path.exists(self.remote_socket))
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_all_four_forms_in_one_batch(self):
        self.start()
        tcp_to_tcp, tcp_to_unix = free_port(), free_port()
        second = self.local_socket + ".second"
        proc = run(self.name,
                   "-L", "127.0.0.1:%d:127.0.0.1:%d" % (tcp_to_tcp, self.tcp_port),
                   "-L", "127.0.0.1:%d:%s" % (tcp_to_unix, self.remote_socket),
                   "-L", "%s:127.0.0.1:%d" % (self.local_socket, self.tcp_port),
                   "-L", "%s:%s" % (second, self.remote_socket))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for endpoint in (tcp_to_tcp, tcp_to_unix, self.local_socket, second):
            self.assertEqual(exchange(endpoint), b"forwarded")
        self.assertEqual(len(self.jobs()), 4)
        self.assertEqual(run(self.name, "--stop").returncode, 0)
        self.assertFalse(os.path.lexists(self.local_socket))
        self.assertFalse(os.path.lexists(second))

    def test_relative_path_uses_configuring_client_directory(self):
        self.start()
        for path in ("local.sock", "client space.sock"):
            original = os.getcwd()
            try:
                os.chdir(self.temp.name)
                self.add("./%s:%s" % (path, self.remote_socket))
            finally:
                os.chdir(original)
            self.assertEqual(exchange(os.path.join(self.temp.name, path)), b"forwarded")

    def test_escaped_path_and_parallel_binary_half_closes(self):
        self.unix.RequestHandlerClass = ReplyAfterEOF
        self.start()
        path = self.local_socket + ":colon\\slash"
        spec = mssh._local_forward_spec({
            "local_socket": path, "remote_socket": self.remote_socket})
        self.add(spec)
        payload = bytes(range(256)) * 8192
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: exchange(path, payload), range(3)))
        self.assertEqual(results, [b"reply:" + payload] * 3)

    def test_remote_half_close_preserves_local_input(self):
        self.unix.RequestHandlerClass = EOFBeforeInput
        self.start()
        self.add("%s:%s" % (self.local_socket, self.remote_socket))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5)
            conn.connect(self.local_socket)
            data = bytearray()
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
            self.assertEqual(data, b"greeting")
            conn.sendall(b"after-eof")
            conn.shutdown(socket.SHUT_WR)
        self.assertTrue(self.unix.done.wait(3))
        self.assertEqual(self.unix.received, b"after-eof")

    def test_existing_file_symlink_and_socket_are_not_overwritten(self):
        self.start()
        for kind in ("file", "symlink", "socket"):
            path = os.path.join(self.temp.name, kind)
            listener = None
            if kind == "file":
                with open(path, "wb") as handle:
                    handle.write(b"keep")
            elif kind == "symlink":
                os.symlink("not-a-real-file", path)
            else:
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(path)
                listener.listen()
            try:
                before = os.lstat(path)
                proc = run(self.name, "-L", "%s:%s" % (path, self.remote_socket))
                self.assertEqual(proc.returncode, 1, proc.stderr)
                after = os.lstat(path)
                self.assertEqual((before.st_dev, before.st_ino),
                                 (after.st_dev, after.st_ino))
                self.assertEqual(self.jobs(), [])
            finally:
                if listener is not None:
                    listener.close()

    def test_socket_replaced_before_stop_is_preserved(self):
        self.start()
        for replacement in ("file", "socket"):
            job = self.add("%s:%s" % (self.local_socket, self.remote_socket))
            os.unlink(self.local_socket)
            listener = None
            if replacement == "file":
                with open(self.local_socket, "w") as handle:
                    handle.write("replacement")
            else:
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(self.local_socket)
            try:
                proc = run(self.name, "--stop", "--job", str(job))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertTrue(os.path.lexists(self.local_socket))
            finally:
                if listener is not None:
                    listener.close()
                os.unlink(self.local_socket)

    def test_failed_batch_and_startup_remove_only_created_socket(self):
        self.start()
        proc = run(self.name, "-L", "%s:%s" % (self.local_socket, self.remote_socket),
                   "-L", "%s:%s" % (self.remote_socket, self.remote_socket))
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.lexists(self.local_socket))
        self.assertTrue(os.path.exists(self.remote_socket))
        self.assertEqual(self.jobs(), [])
        run(self.name, "--stop")
        proc = run("--session", self.name, "root@10.0.0.1",
                   "-L", "%s:%s" % (self.local_socket, self.remote_socket),
                   "-L", "%s:%s" % (self.remote_socket, self.remote_socket))
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.lexists(self.local_socket))
        self.assertTrue(os.path.exists(self.remote_socket))

    def test_refused_remote_socket_keeps_listener_and_session_alive(self):
        self.start()
        self.add("%s:%s.missing" % (self.local_socket, self.remote_socket))
        self.assertEqual(exchange(self.local_socket, b""), b"")
        self.assertEqual(len(self.jobs()), 1)
        self.assertTrue(os.path.exists(self.local_socket))
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_stopping_active_connection_removes_socket(self):
        self.start()
        job = self.add("%s:%s" % (self.local_socket, self.remote_socket))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5)
            conn.connect(self.local_socket)
            conn.sendall(b"open")
            self.assertEqual(conn.recv(4), b"open")
            proc = run(self.name, "--interrupt", "--job", str(job))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(conn.recv(1), b"")
        self.assertFalse(os.path.lexists(self.local_socket))
        self.assertEqual(self.send("echo alive").stdout, b"alive\n")

    def test_idle_expiry_removes_local_socket(self):
        self.start("-L", "%s:%s" % (self.local_socket, self.remote_socket),
                   "--session-idle", "0.3")
        with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not os.path.lexists(self.local_socket):
                    break
                time.sleep(0.05)
            self.assertFalse(os.path.lexists(self.local_socket))
            self.assertFalse(os.path.exists(mssh.session_path(self.name)))
        self.assertTrue(os.path.exists(self.remote_socket))

    def test_interactive_session_supports_unix_forward(self):
        self.start("-c", "cat", "--idle", "0.1")
        self.add("%s:%s" % (self.local_socket, self.remote_socket))
        self.assertEqual(exchange(self.local_socket), b"forwarded")
        self.assertEqual(self.send("still alive").stdout, b"still alive\n")

    def test_reconnect_keeps_socket_inode_and_job_identity(self):
        down = os.path.join(self.temp.name, "down")
        with mock.patch.dict(ENV, MSSH_FAKE_DOWN=down):
            self.start("-L", "%s:%s" % (self.local_socket, self.remote_socket),
                       "--reconnect", "10")
            original = self.jobs()[0]["id"]
            inode = os.stat(self.local_socket).st_ino
            self.assertEqual(exchange(self.local_socket), b"forwarded")
            with open(down, "w"):
                pass
            self.assertEqual(self.send("echo disconnected").returncode, 1)
            with mock.patch.dict(os.environ, HOME=ENV["HOME"]):
                self.assertNotEqual(mssh.session_request(
                    self.name, {"op": "status"}, timeout=3)["link"], "up")
                self.assertEqual([row["id"] for row in self.jobs()], [original])
                self.assertEqual(exchange(self.local_socket, b""), b"")
                added = self.local_socket + ".added"
                extra = self.add("%s:%s" % (added, self.remote_socket))
                self.assertEqual(run(self.name, "--stop", "--job",
                                     str(extra)).returncode, 0)
                self.assertFalse(os.path.lexists(added))
                os.unlink(down)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    info = mssh.session_request(self.name, {"op": "status"}, timeout=3)
                    if info["link"] == "up":
                        break
                    time.sleep(0.05)
                self.assertEqual(info["link"], "up")
            self.assertEqual([row["id"] for row in self.jobs()], [original])
            self.assertEqual(os.stat(self.local_socket).st_ino, inode)
            self.assertEqual(exchange(self.local_socket), b"forwarded")


@unittest.skipUnless(shutil.which("sshd") and os.getuid() == 0,
                     "local OpenSSH inetd tests require sshd and root")
class TestOpenSSHUnixForward(unittest.TestCase, UnixServers):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="mssh-openssh-unix-")
        self.addCleanup(self.temp.cleanup)
        self.start_servers(self.temp.name)
        self.addCleanup(self.stop_servers)
        key = paramiko.RSAKey.generate(2048)
        host_key = os.path.join(self.temp.name, "host_key")
        key.write_private_key_file(host_key)
        authorized = os.path.join(self.temp.name, "authorized_keys")
        with open(authorized, "w") as handle:
            handle.write("%s %s\n" % (key.get_name(), key.get_base64()))
        config = os.path.join(self.temp.name, "sshd_config")
        with open(config, "w") as handle:
            handle.write(
                "HostKey %s\nAuthorizedKeysFile %s\nStrictModes no\n"
                "UsePAM no\nPermitRootLogin yes\nPubkeyAuthentication yes\n"
                "PasswordAuthentication no\nAllowTcpForwarding yes\n"
                "AllowStreamLocalForwarding yes\nLogLevel ERROR\n"
                % (host_key, authorized))
        local, remote = socket.socketpair()
        self.transport = paramiko.Transport(local)
        self.addCleanup(self.transport.close)
        try:
            self.daemon = subprocess.Popen(
                [shutil.which("sshd"), "-i", "-e", "-f", config],
                stdin=remote, stdout=remote, stderr=subprocess.PIPE)
        finally:
            remote.close()
        self.addCleanup(self.stop_sshd)
        self.transport.start_client(timeout=5)
        self.transport.auth_publickey(getpass.getuser(), key)
        self.session = mssh._Session(mock.Mock(), "marker", "shell", None, 0.1,
                                     "target", None, transport=self.transport, timeout=5)
        self.addCleanup(self.session.close_channels)

    def stop_sshd(self):
        self.transport.close()
        try:
            self.daemon.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.daemon.kill()
            self.daemon.communicate(timeout=5)

    def test_real_openssh_all_unix_combinations_and_half_closes(self):
        self.unix.RequestHandlerClass = ReplyAfterEOF
        self.tcp.RequestHandlerClass = ReplyAfterEOF
        tcp_port = free_port()
        local_tcp = os.path.join(self.temp.name, "local-tcp.sock")
        local_unix = os.path.join(self.temp.name, "local-unix.sock")
        jobs = self.session.add_forwards([
            "127.0.0.1:%d:%s" % (tcp_port, self.remote_socket),
            "%s:127.0.0.1:%d" % (local_tcp, self.tcp_port),
            "%s:%s" % (local_unix, self.remote_socket)])
        payload = bytes(range(256)) * 8192
        for endpoint in (tcp_port, local_tcp, local_unix):
            self.assertEqual(exchange(endpoint, payload), b"reply:" + payload)
        for job in jobs:
            self.session.interrupt_job(job.id)
        self.assertTrue(self.transport.is_active())
        self.assertEqual(self.session.jobs_snapshot(), [])
        self.assertFalse(os.path.lexists(local_tcp))
        self.assertFalse(os.path.lexists(local_unix))

    def test_refused_streamlocal_channel_does_not_break_transport(self):
        with self.assertRaises(paramiko.SSHException):
            mssh._open_streamlocal_channel(
                self.transport, self.remote_socket + ".missing", 3)
        self.assertTrue(self.transport.is_active())
        chan = mssh._open_streamlocal_channel(self.transport, self.remote_socket, 3)
        try:
            chan.sendall(b"still alive")
            self.assertEqual(chan.recv(11), b"still alive")
        finally:
            mssh._close_session_channel(chan)

    def test_late_success_closes_actual_remote_socket_after_timeout(self):
        from paramiko.common import MSG_CHANNEL_OPEN_SUCCESS

        class DetectClose(Echo):
            def handle(handler):
                try:
                    super(DetectClose, handler).handle()
                finally:
                    handler.server.done.set()

        self.unix.RequestHandlerClass = DetectClose
        entered, release = threading.Event(), threading.Event()
        original = self.transport._handler_table[MSG_CHANNEL_OPEN_SUCCESS]

        def delay(response):
            entered.set()
            release.wait(5)
            original(response)

        self.transport._handler_table[MSG_CHANNEL_OPEN_SUCCESS] = delay
        try:
            with self.assertRaisesRegex(paramiko.SSHException, "Timeout"):
                mssh._open_streamlocal_channel(
                    self.transport, self.remote_socket, 0.2)
            self.assertTrue(entered.is_set())
            self.assertEqual(len(self.transport._channels), 1)
            release.set()
            self.assertTrue(self.unix.done.wait(3), "remote socket was not closed")
            self.assertEqual(len(self.transport._channels), 0)
            self.assertEqual(self.transport.channel_events, {})
        finally:
            release.set()
            self.transport._handler_table[MSG_CHANNEL_OPEN_SUCCESS] = original
        self.assertTrue(self.transport.is_active())
        chan = mssh._open_streamlocal_channel(self.transport, self.remote_socket, 3)
        try:
            chan.sendall(b"alive")
            self.assertEqual(chan.recv(5), b"alive")
        finally:
            mssh._close_session_channel(chan)
