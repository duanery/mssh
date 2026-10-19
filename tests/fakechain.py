#!/usr/bin/env python3.12
"""Run mssh's main() with build_chain replaced by a local pty.

Everything above the chain -- the socket, the daemon, the protocol, the
framing, the client -- is exercised for real; only the SSH hops below it are
faked, so these tests need no sshd and no network.  Invoked as a separate
process by test_session_daemon.py, because "state survives across processes"
is the whole point of the feature and cannot be tested in one.
"""

import fcntl
import importlib.util
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
MSSH = os.path.join(os.path.dirname(HERE), "mssh")

spec = importlib.util.spec_from_loader("mssh", SourceFileLoader("mssh", MSSH))
mssh = importlib.util.module_from_spec(spec)
sys.modules["mssh"] = mssh
spec.loader.exec_module(mssh)

sys.path.insert(0, HERE)
from test_session_unit import FakeChan          # noqa: E402


# A link that can be taken away, which is what --reconnect is about.  While
# the named file exists the chain cannot be built and every channel on an
# existing one fails the way paramiko's do when its transport dies: reads
# raise, and the channel reports itself finished.  Creating and removing the
# file is a test standing in for a machine going down and coming back.
DOWN = os.environ.get("MSSH_FAKE_DOWN")


def link_up():
    return not (DOWN and os.path.exists(DOWN))


class FakeTransport(object):
    def __init__(self, client):
        self.client = client
        self.reverse_managers = []

    def open_session(self, timeout=None):
        if not link_up():
            raise OSError("fake chain: the connection is down")
        chan = _PtyChan()
        self.client.channels.append(chan)
        return chan

    def is_active(self):
        return link_up() and not self.client.shut

    def set_keepalive(self, interval):
        pass

    def open_channel(self, kind, dest_addr, src_addr=None, timeout=None):
        if kind not in ("direct-tcpip", "direct-streamlocal@openssh.com"):
            raise ValueError("unknown channel kind")
        if not self.is_active():
            raise OSError("fake chain: the connection is down")
        if kind == "direct-tcpip":
            sock = socket.create_connection(dest_addr, timeout=timeout)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout)
                sock.connect(dest_addr)
            except BaseException:
                sock.close()
                raise
        chan = _TCPChan(sock)
        self.client.channels.append(chan)
        return chan


class _TCPChan(object):
    """A channel-shaped TCP/Unix socket for local-forward integration tests."""

    def __init__(self, sock):
        self.sock = sock
        self.closed = False
        self.eof_received = False

    def fileno(self):
        return self.sock.fileno()

    def settimeout(self, timeout):
        self.sock.settimeout(timeout)

    def send(self, data):
        try:
            return self.sock.send(data)
        except BlockingIOError:
            raise socket.timeout()

    def recv_ready(self):
        return bool(select.select([self.sock], [], [], 0)[0])

    def recv(self, size):
        data = self.sock.recv(size)
        if not data:
            self.eof_received = True
        return data

    def shutdown_write(self):
        self.sock.shutdown(socket.SHUT_WR)

    def close(self):
        self.closed = True
        self.sock.close()


class FakeClient(object):
    def __init__(self):
        self.channels = []
        self.shut = False
        self.transport = FakeTransport(self)

    def get_transport(self):
        return self.transport

    def close(self):
        """Take the chain's channels with it, as closing a transport does.

        A reconnect closes the chain it replaced, and without this the
        program behind the old pty would outlive it: a stray /bin/sh per
        attempt, still holding the fd the test is about to check.
        """
        self.shut = True
        for manager in self.transport.reverse_managers:
            manager.close()
        for chan in self.channels:
            try:
                chan.close()
            except Exception:
                pass
        del self.channels[:]


