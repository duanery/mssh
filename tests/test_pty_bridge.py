#!/usr/bin/env python3
"""Test pty-bridge: bidirectional pty forwarding, raw mode, escape exit.

Run: python3 tests/test_pty_bridge.py
"""
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, '..', 'pty-bridge')

TIOCSWINSZ = termios.TIOCSWINSZ

failures = 0


def set_winsize(master_fd, rows, cols):
    """Set the pty window size (the bridge child gets SIGWINCH)."""
    fcntl.ioctl(master_fd, TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))


def open_target():
    """Create the target pty the way the session behind it would have
    it: raw, configured for byte relaying. --pty attaches to a working
    session and must not touch its settings, so the tests provide the
    working session."""
    m, s = pty.openpty()
    attrs = termios.tcgetattr(s)
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
    iflag &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK |
               termios.ISTRIP | termios.IXON)
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN |
               termios.ISIG)
    cc[termios.VMIN] = 1
    cc[termios.VTIME] = 0
    termios.tcsetattr(s, termios.TCSANOW,
                      [iflag, oflag, cflag, lflag, ispeed, ospeed, cc])
    return m, s


def check(name, cond, detail=''):
    global failures
    print(('PASS' if cond else 'FAIL'), name, '' if cond else detail)
    if not cond:
        failures += 1


def read_avail(fd, timeout=1.0):
    """Read whatever is available on fd within timeout."""
    out = b''
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                d = os.read(fd, 4096)
            except OSError:
                break
            if not d:
                break
            out += d
    return out


def wait_pid_exit(pid, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        p, status = os.waitpid(pid, os.WNOHANG)
        if p == pid:
            return status
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    return os.waitpid(pid, 0)[1]


def build():
    """Compile pty_bridge if the binary is missing."""
    if os.path.exists(BIN):
        return
    subprocess.check_call(
        ['gcc', '-Wall', '-Wextra', '-O2', '-o', BIN, BIN + '.c'])


build()

# --- the pty given to pty_bridge as its /dev/pts/N argument ---
target_master, target_slave = open_target()
target_name = os.ttyname(target_slave)

# --- spawn pty_bridge under its own pty, so stdin is a controlling terminal ---
pid, local_master = pty.fork()
if pid == 0:
    os.execv(BIN, ['pty-bridge', '-e', '^]', '--pty', target_name])

banner = read_avail(local_master, 0.5)
check('escape banner printed', b'Escape character is ^]' in banner, repr(banner))

# 1) keyboard -> target: write on the local pty master, expect on the target
# (the leading \r is the simulated Enter pressed before the main loop)
os.write(local_master, b'hello bridge\n')
got = read_avail(target_master)
check('stdin->pty forwarding', got == b'\rhello bridge\n', repr(got))

# 2) target -> screen: write on the target master, expect on the local terminal
os.write(target_master, b'back out\n')
got = read_avail(local_master)
check('pty->stdout forwarding', b'back out' in got, repr(got))

# 3) raw mode both ways: ^C and ^D must arrive as plain bytes
os.write(local_master, b'\x03\x04')
got = read_avail(target_master)
check('raw passthrough of ^C/^D', got == b'\x03\x04', repr(got))

# 4) bytes received before the escape character are still forwarded
os.write(local_master, b'tail\x1d')  # 'tail' then Ctrl-]
got = read_avail(target_master)
check('bytes before escape forwarded', got == b'tail', repr(got))

# 5) escape ^] exits 0 and emits a newline so the shell prompt starts fresh
status = wait_pid_exit(pid)
detached = read_avail(local_master, 0.5)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('escape exits 0', ok, repr(status))
check('newline on detach', b'\r\n' in detached, repr(detached))

# 6) stdin not a tty -> refuse with a nonzero exit
r, w = os.pipe()
p2 = os.fork()
if p2 == 0:
    os.dup2(r, 0)
    os.dup2(w, 2)
    os.close(r)
    os.close(w)
    os.execv(BIN, ['pty-bridge', '--pty', target_name])
os.close(w)                      # parent reads the child's stderr from r
status = wait_pid_exit(p2)
err = read_avail(r, 0.5)
os.close(r)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) != 0
check('non-tty stdin refused', ok, repr(status) + ' err=' + repr(err))
check('non-tty stdin message', b'not a terminal' in err, repr(err))

# 7) non-tty argument -> refuse with exit code 2
pid3, m3 = pty.fork()
if pid3 == 0:
    os.execv(BIN, ['pty-bridge', '--pty', '/etc/passwd'])
out3 = read_avail(m3, 0.8)
status = wait_pid_exit(pid3)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2
check('non-tty arg refused', ok, repr(status) + ' out=' + repr(out3))

# 8) target pty closed by its master -> pty_bridge exits
pid4, m4 = pty.fork()
if pid4 == 0:
    os.execv(BIN, ['pty-bridge', '-e', '^a', '--pty', target_name])
time.sleep(0.3)
read_avail(m4, 0.2)
os.close(target_master)
status = wait_pid_exit(pid4)
check('pts closed -> exit', status is not None and os.WIFEXITED(status), repr(status))

# 9) alternate escape character (-e ^a = 0x01)
tm2, ts2 = open_target()
pid5, m5 = pty.fork()
if pid5 == 0:
    os.execv(BIN, ['pty-bridge', '-e', '^a', '--pty', os.ttyname(ts2)])
