#!/usr/bin/env python3
"""A serial-console channel for pty-bridge's transfer tests.

Usage: fake_serial.py COMMAND [ARG]...

Plays "virsh console": the bridge's pty (stdin/stdout) goes raw, COMMAND
runs on a pty of its own -- the guest's console -- and bytes are relayed
both ways. Like virsh it leaves at a raw ^] (0x1d) on stdin, printing
"ESCAPED" on stderr. The environment shapes the line:

  FS_CORRUPT_IN=N   flip the bits of the N-th byte towards COMMAND
  FS_CORRUPT_OUT=N  ... of the N-th byte coming from COMMAND
  FS_RATE=B         at most B bytes/s in each direction (a slow line)
  FS_DELAY=MS       every byte arrives MS milliseconds after it was sent,
                    either way (a far line); at most FS_QUEUE bytes
                    (default 1 MiB) are on the way in each direction.
                    Not with FS_RATE.
  SIGUSR1           the line goes dead: nothing is relayed any more, either
                    way (a SIGHUP still ends it)
  FS_LINGER=S       leave the way virsh does: the guest's console is not
                    hung up -- COMMAND keeps its pty for S more seconds,
                    its output read and dropped, after this exits
"""
import collections
import os
import pty
import select
import signal
import sys
import termios
import time
import tty


def linger(m, seconds):
    """Keep COMMAND's pty open from a process of its own, so this one
    can exit -- and be reaped -- without a hangup reaching COMMAND."""
    if os.fork() != 0:
        return
    os.setsid()
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([m], [], [], 0.2)
        if r:
            try:
                if not os.read(m, 4096):
                    break
            except OSError:
                break           # COMMAND is gone
    os._exit(0)


def relay_delayed(m, delay, cap, corrupt):
    """Relay stdin <-> m, each chunk held for delay seconds; corrupt:
    the byte to flip each way, as FS_CORRUPT_IN/OUT say."""
    lanes = [(0, m, collections.deque()), (m, 1, collections.deque())]
    held, count = [0, 0], [0, 0]
    while True:
        now = time.time()
        rd, wait = [], None
        for i, (src, dst, q) in enumerate(lanes):
            while q and q[0][0] <= now:
                _, d = q.popleft()
                held[i] -= len(d)
                try:
                    os.write(dst, d)
                except OSError:
                    return
            if q:
                left = q[0][0] - now
                wait = left if wait is None else min(wait, left)
            if held[i] < cap:
                rd.append(src)
        try:
            r, _, _ = select.select(rd, [], [], wait)
        except InterruptedError:
            continue
        for i, (src, dst, q) in enumerate(lanes):
            if src not in r:
                continue
            try:
                d = os.read(src, 65536)
            except OSError:
                return
            if not d:
                return
            if src == 0 and 0x1d in d:
                print('ESCAPED', file=sys.stderr, flush=True)
                return
            d = bytearray(d)
            if count[i] <= corrupt[i] < count[i] + len(d):
                d[corrupt[i] - count[i]] ^= 0xff
            count[i] += len(d)
            q.append((time.time() + delay, d))
            held[i] += len(d)


def main():
    corrupt_in = int(os.environ.get('FS_CORRUPT_IN', '-1'))
    corrupt_out = int(os.environ.get('FS_CORRUPT_OUT', '-1'))
    rate = int(os.environ.get('FS_RATE', '0'))
    keep = float(os.environ.get('FS_LINGER', '0'))
    delay = float(os.environ.get('FS_DELAY', '0')) / 1000
    pid, m = pty.fork()
    if pid == 0:
        os.execvp(sys.argv[1], sys.argv[1:])
    if keep:
        # the bridge hangs us up when it leaves: that must not end the
        # guest side, so the relay notices the closed pty and lingers
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGUSR1, lambda *_: stalled.append(1))
    stalled = []
    tty.setraw(0, termios.TCSANOW)
    print('Escape character is ^]', file=sys.stderr, flush=True)
    nin = nout = 0
    chunk = max(1, rate // 50) if rate else 4096
    if delay:
        relay_delayed(m, delay, int(os.environ.get('FS_QUEUE', 1 << 20)),
                      (corrupt_in, corrupt_out))
    while not delay:
        if stalled:
            time.sleep(1)
            continue
        try:
            r, _, _ = select.select([0, m], [], [])
        except InterruptedError:
            continue
        t0 = time.time()
        if 0 in r:
            try:
                d = bytearray(os.read(0, chunk))
            except OSError:
                break           # the bridge closed its end
            if not d:
                break
            if 0x1d in d:
                print('ESCAPED', file=sys.stderr, flush=True)
                break
            if nin <= corrupt_in < nin + len(d):
                d[corrupt_in - nin] ^= 0xff
            nin += len(d)
            os.write(m, d)
        if m in r:
            try:
                d = bytearray(os.read(m, chunk))
            except OSError:
                break
            if not d:
                break
            if nout <= corrupt_out < nout + len(d):
                d[corrupt_out - nout] ^= 0xff
            nout += len(d)
            try:
                os.write(1, d)
            except OSError:
                break
        if rate:
            time.sleep(max(0.0, chunk / rate - (time.time() - t0)))
    if keep:
        linger(m, keep)


main()