class _FakeRemoteForwardManager(object):
    """Fake only sshd's listener; actual remote jobs and relays run unchanged.

    Protocol packets are tested against Paramiko and a real OpenSSH fixture.
    These sockets instead let separate CLI processes test daemon ownership,
    cancellation and rebind without needing credentials or a network host.
    """

    endpoint = staticmethod(mssh._RemoteForwardManager.endpoint)

    def __init__(self, transport, timeout):
        self.transport = transport
        self.lock = threading.Lock()
        self.entries = {}
        transport.reverse_managers.append(self)

    def bind(self, job):
        if not self.transport.is_active():
            raise OSError("fake chain: the connection is down")
        endpoint = self.endpoint(job.settings)
        family = socket.AF_UNIX if endpoint[0] == "unix" else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        try:
            if family == socket.AF_UNIX:
                address = endpoint[1]
            else:
                address = ("127.0.0.1" if endpoint[1] == "localhost" else
                           endpoint[1], endpoint[2])
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(address)
            listener.listen(16)
            listener.settimeout(0.1)
            with self.lock:
                if endpoint in self.entries:
                    raise ValueError("remote listener is already configured")
                self.entries[endpoint] = (job, listener)
            worker = threading.Thread(target=self.accept, args=(job, listener))
            worker.daemon = True
            worker.start()
        except BaseException:
            with self.lock:
                if self.entries.get(endpoint) == (job, listener):
                    self.entries.pop(endpoint)
            listener.close()
            raise

    def accept(self, job, listener):
        try:
            while self.transport.is_active():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                chan = _TCPChan(conn)
                self.transport.client.channels.append(chan)
                if job.accepting(self.transport):
                    job.incoming(chan, self.transport)
                else:
                    chan.close()
        except OSError:
            pass
        finally:
            self.cancel(job)

    def cancel(self, job):
        with self.lock:
            entry = self.entries.get(self.endpoint(job.settings))
            if entry is None or entry[0] is not job:
                return
            self.entries.pop(self.endpoint(job.settings))
        try:
            entry[1].shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        entry[1].close()

    def close(self):
        with self.lock:
            jobs = [entry[0] for entry in self.entries.values()]
        for job in jobs:
            self.cancel(job)


mssh._RemoteForwardManager = _FakeRemoteForwardManager