time.sleep(0.3)
read_avail(m5, 0.2)
os.write(m5, b'\x01')
status = wait_pid_exit(pid5)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('custom escape ^a exits', ok, repr(status))

# 10) -p pattern: prefix appearing in the output auto-types the reply + Enter
tm3, ts3 = open_target()
pid6, m6 = pty.fork()
if pid6 == 0:
    os.execv(BIN, ['pty-bridge', '-p', 'login: root', '--pty', os.ttyname(ts3)])
time.sleep(0.3)
read_avail(m6, 0.3)
read_avail(tm3, 0.3)    # drain the simulated startup Enter
os.write(tm3, b'welcome\nlogin: ')
got = read_avail(tm3)
check('pattern auto-reply', got == b'root\r', repr(got))

# 11) each pattern fires only once
os.write(tm3, b'login: ')
got = read_avail(tm3, 0.5)
check('pattern used only once', got == b'', repr(got))

# 11b) exact tail compare: prompt without its trailing space never fires
tm5, ts5 = open_target()
pid9, m9 = pty.fork()
if pid9 == 0:
    os.execv(BIN, ['pty-bridge', '-p', 'login: root', '--pty', os.ttyname(ts5)])
time.sleep(0.3)
read_avail(m9, 0.3)
read_avail(tm5, 0.3)    # drain the simulated startup Enter
os.write(tm5, b'login:')  # no trailing space: not the full prompt
got = read_avail(tm5, 0.5)
check('incomplete prompt not matched', got == b'', repr(got))
os.write(tm5, b' ')       # now the full "login: " tail is there
got = read_avail(tm5)
check('full prompt with space matched', got == b'root\r', repr(got))
os.write(m9, b'\x1d')
status = wait_pid_exit(pid9)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('escape exits after exact match', ok, repr(status))

# 12) still interactive after the pattern fired; escape still works
os.write(tm3, b'\nPassword: ')
got = read_avail(tm3, 0.3)
check('no reply for unmatched prompt', got == b'', repr(got))
os.write(m6, b'bye\x1d')  # 'bye' forwarded, Ctrl-] exits
got = read_avail(tm3)
status = wait_pid_exit(pid6)
check('forwarding after pattern fire', got == b'bye', repr(got))
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('escape exits after pattern fire', ok, repr(status))

# 13) two patterns, each used once, in either order of arrival
tm4, ts4 = open_target()
pid7, m7 = pty.fork()
if pid7 == 0:
    os.execv(BIN, ['pty-bridge', '-p', 'login: root', '-p', 'Password: secret',
                   '--pty', os.ttyname(ts4)])
time.sleep(0.3)
read_avail(m7, 0.3)
read_avail(tm4, 0.3)    # drain the simulated startup Enter
os.write(tm4, b'Password: ')
got = read_avail(tm4)
check('second pattern auto-reply', got == b'secret\r', repr(got))
os.write(tm4, b'\nlogin: ')
got = read_avail(tm4)
check('first pattern auto-reply later', got == b'root\r', repr(got))
os.write(m7, b'\x1d')
status = wait_pid_exit(pid7)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('escape exits with patterns', ok, repr(status))

# 13b) the reply keeps the spaces it has: -p ']# cd /tmp' runs "cd /tmp"
#      (the split is at the FIRST space, so only the prompt is cut short)
tm9, ts9 = open_target()
pid19, m19 = pty.fork()
if pid19 == 0:
    os.execv(BIN, ['pty-bridge', '-p', ']# cd /tmp', '--pty', os.ttyname(ts9)])
time.sleep(0.3)
read_avail(m19, 0.3)
read_avail(tm9, 0.3)    # drain the simulated startup Enter
os.write(tm9, b'[root@host ~]# ')
got = read_avail(tm9)
check('reply keeps its spaces', got == b'cd /tmp\r', repr(got))
os.write(m19, b'\x1d')
status = wait_pid_exit(pid19)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('escape exits after spaced reply', ok, repr(status))

# 14) malformed -p value -> exit 2
pid8 = os.fork()
if pid8 == 0:
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.execv(BIN, ['pty-bridge', '-p', 'nospace', '--pty', '/dev/pts/1'])
status = wait_pid_exit(pid8)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2
check('malformed pattern refused', ok, repr(status))

# 14b) two sentinel patterns -> exit 2
pid12 = os.fork()
if pid12 == 0:
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.execv(BIN, ['pty-bridge', '-p', ']# ', '-p', '$ ',
                   '--pty', '/dev/pts/1'])
status = wait_pid_exit(pid12)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2
check('second sentinel refused', ok, repr(status))

# 15) --term serial + sentinel (empty reply): the sentinel fires on the
#     command prompt, types nothing by itself, retires every still-unused
#     pattern, and the first window-size push carries the one-time TERM
#     export together with the stty command
tm6, ts6 = open_target()
pid10, m10 = pty.fork()
if pid10 == 0:
    os.execve(BIN, ['pty-bridge', '--term', 'serial', '-p', 'login: root',
                    '-p', ']# ', '--pty', os.ttyname(ts6)],
              dict(os.environ, TERM='xterm-test'))
