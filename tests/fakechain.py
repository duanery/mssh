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
    """A channel that starts its program on exec_command, not on __init__."""

    def __init__(self):
        self._timeout = None
        self.eof = False
        self.pid = None
        self.fd = None

    def get_pty(self, term=None, width=0, height=0):
        pass

    def exec_command(self, command):
        self.pid, self.fd = os.forkpty()
        if self.pid == 0:
            os.environ["TERM"] = "dumb"
            os.execvp("/bin/sh", ["/bin/sh", "-c", command])


def fake_build_chain(jumps, target, opts):
    client = FakeClient()
    return [client], client


mssh.build_chain = fake_build_chain

if __name__ == "__main__":
    sys.exit(mssh.main())