class _PtyChan(FakeChan):
    """A channel that starts its program on exec_command, not on __init__.

    A pty is allocated only when get_pty() is asked for, as a real channel does.
    That distinction matters for the stdin writer channel: it gets no pty, so its
    stdin is a plain pipe whose close is a real EOF -- through a pty it would be
    line-disciplined instead, and the remote 'cat' would never see end of input.
    """

    def __init__(self):
        self._timeout = None
        self.eof = False
        self.pid = None
        self.fd = None
        self.want_pty = False
        self.term = "dumb"
        self.size = (0, 0)
        self.status = None                # the pty child's, once reaped
        self.proc = None                  # set when running without a pty
        self.closed = False

    def get_pty(self, term=None, width=0, height=0):
        """Record what was asked for, as a server would act on it.

        --exec -t carries the client's own terminal type and window size, so a
        fake that dropped them would let a wrong size pass unnoticed; the size
        is put on the pty below, where the remote program can read it.
        """
        self.want_pty = True
        self.term = term or "dumb"
        self.size = (int(width or 0), int(height or 0))

    def resize_pty(self, width=0, height=0, width_pixels=0, height_pixels=0):
        """The client's window changed; a real server puts it on the pty.

        Doing the ioctl here is what makes the resize observable at all: the
        remote program learns its new size by asking the terminal, so a fake
        that only recorded the request would let a size that never arrived
        pass for one that did.
        """
        self.size = (int(width or 0), int(height or 0))
        self._set_size()
        try:
            os.kill(self.pid, signal.SIGWINCH)
        except (OSError, TypeError):
            pass

    def _set_size(self):
        cols, rows = self.size
        if self.fd is None or not (cols and rows):
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def invoke_shell(self):
        """A login: the shell itself, which is what 'mssh host' asks for.

        Started the same way a command is, so the whole pty path below -- the
        size, the window change, the exit status -- is the one path either
        way; only the program differs, as it does on a real server.
        """
        self.exec_command("exec /bin/sh")

    def invoke_subsystem(self, name):
        if name != "sftp":
            raise ValueError("unknown subsystem")
        if os.environ.get("MSSH_FAKE_NO_SFTP"):
            raise RuntimeError("target has no sftp subsystem")
        self.exec_command("exec /usr/libexec/openssh/sftp-server")

    def exec_command(self, command):
        if self.want_pty:
            self.pid, self.fd = os.forkpty()
            if self.pid == 0:
                os.environ["TERM"] = self.term
                os.execvp("/bin/sh", ["/bin/sh", "-c", command])
            self._set_size()
            return
        # No pty: pipes, so shutdown_write() below is a genuine EOF.
        self.proc = subprocess.Popen(
            ["/bin/sh", "-c", command], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # -- the pipe-backed half, used by the stdin writer channel -----------

    def _link(self):
        """Fail the way a channel does once its transport has gone."""
        if not link_up():
            raise OSError("fake chain: the connection is down")

    def fileno(self):
        """The fd to select on, which is where the program's output comes out.

        The daemon watches the channel together with the client's stdin, the
        output pipe and the request socket, so a fake has to be selectable the
        way a real one is.
        """
        if self.proc is None:
            return self.fd
        return self.proc.stdout.fileno()

    def recv_ready(self):
        if not link_up():
            return True               # a dead channel is always "ready": EOF
        return bool(select.select([self.fileno()], [], [], 0)[0])

    def sendall(self, data):
        self._link()
        if self.proc is None:
            return FakeChan.sendall(self, data)
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def send(self, data):
        """Partial sends are the real thing's behaviour; a pipe takes it all."""
        self.sendall(data)
        return len(data)

    def recv(self, size):
        self._link()
        if self.proc is None:
            return FakeChan.recv(self, size)
        ready, _, _ = select.select([self.proc.stdout], [], [], self._timeout)
        if not ready:
            raise socket.timeout()
        data = self.proc.stdout.read1(size) if hasattr(self.proc.stdout, "read1") \
            else self.proc.stdout.read(size)
        if not data:
            self.eof = True
        return data

    def recv_stderr_ready(self):
        if self.proc is None:
            return False
        return bool(select.select([self.proc.stderr], [], [], 0)[0])

    def recv_stderr(self, size):
        if self.proc is None:
            return b""
        return self.proc.stderr.read1(size)

    def shutdown_write(self):
        if self.proc is not None and self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()

    def exit_status_ready(self):
        if not link_up():
            # paramiko sets the status event on every channel when the
            # transport goes, so a dead link looks exactly like this.
            return True
        if self.proc is not None:
            return self.proc.poll() is not None
        return FakeChan.exit_status_ready(self)

    def recv_exit_status(self):
        """The command's exit code, as a real channel reports it.

        What --exec frames on: a command running on its own channel has no
        marker printed into its output, so the channel itself is what says how
        it ended.
        """
        if not link_up():
            return -1
        if self.proc is not None:
            return self.proc.wait()
        if self.pid is None:
            return -1
        if self.status is None:
            try:
                _pid, raw = os.waitpid(self.pid, 0)
            except OSError:
                return -1
            self.status = raw >> 8 if raw & 0xFF == 0 else 128 + (raw & 0x7F)
        return self.status

    def close(self):
        self.closed = True
        if self.proc is not None:
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    if stream and not stream.closed:
                        stream.close()
                except Exception:
                    pass
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
            return
        FakeChan.close(self)


def fake_build_chain(jumps, target, opts):
    if not link_up():
        raise SystemExit("mssh: hop 1/1: cannot reach %s: fake chain is down"
                         % target)
    client = FakeClient()
    return [client], client


mssh.build_chain = fake_build_chain
mssh._open_streamlocal_channel = lambda transport, path, timeout: \
    transport.open_channel("direct-streamlocal@openssh.com", path, timeout=timeout)

if __name__ == "__main__":
    sys.exit(mssh.main())