set_winsize(m10, 40, 100)
time.sleep(0.3)
read_avail(m10, 0.3)
got = read_avail(tm6, 0.3)    # serial keeps the simulated startup Enter
check('serial: startup Enter typed', got == b'\r', repr(got))
# shell prompt arrives first (session was already logged in):
# sentinel fires, types nothing but the TERM export + stty sequence, and
# the login prompt is retired
os.write(tm6, b'Last login: ...\n[root@28 ~]# ')
got = read_avail(tm6, 0.5)
check('sentinel pushes TERM+winsize init',
      got == b'export TERM=xterm-test; stty rows 40 columns 100\r', repr(got))

# 16) sitting at the prompt: a window resize is pushed immediately
set_winsize(m10, 50, 120)   # SIGWINCH -> immediate sync
got = read_avail(tm6, 0.5)
check('resize pushed immediately at prompt',
      got == b'stty rows 50 columns 120\r', repr(got))
# next prompt without a new SIGWINCH: nothing is pushed
os.write(tm6, b'\n[root@28 ~]# ')
got = read_avail(tm6, 0.5)
check('no resize without window change', got == b'', repr(got))

# 17) output streaming clears the at-prompt state: a resize is deferred
#     to the next prompt instead of injecting stty into running output;
#     the login prompt stays retired meanwhile
os.write(tm6, b'login: ')
got = read_avail(tm6, 0.5)
check('sentinel retires unused patterns', got == b'', repr(got))
set_winsize(m10, 60, 130)   # not at the prompt: nothing pushed yet
got = read_avail(tm6, 0.5)
check('resize deferred while not at prompt', got == b'', repr(got))
os.write(tm6, b'\n[root@28 ~]# ')
got = read_avail(tm6, 0.5)
check('deferred resize pushed at next prompt',
      got == b'stty rows 60 columns 130\r', repr(got))
os.write(m10, b'\x1d')
status = wait_pid_exit(pid10)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('escape exits after sentinel', ok, repr(status))

# 17) normal flow: login prompt fires first, then the sentinel stops
#     auto-replying and initializes TERM plus the window size
tm7, ts7 = open_target()
pid11, m11 = pty.fork()
if pid11 == 0:
    os.execve(BIN, ['pty-bridge', '--term', 'serial', '-p', 'login: root',
                    '-p', ']# ', '--pty', os.ttyname(ts7)],
              dict(os.environ, TERM='xterm-test'))
set_winsize(m11, 30, 90)
time.sleep(0.3)
read_avail(m11, 0.3)
read_avail(tm7, 0.3)
os.write(tm7, b'login: ')
got = read_avail(tm7)
check('login fires before sentinel', got == b'root\r', repr(got))
os.write(tm7, b'\nmotd\n]# ')
got = read_avail(tm7, 0.5)
check('sentinel pushes TERM+winsize after login',
      got == b'export TERM=xterm-test; stty rows 30 columns 90\r', repr(got))
os.write(m11, b'\x1d')
status = wait_pid_exit(pid11)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('escape exits after login+sentinel', ok, repr(status))

# 18) command mode: without --pty a new pty is created and COMMAND runs
#     on its slave as the channel to a (remote) pty. A pty channel gets
#     no startup phase: the main loop starts immediately, and output the
#     command prints before it puts the pty into raw mode (its \n already
#     expanded to \r\n by the still-canonical line discipline) is
#     forwarded and displayed just the same.
pid13, m13 = pty.fork()
if pid13 == 0:
    os.execv(BIN, ['pty-bridge', '--',
                   'sh', '-c', 'printf "banner\n"; stty raw -echo; cat'])
got = read_avail(m13, 1.0)
check('early output forwarded before pty is raw',
      b'banner' in got and b'\r\n' in got, repr(got))
time.sleep(0.3)
os.write(m13, b'meow\n')
got = read_avail(m13)
check('command mode forwards to child pty', b'meow' in got, repr(got))
os.write(m13, b'\x1d')
status = wait_pid_exit(pid13)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('command mode escape exits 0', ok, repr(status))

# 18b) a command that never sets the pty raw and never exits: a pty
#      channel has no startup phase to get stuck in, so forwarding
#      (including the escape character) starts immediately
pid13b, m13b = pty.fork()
if pid13b == 0:
    os.execv(BIN, ['pty-bridge', '--', 'cat'])
time.sleep(0.5)
read_avail(m13b, 0.3)
os.write(m13b, b'\x1d')
status = wait_pid_exit(pid13b)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('no startup wait for a pty channel', ok, repr(status))

# 19) the channel command starts with the local window size (so e.g.
#     ssh -tt relays the right size to the remote pty from the start)
pid14, m14 = pty.fork()
if pid14 == 0:
    os.execv(BIN, ['pty-bridge', '--', 'sh', '-c', 'sleep 0.5; stty size'])
set_winsize(m14, 24, 80)
got = read_avail(m14, 3.0)
check('command starts with local winsize', b'24 80' in got, repr(got))
wait_pid_exit(pid14)

