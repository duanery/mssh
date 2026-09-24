# pty-bridge -- implementation notes

`pty-bridge` attaches the local terminal to an existing pty (typically
`/dev/pts/N`): what you type is written into the pty, and everything the pty
produces is printed on your screen. It is a small C program with no
dependencies beyond libc, built with:

```sh
gcc -Wall -Wextra -O2 -o pty-bridge pty-bridge.c
```

```sh
pty-bridge [-e CHAR|--escape CHAR] [-p "<prompt> <reply>"]... /dev/pts/3
```

## Overview

```
                       local terminal (tty, raw)
  keyboard --> stdin --------------------+
                                         |  poll() + ppoll() loop
  screen   <-- stdout <------------------+
                       |                 |
                       v                 v
                write(pts_fd)      read(pts_fd)
                       |                 ^
                       +-> /dev/pts/N <--+
                     (slave side, raw)
```

`pty-bridge` opens the *slave* side of an existing pty and shuttles bytes
both ways. It never touches the master side; whoever holds the master (an
mssh session daemon, `screen`, a test harness, ...) keeps working
unchanged -- bytes we write into the slave appear on the master as if
typed on the session's keyboard, and bytes the session writes come back
to us.

## Startup checks

1. `isatty(0)` -- stdin must be a terminal.
2. `tcgetsid(0) == getsid(0)` -- stdin must be the *controlling terminal*
   of the process. Running under a pipe or a background job is refused;
   the tool is an interactive attach console.
3. The pty argument is opened with `O_RDWR | O_NOCTTY` and checked with
   `isatty()`. `O_NOCTTY` matters: opening a slave pty must never steal
   the controlling-terminal role from the session that owns it.

## Raw mode on both ends

Both the local tty and the slave pty are put into raw mode with
`cfmakeraw()` (`VMIN=1, VTIME=0`), and the original local settings are
saved and restored on every exit path -- including `SIGINT`, `SIGTERM`
and `SIGHUP`, whose handler restores the tty and then re-raises the
signal so the process dies with its true status.

- **Local tty raw**: every keystroke (including `^C`, `^D`, arrows,
  escape sequences) is forwarded byte-by-byte; the line discipline does
  no editing, no signals, no echo. The remote session already provides
  line editing and echo, so doing it twice would double every character.
- **Slave pty raw**: bytes we write into the slave are not mangled or
  echoed back by the line discipline. Without this, the master would see
  every byte twice (once as our input, once as the echo), and `^C` would
  never reach the remote shell as a byte.

## The forwarding loop

The main loop `ppoll()`s on stdin and the pty:

- **stdin readable**: read a chunk, scan it for the escape character.
  If found, any bytes before it are still forwarded, then the loop
  exits (a `\r\n` is printed first so the shell prompt starts on a
  fresh line -- the remote prompt we were sitting on ends without a
  newline). Otherwise the chunk is written to the pty.
- **pty readable**: read a chunk, write it to stdout, then hand it to
  `record_and_check()` (patterns, below). `EIO`/`EAGAIN`/`POLLHUP` on
  the pty means the master is gone and the loop ends.

## Escape character

`-e/--escape CHAR` selects the exit key, default `^]` (Ctrl-]).
`CHAR` is either one literal byte or `^X` notation: a letter/symbol `X`
maps to `X & 0x1f` (`^]` = 0x1d), and `^?` maps to DEL (0x7f). The
parsed byte is also announced before raw mode starts
(`Escape character is ^]`) so it is readable.

## Pattern auto-reply (`-p`)

`-p "<prompt> <reply>"` automates prompted logins. The value is split at
the **last** space; the space stays with the prompt, so in
`-p "login: root"` the full prompt text `login: ` (colon *and* trailing
space) is matched, and `root` is typed.

Matching rules:

- Only the **tail** of the output stream is compared, with `memcmp` on
  the exact `mlen` bytes. A prompt is defined by that property: once the
  peer prints it, it stops and waits for input, so the prompt is always
  the last thing in the stream. A `login:` buried in a banner or in
  scrolling output never fires, because at the moment of comparison the
  tail is something else.
