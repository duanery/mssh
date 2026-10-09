#!/usr/bin/env python3.12
"""Run mssh's main() with build_chain replaced by a local pty.

Everything above the chain -- the socket, the daemon, the protocol, the
framing, the client -- is exercised for real; only the SSH hops below it are
faked, so these tests need no sshd and no network.  Invoked as a separate
process by test_session_daemon.py, because "state survives across processes"
is the whole point of the feature and cannot be tested in one.
"""

import importlib.util
import os
import select
import socket
import subprocess
import sys
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
MSSH = os.path.join(os.path.dirname(HERE), "mssh")

spec = importlib.util.spec_from_loader("mssh", SourceFileLoader("mssh", MSSH))
mssh = importlib.util.module_from_spec(spec)
sys.modules["mssh"] = mssh
spec.loader.exec_module(mssh)

sys.path.insert(0, HERE)
from test_session_unit import FakeChan          # noqa: E402


class FakeTransport(object):
    def open_session(self, timeout=None):
        return _PtyChan()

    def set_keepalive(self, interval):
        pass


class FakeClient(object):
    def get_transport(self):
        return FakeTransport()

    def close(self):
        pass


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
        self.proc = None                  # set when running without a pty
        self.closed = False

    def get_pty(self, term=None, width=0, height=0):
        self.want_pty = True

    def exec_command(self, command):
        if self.want_pty:
            self.pid, self.fd = os.forkpty()
            if self.pid == 0:
                os.environ["TERM"] = "dumb"
                os.execvp("/bin/sh", ["/bin/sh", "-c", command])
            return
        # No pty: pipes, so shutdown_write() below is a genuine EOF.
        self.proc = subprocess.Popen(
            ["/bin/sh", "-c", command], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # -- the pipe-backed half, used by the stdin writer channel -----------

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
        return bool(select.select([self.fileno()], [], [], 0)[0])

    def sendall(self, data):
        if self.proc is None:
            return FakeChan.sendall(self, data)
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def send(self, data):
        """Partial sends are the real thing's behaviour; a pipe takes it all."""
        self.sendall(data)
        return len(data)

    def recv(self, size):
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
        if self.proc is not None:
            return self.proc.poll() is not None
        return FakeChan.exit_status_ready(self)

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
    client = FakeClient()
    return [client], client


mssh.build_chain = fake_build_chain

if __name__ == "__main__":
    sys.exit(mssh.main())