# 19b) the new pty is seeded with the local terminal's termios, not
#      with the driver defaults: ECHO is off on the terminal we hand
#      to pty-bridge, so the channel command must see ECHO off too
lm, ls = pty.openpty()
attrs = termios.tcgetattr(ls)
attrs[3] &= ~termios.ECHO
termios.tcsetattr(ls, termios.TCSANOW, attrs)
pid14b = os.fork()
if pid14b == 0:
    os.close(lm)
    os.setsid()
    fcntl.ioctl(ls, termios.TIOCSCTTY, 0)
    os.dup2(ls, 0)
    os.dup2(ls, 1)
    os.dup2(ls, 2)
    os.close(ls)
    os.execv(BIN, ['pty-bridge', '--', 'sh', '-c', 'stty -a; sleep 5'])
os.close(ls)
got = read_avail(lm, 4.5)
check('command pty inherits local termios', b'-echo' in got, repr(got))
os.write(lm, b'\x1d')
wait_pid_exit(pid14b)
os.close(lm)

# 20) the channel command exits -> pty-bridge exits by itself
#     (master read fails with EIO once the slave is closed)
pid15, m15 = pty.fork()
if pid15 == 0:
    os.execv(BIN, ['pty-bridge', '--', 'true'])
status = wait_pid_exit(pid15)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('child exit closes the bridge', ok, repr(status))

# 21) exec failure: the child prints the error and the bridge exits
pid16, m16 = pty.fork()
if pid16 == 0:
    os.execv(BIN, ['pty-bridge', '--', 'no-such-command-xyz'])
got = read_avail(m16, 2.0)
status = wait_pid_exit(pid16)
ok = status is not None and os.WIFEXITED(status) and b'No such file' in got
check('exec failure exits the bridge', ok, repr(status) + ' out=' + repr(got))

# 22) COMMAND combined with --pty -> refused with exit code 2
pid17, m17 = pty.fork()
if pid17 == 0:
    os.execv(BIN, ['pty-bridge', '--pty', '/dev/pts/1', 'cat'])
out = read_avail(m17, 0.8)
status = wait_pid_exit(pid17)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2
check('--pty with COMMAND refused', ok, repr(status) + ' out=' + repr(out))

# 23) without a sentinel there is no prompt boundary to wait for: a
#     window resize is pushed into the pty via TIOCSWINSZ (SIGWINCH to
#     the session behind it), no stty typing involved
tm8, ts8 = open_target()
pid18, m18 = pty.fork()
if pid18 == 0:
    os.execv(BIN, ['pty-bridge', '--pty', os.ttyname(ts8)])
time.sleep(0.3)
read_avail(m18, 0.3)
read_avail(tm8, 0.3)    # drain the simulated startup Enter
set_winsize(m18, 33, 77)
got = read_avail(tm8, 0.5)
check('no sentinel: resize types nothing', got == b'', repr(got))
rows, cols, _, _ = struct.unpack(
    'HHHH', fcntl.ioctl(tm8, termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0)))
check('no sentinel: pty size synced via ioctl', (rows, cols) == (33, 77),
      'got %dx%d' % (rows, cols))
os.write(m18, b'\x1d')
status = wait_pid_exit(pid18)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('no sentinel: escape still exits', ok, repr(status))

# 24) --term serial without a sentinel: no prompt boundary to type at,
#     so it warns and falls back to the pty method (TIOCSWINSZ, nothing
#     typed)
tm10, ts10 = open_target()
pid20, m20 = pty.fork()
if pid20 == 0:
    os.execv(BIN, ['pty-bridge', '--term', 'serial', '--pty', os.ttyname(ts10)])
time.sleep(0.3)
early = read_avail(m20, 0.3)
check('serial without sentinel warns', b'needs a sentinel pattern' in early,
      repr(early))
read_avail(tm10, 0.3)    # drain the simulated startup Enter
set_winsize(m20, 44, 88)
got = read_avail(tm10, 0.5)
check('serial without sentinel types nothing', got == b'', repr(got))
rows, cols, _, _ = struct.unpack(
    'HHHH', fcntl.ioctl(tm10, termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0)))
check('serial without sentinel: size synced via ioctl', (rows, cols) == (44, 88),
      'got %dx%d' % (rows, cols))
os.write(m20, b'\x1d')
wait_pid_exit(pid20)

