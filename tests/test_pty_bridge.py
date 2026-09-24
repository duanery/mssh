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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, '..', 'pty-bridge')

TIOCSWINSZ = termios.TIOCSWINSZ

failures = 0


def set_winsize(master_fd, rows, cols):
    """Set the pty window size (the bridge child gets SIGWINCH)."""
    fcntl.ioctl(master_fd, TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))


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
target_master, target_slave = pty.openpty()
target_name = os.ttyname(target_slave)

# --- spawn pty_bridge under its own pty, so stdin is a controlling terminal ---
pid, local_master = pty.fork()
if pid == 0:
    os.execv(BIN, ['pty-bridge', '-e', '^]', target_name])

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
    os.execv(BIN, ['pty-bridge', target_name])
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
    os.execv(BIN, ['pty-bridge', '/etc/passwd'])
out3 = read_avail(m3, 0.8)
status = wait_pid_exit(pid3)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2
check('non-tty arg refused', ok, repr(status) + ' out=' + repr(out3))

# 8) target pty closed by its master -> pty_bridge exits
pid4, m4 = pty.fork()
if pid4 == 0:
    os.execv(BIN, ['pty-bridge', '-e', '^a', target_name])
time.sleep(0.3)
read_avail(m4, 0.2)
os.close(target_master)
status = wait_pid_exit(pid4)
check('pts closed -> exit', status is not None and os.WIFEXITED(status), repr(status))

# 9) alternate escape character (-e ^a = 0x01)
tm2, ts2 = pty.openpty()
pid5, m5 = pty.fork()
if pid5 == 0:
    os.execv(BIN, ['pty-bridge', '-e', '^a', os.ttyname(ts2)])
time.sleep(0.3)
read_avail(m5, 0.2)
os.write(m5, b'\x01')
status = wait_pid_exit(pid5)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('custom escape ^a exits', ok, repr(status))

# 10) -p pattern: prefix appearing in the output auto-types the reply + Enter
tm3, ts3 = pty.openpty()
pid6, m6 = pty.fork()
if pid6 == 0:
    os.execv(BIN, ['pty-bridge', '-p', 'login: root', os.ttyname(ts3)])
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
tm5, ts5 = pty.openpty()
pid9, m9 = pty.fork()
if pid9 == 0:
    os.execv(BIN, ['pty-bridge', '-p', 'login: root', os.ttyname(ts5)])
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
tm4, ts4 = pty.openpty()
pid7, m7 = pty.fork()
if pid7 == 0:
    os.execv(BIN, ['pty-bridge', '-p', 'login: root', '-p', 'Password: secret',
                   os.ttyname(ts4)])
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

# 14) malformed -p value -> exit 2
pid8 = os.fork()
if pid8 == 0:
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.execv(BIN, ['pty-bridge', '-p', 'nospace', '/dev/pts/1'])
status = wait_pid_exit(pid8)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2
check('malformed pattern refused', ok, repr(status))

# 14b) two sentinel patterns -> exit 2
pid12 = os.fork()
if pid12 == 0:
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.execv(BIN, ['pty-bridge', '-p', ']# ', '-p', '$ ',
                   '/dev/pts/1'])
status = wait_pid_exit(pid12)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2
check('second sentinel refused', ok, repr(status))

# 15) sentinel (empty reply) fires on the command prompt: types nothing,
#     retires every still-unused pattern, and pushes the window size via
#     stty commands written into the pty
tm6, ts6 = pty.openpty()
pid10, m10 = pty.fork()
if pid10 == 0:
    os.execv(BIN, ['pty-bridge', '-p', 'login: root', '-p', ']# ',
                   os.ttyname(ts6)])
set_winsize(m10, 40, 100)
time.sleep(0.3)
read_avail(m10, 0.3)
read_avail(tm6, 0.3)    # drain the simulated startup Enter
# shell prompt arrives first (session was already logged in):
# sentinel fires, types nothing but the stty sequence, and the login
# prompt is retired
os.write(tm6, b'Last login: ...\n[root@28 ~]# ')
got = read_avail(tm6, 0.5)
check('sentinel pushes winsize init',
      got == b'stty rows 40 columns 100\r', repr(got))

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
#     auto-replying and initializes the window size
tm7, ts7 = pty.openpty()
pid11, m11 = pty.fork()
if pid11 == 0:
    os.execv(BIN, ['pty-bridge', '-p', 'login: root', '-p', ']# ',
                   os.ttyname(ts7)])
set_winsize(m11, 30, 90)
time.sleep(0.3)
read_avail(m11, 0.3)
read_avail(tm7, 0.3)
os.write(tm7, b'login: ')
got = read_avail(tm7)
check('login fires before sentinel', got == b'root\r', repr(got))
os.write(tm7, b'\nmotd\n]# ')
got = read_avail(tm7, 0.5)
check('sentinel pushes winsize after login',
      got == b'stty rows 30 columns 90\r', repr(got))
os.write(m11, b'\x1d')
status = wait_pid_exit(pid11)
ok = status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
check('escape exits after login+sentinel', ok, repr(status))

print('---')
print('FAILURES:', failures)
sys.exit(1 if failures else 0)