- Each normal pattern fires **at most once** (`used` flag). After
  firing, the reply plus `\r` (Enter) is written into the pty and the
  record buffer is cleared so the echoed reply cannot trigger another
  pattern.

### Recording window

`record_and_check()` keeps only the last `max_mlen` bytes of pty output,
where `max_mlen` is the longest pattern text, computed once at parse
time. Since matching is tail-only, this window is all that can ever be
needed; chunks longer than the window contribute only their tail. When
nothing is left to match (no `-p` at all, or every normal pattern fired
and no sentinel exists), recording stops for good and forwarding runs
with zero matching overhead.

## The sentinel pattern (empty reply)

A `-p` whose reply is empty -- `-p ']# '` matches the prompt `]# ` -- is
a *sentinel*: it marks the shell command prompt, the boundary where the
peer sits waiting for the user to type. Sentinels behave differently:

- At most **one** sentinel is allowed; a second one is refused.
- When it fires, **nothing is typed** (no reply, no Enter).
- Every still-unused normal pattern is marked used: once a shell prompt
  showed up, `login:`/`Password:` prompts are stale and must never fire.
- The sentinel itself **stays armed** -- the shell returns to its prompt
  after every command, so the sentinel can fire again and again.
- `sentinel_seen` tracks whether we are *currently* at the prompt: set
  when the sentinel matches, cleared by `record_and_check()` as soon as
  any other pty output arrives (a command is running, output is
  streaming).

### Window size synchronization

The remote shell does not know about local window resizes (the master
holder cannot forward `TIOCSWINSZ` for us), so `pty-bridge` pushes the
size by typing shell commands into the session:

```
stty rows <R> columns <C>\r
```

typed in the open -- wrapping it in `stty -echo` / `stty echo` would
leave a visible `stty -echo` string on screen anyway, so the resize is
simply shown to the user.

The trigger logic:

1. `SIGWINCH` is installed with a handler that only sets
   `winch_pending = 1` (`volatile sig_atomic_t`).
2. **Before the main loop**, `SIGWINCH` is blocked with
   `sigprocmask(SIG_BLOCK, ...)`, saving the original mask.
3. The wait is done with `ppoll(..., &orig_set)`: ppoll installs the
   *original* mask (without SIGWINCH) only while waiting and restores
   the blocked mask on return. SIGWINCH is therefore delivered exactly
   inside the wait -- it always interrupts `ppoll()` with `EINTR` --
   and can never slip in between the flag check and the wait. This is
   the classic race-free signal/wait pattern (no self-pipe needed).
4. At the top of every loop iteration:
   - **sitting at the prompt** (`sentinel_seen == 1`) and the window
     changed: push the size immediately.
   - **output streaming** (`sentinel_seen == 0`, a command is running):
     do *not* inject `stty` into whatever is running; `winch_pending`
     survives, and the next sentinel firing (the next prompt) pushes
     the size instead.
5. `winch_pending` starts **set** (it is set at parse time when a
   sentinel is configured), so the very first prompt triggers the
   initial size push -- the "attach and fix the window" case.

## Testing

`tests/test_pty_bridge.py` drives the real binary through real ptys
(no mocking of the terminal layer):

- a target pty plays the remote session (`pty.openpty()`),
- `pty.fork()` gives the tool its own controlling terminal,
- the test writes on one master and asserts on the other.

Covered: bidirectional forwarding, raw passthrough of `^C`/`^D`, escape
exit (default and custom, exit status 0), bytes before the escape still
forwarded, the detach newline, refusals (stdin not a tty, non-tty
argument, malformed pattern, two sentinels), pattern auto-reply
including one-shot semantics and exact-tail matching, sentinel behavior
(retiring stale patterns, typing nothing), and the full window-size
state machine (initial push, immediate push at the prompt, deferred
push after streaming output). The test compiles the binary itself if it
is missing and exits non-zero on any failure:

```sh
python3 tests/test_pty_bridge.py
```