# 25) auto: a "virsh console" COMMAND is recognized as a serial console
#     channel -- with a sentinel, the first prompt carries the one-time
#     TERM export and the stty push (a fake virsh plays the channel).
#     The fake plays the console faithfully: raw as soon as it connects,
#     and silent -- the prompt appears only in answer to an Enter
#     (swallowed_enters: how many Enters the console ignores first)
with tempfile.TemporaryDirectory() as tmpdir:
    def fake_virsh_script(swallowed_enters):
        s = '#!/bin/sh\nstty raw -echo\n'
        s += 'dd bs=1 count=1 >/dev/null 2>&1\n' * swallowed_enters
        s += "printf ']# '\ncat\n"
        return s

    fake_virsh = os.path.join(tmpdir, 'virsh')
    with open(fake_virsh, 'w') as f:
        f.write(fake_virsh_script(1))
    os.chmod(fake_virsh, 0o755)
    pid21, m21 = pty.fork()
    if pid21 == 0:
        os.execve(BIN, ['pty-bridge', '-p', ']# ', '--',
                        fake_virsh, 'console', 'vm7'],
                  dict(os.environ, TERM='xterm-test'))
    set_winsize(m21, 25, 75)
    got = read_avail(m21, 2.0)
    check('auto: virsh console uses the serial method',
          b'export TERM=xterm-test; stty rows 25 columns 75\r' in got, repr(got))
    os.write(m21, b'\x1d')
    wait_pid_exit(pid21)

    # 25b) the console ignores the first Enter: the startup phase types
    #      another one after ENTER_RETRY_SEC, and only then does the
    #      prompt (and with it the serial method) come out. The fake
    #      lives in a subdirectory: auto detection matches on the
    #      command's basename, which must stay "virsh"
    os.makedirs(os.path.join(tmpdir, 'v2'))
    fake_virsh2 = os.path.join(tmpdir, 'v2', 'virsh')
    with open(fake_virsh2, 'w') as f:
        f.write(fake_virsh_script(2))
    os.chmod(fake_virsh2, 0o755)
    pid21b, m21b = pty.fork()
    if pid21b == 0:
        os.execve(BIN, ['pty-bridge', '-p', ']# ', '--',
                        fake_virsh2, 'console', 'vm7'],
                  dict(os.environ, TERM='xterm-test'))
    set_winsize(m21b, 25, 75)
    got = read_avail(m21b, 5.0)
    check('serial: silent console poked again after the retry',
          b'export TERM=xterm-test; stty rows 25 columns 75\r' in got, repr(got))
    os.write(m21b, b'\x1d')
    wait_pid_exit(pid21b)

    # 25c) the Enter waits for the raw switch: a command MAY make the
    #      switch with tcsetattr(TCSAFLUSH) -- the flushing variant,
    #      which discards input still pending unread -- and nothing
    #      promises it doesn't, so an Enter typed before the switch
    #      could be dropped by the very switch meant to deliver it.
    #      This fake console plays exactly that variant: it inspects
    #      its input queue right before switching (after a grace
    #      period, so an overeager bridge has every chance to type),
    #      and nothing may be sitting there. The byte it reads right
    #      after the switch is the poke Enter, and the prompt it
    #      prints in answer completes the serial flow (sentinel fires
    #      -> TERM export + stty push, relayed back)
    fake_console = os.path.join(tmpdir, 'console.py')
    with open(fake_console, 'w') as f:
        f.write(r'''
import fcntl, os, struct, sys, termios, time

time.sleep(0.5)              # the grace period before the switch
n = struct.unpack('i', fcntl.ioctl(0, termios.TIOCINQ,
                                   struct.pack('i', 0)))[0]
print('pre-raw input: %d byte(s)' % n)   # still canonical: \n -> \r\n
sys.stdout.flush()
a = termios.tcgetattr(0)
a[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK |
          termios.ISTRIP | termios.IXON)
a[1] &= ~termios.OPOST
a[2] &= ~(termios.CSIZE | termios.PARENB)
a[2] |= termios.CS8
a[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
a[6][termios.VMIN] = 1
a[6][termios.VTIME] = 0
termios.tcsetattr(0, termios.TCSAFLUSH, a)   # the switch: flushes input
os.read(0, 1)                # the poke Enter -- typed after the switch
sys.stdout.write(']# ')      # the prompt, in answer to the Enter
sys.stdout.flush()
while True:                  # relay, the way a console would
    b = os.read(0, 4096)
    if not b:
        break
    os.write(1, b)
''')
    pid21c, m21c = pty.fork()
    if pid21c == 0:
        os.execve(BIN, ['pty-bridge', '--term', 'serial', '-p', ']# ', '--',
                        sys.executable, fake_console],
                  dict(os.environ, TERM='xterm-test'))
    set_winsize(m21c, 26, 76)
    got = read_avail(m21c, 3.0)
    check('serial: nothing typed before the raw switch',
          b'pre-raw input: 0 byte' in got, repr(got))
    check('serial: Enter after the switch, prompt answered',
          b'export TERM=xterm-test; stty rows 26 columns 76\r' in got,
          repr(got))
    os.write(m21c, b'\x1d')
    status = wait_pid_exit(pid21c)
    ok = status is not None and os.WIFEXITED(status) and \
        os.WEXITSTATUS(status) == 0
    check('serial: escape exits after the TCSAFLUSH switch', ok, repr(status))

# 26) auto: any other COMMAND stays on the pty method -- the sentinel
#     types nothing, the size goes through TIOCSWINSZ
pid22, m22 = pty.fork()
if pid22 == 0:
    os.execv(BIN, ['pty-bridge', '-p', 'ready> ', '--',
                   'sh', '-c', 'stty raw -echo; printf "ready> "; cat'])
time.sleep(0.5)
got = read_avail(m22, 0.5)
check('auto: non-virsh command types nothing',
      b'stty rows' not in got and b'export TERM' not in got, repr(got))
os.write(m22, b'\x1d')
wait_pid_exit(pid22)

# 27) --term pty with a sentinel: the sentinel still retires patterns
#     and types nothing, but resizes go through TIOCSWINSZ, not stty
tm11, ts11 = open_target()
pid23, m23 = pty.fork()
if pid23 == 0:
    os.execv(BIN, ['pty-bridge', '--term', 'pty', '-p', ']# ',
                   '--pty', os.ttyname(ts11)])
time.sleep(0.3)
read_avail(m23, 0.3)
read_avail(tm11, 0.3)    # drain the simulated startup Enter
os.write(tm11, b']# ')   # sentinel fires: nothing typed
got = read_avail(tm11, 0.5)
check('pty mode: sentinel types nothing', got == b'', repr(got))
set_winsize(m23, 55, 66)
got = read_avail(tm11, 0.5)
check('pty mode: resize types nothing', got == b'', repr(got))
rows, cols, _, _ = struct.unpack(
    'HHHH', fcntl.ioctl(tm11, termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0)))
check('pty mode: size synced via ioctl', (rows, cols) == (55, 66),
      'got %dx%d' % (rows, cols))
os.write(m23, b'\x1d')
wait_pid_exit(pid23)

# 28) invalid --term mode -> exit 2
pid24 = os.fork()
if pid24 == 0:
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.execv(BIN, ['pty-bridge', '--term', 'bogus', '--pty', '/dev/pts/1'])
status = wait_pid_exit(pid24)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2
check('invalid --term refused', ok, repr(status))

# 29) --term serial with no TERM in the environment: nothing to export,
#     the first push is a plain stty line
tm12, ts12 = open_target()
pid25, m25 = pty.fork()
if pid25 == 0:
    env = {k: v for k, v in os.environ.items() if k != 'TERM'}
    os.execve(BIN, ['pty-bridge', '--term', 'serial', '-p', ']# ',
                    '--pty', os.ttyname(ts12)], env)
set_winsize(m25, 41, 111)
time.sleep(0.3)
read_avail(m25, 0.3)
read_avail(tm12, 0.3)    # drain the simulated startup Enter
os.write(tm12, b']# ')
got = read_avail(tm12, 0.5)
check('no TERM in env: plain stty only',
      got == b'stty rows 41 columns 111\r', repr(got))
os.write(m25, b'\x1d')
wait_pid_exit(pid25)

# 30) --pty + sentinel (auto -> pty method): the local size is pushed
#     once at attach, so a pty left at a stale size by the previous
#     session is fixed without any local resize; nothing is typed
tm13, ts13 = open_target()
set_winsize(tm13, 24, 80)   # stale size left behind by the last session
pid26, m26 = pty.fork()
if pid26 == 0:
    os.execv(BIN, ['pty-bridge', '-p', ']# ', '--pty', os.ttyname(ts13)])
set_winsize(m26, 40, 100)   # local size, set before the bridge looks at it
time.sleep(0.5)
read_avail(m26, 0.3)
read_avail(tm13, 0.3)    # drain the simulated startup Enter
got = read_avail(tm13, 0.3)
check('pty mode: attach types nothing', got == b'', repr(got))
rows, cols, _, _ = struct.unpack(
    'HHHH', fcntl.ioctl(tm13, termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0)))
check('pty mode: attach fixes a stale size', (rows, cols) == (40, 100),
      'got %dx%d' % (rows, cols))
os.write(m26, b'\x1d')
wait_pid_exit(pid26)

# 31) --term serial with a TERM that would not survive the shell
#     command line it is spliced into (metacharacters): the export is
#     refused with a warning, the first push is a plain stty line
tm14, ts14 = open_target()
pid27, m27 = pty.fork()
if pid27 == 0:
    os.execve(BIN, ['pty-bridge', '--term', 'serial', '-p', ']# ',
                    '--pty', os.ttyname(ts14)],
              dict(os.environ, TERM='x;ter>m'))
set_winsize(m27, 33, 77)
time.sleep(0.3)
early = read_avail(m27, 0.3)
check('unusable TERM refused with warning', b'unusable TERM' in early,
      repr(early))
read_avail(tm14, 0.3)    # drain the simulated startup Enter
os.write(tm14, b']# ')
got = read_avail(tm14, 0.5)
check('unusable TERM: plain stty only',
      got == b'stty rows 33 columns 77\r', repr(got))
os.write(m27, b'\x1d')
wait_pid_exit(pid27)

# 32) -v: the verbose trace on stderr -- the parsed patterns, the tail
#     checked against them, which pattern matched and what it typed,
#     and the sentinel firing
tm15, ts15 = open_target()
pid28, m28 = pty.fork()
if pid28 == 0:
    os.execv(BIN, ['pty-bridge', '-v', '-p', 'login: root', '-p', ']# ',
                   '--pty', os.ttyname(ts15)])
time.sleep(0.3)
got = read_avail(m28, 0.5)
check('-v: patterns logged at startup',
      b"pattern[0] match 'login: ' -> reply 'root'" in got and
      b"pattern[1] sentinel: match ']# '" in got, repr(got))
os.write(tm15, b'noise\nlogin: ')
got = read_avail(m28, 1.0)
check('-v: match and reply traced',
      b"pattern[0] 'login: ': matched, typing reply 'root' + Enter" in got,
      repr(got))
os.write(tm15, b'\nfoo')
got = read_avail(m28, 0.5)
check('-v: no match traced', b"pattern[1] ']# ': no match" in got, repr(got))
os.write(tm15, b']# ')
got = read_avail(m28, 0.5)
check('-v: sentinel traced', b"pattern[1] sentinel ']# ': matched" in got,
      repr(got))
check('-v: record window trimmed at the sentinel',
      b'recording trimmed to the last 3 bytes' in got, repr(got))
# the sentinel still fires with the trimmed window: a longer chunk
# contributes only its tail
os.write(tm15, b'\nfoo]# ')
got = read_avail(m28, 0.5)
check('-v: sentinel still fires with the trimmed window',
      b"pattern[1] sentinel ']# ': matched" in got, repr(got))
os.write(m28, b'\x1d')
wait_pid_exit(pid28)

# 33) a pty channel gets no startup phase and no Enter: a prompt the
#     channel command prints before it puts the pty into raw mode (an
#     ssh host-key confirmation, say) is matched by the main loop as
#     soon as it arrives, and the reply is the first line the prompt
#     sees
pid29, m29 = pty.fork()
if pid29 == 0:
    os.execv(BIN, ['pty-bridge', '-p', '(yes/no/[fingerprint])? yes', '--',
                   'sh', '-c',
                   'printf "Are you sure you want to continue connecting '
                   '(yes/no/[fingerprint])? "; read ans; '
                   'printf "[%s]" "$ans"; stty raw -echo; cat'])
got = read_avail(m29, 2.0)
check('pre-raw prompt matched and reply typed', b'[yes]' in got,
      repr(got))
os.write(m29, b'ok\n')
got = read_avail(m29, 1.0)
check('session continues after the match', b'ok' in got, repr(got))
os.write(m29, b'\x1d')
status = wait_pid_exit(pid29)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('escape exits after the match', ok, repr(status))

# 33b) the serial counterpart of 33: the startup phase waits for the
#      pty to go raw, and the output of that wait runs through
#      record_and_check too -- a prompt the channel command prints
#      before the switch (a login it wants handled before connecting)
#      is matched during the wait, and the reply is typed into the
#      still-canonical pty. The reply is safe where an early Enter
#      would not be, even against a TCSAFLUSH-style switch: the
#      command is reading the reply right then, so it is consumed
#      before the switch happens
pid29b, m29b = pty.fork()
if pid29b == 0:
    os.execve(BIN, ['pty-bridge', '--term', 'serial',
                    '-p', 'login: root', '-p', ']# ', '--',
                    'sh', '-c',
                    'printf "login: "; read ans; printf "[%s]" "$ans"; '
                    'stty raw -echo; printf "]# "; cat'],
              dict(os.environ, TERM='xterm-test'))
set_winsize(m29b, 27, 77)
got = read_avail(m29b, 3.0)
check('serial: pre-raw prompt matched during the wait',
      b'[root]' in got, repr(got))
check('serial: TERM+winsize pushed after the switch',
      b'export TERM=xterm-test; stty rows 27 columns 77\r' in got, repr(got))
os.write(m29b, b'\x1d')
status = wait_pid_exit(pid29b)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('serial: escape exits after pre-raw match', ok, repr(status))

# 33c) the command queue: a pattern with the sentinel's own match (its
#      -p argument starts with the sentinel's, so the split at the
#      first space yields the same match) is a COMMAND. The sentinel
#      runs one per prompt and the first prompt with none left exits
#      the bridge -- here the user's own flow: log in, run ls at the
#      first prompt, and leave without touching the keyboard
pid29c, m29c = pty.fork()
if pid29c == 0:
    os.execv(BIN, ['pty-bridge', '-p', 'login: root', '-p', 'Password: xx',
                   '-p', ']# ', '-p', ']# ls', '--',
                   'sh', '-c',
                   'printf "login: "; read u; printf "Password: "; read pw; '
                   'printf "welcome %s/%s\\n" "$u" "$pw"; '
                   'printf "]# "; while IFS= read -r line; do '
                   'printf "ran:%s\\n" "$line"; printf "]# "; done'])
got = read_avail(m29c, 3.0)
check('command queue: login answered, command ran',
      b'welcome root/xx' in got and b'ran:ls' in got, repr(got))
status = wait_pid_exit(pid29c)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('command queue: exits by itself once the command ran', ok, repr(status))

# 33d) commands queue in -p order, one per prompt, and the prompt after
#      the last one exits -- two prompts seen before any output of the
#      second command would mean both were typed into one prompt
pid29d, m29d = pty.fork()
if pid29d == 0:
    os.execv(BIN, ['pty-bridge', '-p', ']# ', '-p', ']# echo one',
                   '-p', ']# echo two', '--',
                   'sh', '-c',
                   'printf "]# "; while IFS= read -r line; do '
                   'printf "ran:%s\\n" "$line"; printf "]# "; done'])
got = read_avail(m29d, 3.0)
i1, i2 = got.find(b'ran:echo one'), got.find(b'ran:echo two')
check('command queue: commands run in order',
      i1 != -1 and i2 != -1 and i1 < i2, repr(got))
status = wait_pid_exit(pid29d)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('command queue: two commands, self-exit', ok, repr(status))

# 33e) the order of the -p arguments does not matter: a command listed
#      before the sentinel is still a command (never fired on its own),
#      as the -v trace of the parsed patterns shows
pid29e, m29e = pty.fork()
if pid29e == 0:
    os.execv(BIN, ['pty-bridge', '-v', '-p', ']# echo one', '-p', ']# ', '--',
                   'sh', '-c',
                   'printf "]# "; while IFS= read -r line; do '
                   'printf "ran:%s\\n" "$line"; printf "]# "; done'])
got = read_avail(m29e, 3.0)
check('command listed before the sentinel still queued',
      b"pattern[0] match ']# ' -> command 'echo one'" in got and
      b"typing command 'echo one' + Enter" in got and
      b'ran:echo one' in got, repr(got))
status = wait_pid_exit(pid29e)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('command before sentinel: self-exit', ok, repr(status))

# 33f) the serial counterpart: a prompt gets at most one typed line, so
#      the first prompt's turn goes to the window-size push (with the
#      one-time TERM export), the commands follow one per prompt, and
#      the prompt after the last command exits. The fake is a faithful
#      console: silent until spoken to (the startup Enter makes it
#      print), then it answers every line with ran:<line> and a prompt
pid29f, m29f = pty.fork()
if pid29f == 0:
    os.execve(BIN, ['pty-bridge', '--term', 'serial', '-p', ']# ',
                    '-p', ']# echo one', '--',
                    'sh', '-c',
                    'stty -icanon -echo min 1 time 0; '
                    'while IFS= read -r line; do '
                    'printf "ran:%s\\n" "$line"; printf "]# "; done'],
              dict(os.environ, TERM='xterm-test'))
set_winsize(m29f, 28, 78)
got = read_avail(m29f, 5.0)
ipush, icmd = got.find(b'ran:export TERM='), got.find(b'ran:echo one')
check('serial: push takes the first prompt, the command the next',
      ipush != -1 and icmd != -1 and ipush < icmd and
      b'ran:export TERM=xterm-test; stty rows 28 columns 78\r' in got,
      repr(got))
status = wait_pid_exit(pid29f)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('serial: command queue exits by itself', ok, repr(status))

# 33g) --pty mode command queue: the startup Enter makes the prompt
#      appear, the sentinel fires and types the command, and the prompt
#      after the command exits the bridge
tm16, ts16 = open_target()
pid31, m31 = pty.fork()
if pid31 == 0:
    os.execv(BIN, ['pty-bridge', '-p', ']# ', '-p', ']# ls',
                   '--pty', os.ttyname(ts16)])
time.sleep(0.3)
read_avail(m31, 0.3)
got = read_avail(tm16, 0.3)     # the simulated startup Enter
check('--pty: startup Enter', got == b'\r', repr(got))
os.write(tm16, b']# ')          # the prompt in answer to it
got = read_avail(tm16)
check('--pty: command typed at the prompt', got == b'ls\r', repr(got))
os.write(tm16, b'ran:ls\r\n]# ')   # the command ran, the prompt is back
status = wait_pid_exit(pid31)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('--pty: command queue exits by itself', ok, repr(status))

# 33h) -s/--send is the explicit spelling: no match of its own, queued
#      for the sentinel prompt in the order given, and the bridge exits
#      once they all ran (the -v trace shows the parsed entries)
pid32, m32 = pty.fork()
if pid32 == 0:
    os.execv(BIN, ['pty-bridge', '-v', '-p', ']# ', '-s', 'echo one',
                   '-s', 'echo two', '--',
                   'sh', '-c',
                   'printf "]# "; while IFS= read -r line; do '
                   'printf "ran:%s\\n" "$line"; printf "]# "; done'])
got = read_avail(m32, 3.0)
i1, i2 = got.find(b'ran:echo one'), got.find(b'ran:echo two')
check('--send: commands run in order',
      i1 != -1 and i2 != -1 and i1 < i2 and
      b"pattern[1] -> command 'echo one'" in got, repr(got))
status = wait_pid_exit(pid32)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('--send: exits by itself once they all ran', ok, repr(status))

# 33i) --send without a sentinel: no prompt boundary to type at, so it
#      is refused with exit code 2 and a message saying what is missing
pid33, m33 = pty.fork()
if pid33 == 0:
    os.execv(BIN, ['pty-bridge', '-s', 'ls', '--pty', '/dev/pts/1'])
out = read_avail(m33, 0.8)
status = wait_pid_exit(pid33)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2
check('--send without a sentinel refused', ok, repr(status) + ' out=' + repr(out))
check('--send without a sentinel message',
      b'needs a sentinel pattern' in out, repr(out))

# 34) --pty attaches to a working session and must leave the pty's
#     settings alone: the session behind it owns them (a shell there
#     would lose its echo and editing if the bridge forced raw mode
#     onto the pty). Attach to a cooked pty and verify the settings
#     survive the attach unchanged
tma, tsa = pty.openpty()
attrs = termios.tcgetattr(tsa)
attrs[3] |= termios.ICANON | termios.ECHO   # cooked: the session's choice
termios.tcsetattr(tsa, termios.TCSANOW, attrs)
before = termios.tcgetattr(tma)
pid30, m30 = pty.fork()
if pid30 == 0:
    os.execv(BIN, ['pty-bridge', '--pty', os.ttyname(tsa)])
time.sleep(0.5)
read_avail(m30, 0.3)
read_avail(tma, 0.3)    # the simulated Enter
after = termios.tcgetattr(tma)
check('--pty leaves the pty settings alone', after == before,
      'before=%r after=%r' % (before, after))
os.write(m30, b'\x1d')
status = wait_pid_exit(pid30)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('--pty: escape still exits', ok, repr(status))

print('---')
print('FAILURES:', failures)
sys.exit(1 if failures else 0)
