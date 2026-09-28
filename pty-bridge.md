# pty-bridge -- implementation notes

`pty-bridge` attaches the local terminal to a pty -- local or remote.
What you type is written into the pty, and everything the pty produces
is printed on your screen. The pty to attach is either an existing
local one (`--pty /dev/pts/N`) or a *remote* one reached through a
channel command (`ssh -tt host`, `virsh console vm`, ...): such a
remote pty cannot be opened directly, it is only reachable through the
command that connects to it. It is a small C program with no
dependencies beyond libc (+ libutil for `openpty`), built with:

```sh
gcc -Wall -Wextra -O2 -o pty-bridge pty-bridge.c -lutil
```

```sh
pty-bridge [-e CHAR|--escape CHAR] [-p "<match> <reply>"]... \
           [--term pty|serial|auto] [--pty /dev/pts/3] [command [arg ...]]
```

## Overview

```
                       local terminal (tty, raw)
  keyboard --> stdin --------------------+
                                         |  ppoll() loop
  screen   <-- stdout <------------------+
                       |                 |
                       v                 v
                 write(pty_fd)      read(pty_fd)
                       |                 ^
                       +->  the pty  <---+
```

Two ways to obtain "the pty":

- **`--pty PATH`** -- an existing local pty. `pty-bridge` opens the
  *slave* side (`/dev/pts/N`) and shuttles bytes both ways. It never
  touches the master side; whoever holds the master (an mssh session
  daemon, `screen`, a test harness, ...) keeps working unchanged --
  bytes we write into the slave appear on the master as if typed on
  the session's keyboard, and bytes the session writes come back to
  us.
- **COMMAND mode** (no `--pty`) -- the pty to attach is remote, so a
  fresh pty is created with `openpty()` and a child process is forked
  with the slave as its stdin/stdout/stderr and controlling terminal;
  the child then `execvp()`s the command. The command -- e.g.
  `ssh -tt host` or `virsh console vm` -- is the *channel* that
  carries the remote pty: it draws the remote pty through our pty,
  and `pty-bridge` (holding the master end) bridges the local
  terminal onto it. Before any output, `pty-bridge` prints
  `Running: <command>` and its own escape hint (`Escape character is
  ^] (exits pty-bridge)`), so the escape hint the command itself may
  print later (virsh console prints one for the virtual machine
  console) is not mistaken for ours. When the child exits (remote
  side closed, ssh dropped, ...), the slave is gone, the master read
  fails with `EIO` and `pty-bridge` exits by itself. On the escape
  key the master is closed, the child is given `SIGHUP` and reaped,
  so no stray channel process survives the detach.

The rest of the machinery (raw mode, escape, patterns, window size)
works identically on both modes: it only sees the one bridge fd.

## Startup checks

1. `isatty(0)` -- stdin must be a terminal.
2. `tcgetsid(0) == getsid(0)` -- stdin must be the *controlling terminal*
   of the process. Running under a pipe or a background job is refused;
   the tool is an interactive attach console.
3. The pty is acquired (see above): `--pty` opens the path with
   `O_RDWR | O_NOCTTY` and checks it with `isatty()` (`O_NOCTTY`
   matters: opening a slave pty must never steal the
   controlling-terminal role from the session that owns it); COMMAND
   mode creates the pty and forks the child in `open_command_pty()`.

## The channel command (`open_command_pty()`)

`openpty()` creates the pty pair, seeded with the local terminal's own
`termios` and window size -- both read from stdin, which is still in
its original state at this point. The new pty therefore looks exactly
like the terminal the user is sitting at, so `ssh -tt` relays the
right size and line settings to the remote pty from the very start.
Then:

- **child**: closes the master, `setsid()` (new session, no
  controlling terminal yet), `ioctl(slave, TIOCSCTTY)` to make the
  slave its controlling terminal, `dup2()`s the slave onto
  stdin/stdout/stderr, and `execvp()`s the command. An exec failure
  is reported on stderr and exits 127.
- **parent**: closes the slave (it must not keep the slave side alive,
  or the child's `EIO`-on-exit detection would never fire) and
  remembers the child pid.

`getopt_long()` is called with a leading `+` in the optstring so
option parsing stops at the first non-option argument: the command's
own options (`ssh -tt`, `virsh console vm`) belong to the command,
not to `pty-bridge`. Use `--` to separate them explicitly. COMMAND
together with `--pty` is refused.

### Waiting for the command's raw mode (`wait_pty_raw()`)

The channel command usually puts the pty into raw mode itself, but it
may print -- a banner, an error message -- before doing so. Output
produced while the pty is still canonical goes through the line
discipline (`\n` -> `\r\n`), which is exactly what a still-cooked
local terminal needs for display; output produced after the switch is
raw-relayed and needs a raw local terminal. So the startup sequence
is:

1. the command is forked (`open_command_pty()`),
2. `wait_pty_raw()` polls the master, printing whatever arrives while
   the local terminal is still cooked, until the pty turns raw -- the
   termios settings are shared between master and slave, so the
   check is simply `!(c_lflag & ICANON)` from the master side,
3. then the local terminal is switched to raw and the normal
   forwarding loop starts.

The wait gives up (and proceeds with the main loop) when the command
exits (`EIO`), or after `STARTUP_TIMEOUT_SEC` (3s) of *silence* --
every output restarts that timeout -- so a command that never goes
raw cannot wedge the tool, while a live, still-printing command is
waited for as long as it keeps talking. The phase runs before any
terminal change and before the signal handlers are installed, which
is safe: the local tty has not been touched yet (nothing to restore
on a kill), and SIGWINCH needs no handling either (on a serial channel
the first prompt pushes the window size; on a pty channel the size was
seeded at `openpty()`).

## Raw mode on both ends

The local tty is put into raw mode with `cfmakeraw()` (`VMIN=1,
VTIME=0`) -- in COMMAND mode only after `wait_pty_raw()` saw the
command switch the pty, see above. The original local settings are
saved and restored on every exit path -- including `SIGINT`,
`SIGTERM` and `SIGHUP`, whose handler restores the tty and then
re-raises the signal so the process dies with its true status.

- **Local tty raw**: every keystroke (including `^C`, `^D`, arrows,
  escape sequences) is forwarded byte-by-byte; the line discipline does
  no editing, no signals, no echo. The remote session already provides
  line editing and echo, so doing it twice would double every character.
- **Pty raw**: bytes written into the pty are not mangled or echoed
  back by the line discipline. Without this, every byte would come
  back twice (once as our input, once as the echo), and `^C` would
  never reach the remote shell as a byte. In COMMAND mode the pty is
  *not* set raw by `pty-bridge`: the channel command owns the pty
  settings and switches it itself (`wait_pty_raw()` waits for that),
  and the relayed remote output already carries its own `\r\n`. In
  `--pty` mode the existing pty is switched to raw by `pty-bridge`.

## The forwarding loop

The main loop `ppoll()`s on stdin and the pty:

- **stdin readable**: read a chunk, scan it for the escape character.
  If found, any bytes before it are still forwarded, then the loop
  exits (a `\r\n` is printed first so the shell prompt starts on a
  fresh line -- the remote prompt we were sitting on ends without a
  newline). Otherwise the chunk is written to the pty.
- **pty readable**: read a chunk, write it to stdout, then hand it to
  `record_and_check()` (patterns, below). `EIO`/`EAGAIN`/`POLLHUP` on
  the pty means the peer is gone -- the master holder closed the
  slave (`--pty` mode) or the channel command exited (COMMAND mode) --
  and the loop ends.

## Escape character

`-e/--escape CHAR` selects the exit key, default `^]` (Ctrl-]).
`CHAR` is either one literal byte or `^X` notation: a letter/symbol `X`
maps to `X & 0x1f` (`^]` = 0x1d), and `^?` maps to DEL (0x7f). The
parsed byte is also announced before raw mode starts
(`Escape character is ^]`) so it is readable.

## Pattern auto-reply (`-p`)

`-p "<match> <reply>"` automates prompted logins. The value is split at
the **first** space; the separating space stays with the match, the
reply keeps every space it has. Examples: `-p "login: root"` types the
username at the `login: ` prompt (colon *and* trailing space included),
`-p "Password: secret"` types the password, `-p "]# cd /tmp/"` runs
the command once the `]# ` shell prompt shows up.

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

## Terminal type (`--term`)

Channels differ in what they can carry to the remote end on their own.
An `ssh -tt` channel allocates its pty out of band: it relays window
changes and exports TERM itself, so the bridge only pushes the size
into its own pty and the channel does the rest. A `virsh console`
channel carries a *serial console*: there is no out-of-band
window-change and no environment -- the guest learns the window size
and TERM only by being *told*, i.e. by typing shell commands into the
session.

`--term MODE` selects how the bridge syncs, so no command name is
special-cased:

- **`pty`** -- the channel passes window changes through (ssh-like).
  On every `SIGWINCH` the size is pushed into the pty with
  `TIOCSWINSZ` (`push_winsize()`); the kernel delivers SIGWINCH to the
  process group behind the pty, which is what a real terminal does.
  In `--pty` mode the size is also pushed once at attach: the pty
  keeps whatever size the previous session left behind, and this fixes
  a stale window without a manual resize. TERM is the channel's own
  business; nothing is typed.
- **`serial`** -- the channel is a serial console (virsh-console-like).
  On every resize the size is typed into the session as
  `stty rows <R> columns <C>` (below); the **first** push also exports
  TERM, once, in the same line:
  `export TERM=<TERM>; stty rows <R> columns <C>`. The window size
  changes constantly, TERM is set once at init and never again. TERM
  comes from the environment at startup and is only exported when it
  is a plain terminfo-style name (letters, digits, `+ - . _`): the
  value is spliced into a shell command, so anything carrying spaces,
  quotes or metacharacters is refused -- with a warning -- instead of
  sent.
- **`auto`** (default) -- one recognition rule only: a COMMAND of the
  form `virsh console ...` means `serial`, everything else (`ssh -tt`,
  `--pty`, any other command) means `pty`. The rule is deliberately
  narrow: `sudo virsh console vm` or `virsh -c URI console vm` do not
  match it and need an explicit `--term serial`.

Typing into the session is only safe while the peer sits at a prompt,
which is what the sentinel pattern marks. A `serial` setting without a
sentinel has no prompt boundary to type at, so it warns on stderr and
falls back to the `pty` method: window size via `TIOCSWINSZ`, no TERM
export. `auto` can hit the same fallback -- a `virsh console` command
without a sentinel -- and warns the same way.

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

On a serial channel (`--term serial`) the sentinel doubles as the
injection point for the window size and the one-time TERM export: both
are typed in only while the peer sits at the prompt (see below).

### Window size synchronization

The size is pushed every time the local window changes (`SIGWINCH`
sets `winch_pending`), but *how* depends on the terminal type
(`--term` above):

- **pty**: pushed straight into the pty with `TIOCSWINSZ`
  (`push_winsize()`); the kernel delivers SIGWINCH to the foreground
  process group behind the pty, which is what a real terminal does on
  a resize. In `--pty` mode the size is also pushed once at attach, so
  a pty left at a stale size comes up correct (see the terminal-type
  section above). No typing is involved.
- **serial**: the guest cannot be reached by any ioctl, so the size
  must be typed in as a shell command,

  ```
  stty rows <R> columns <C>\r
  ```

  typed in the open -- wrapping it in `stty -echo` / `stty echo` would
  leave a visible `stty -echo` string on screen anyway, so the resize
  is simply shown to the user. The **first** push carries the one-time
  TERM export in the same line:
  `export TERM=<TERM>; stty rows <R> columns <C>`.

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
4. At the top of every loop iteration, if the window changed:
   - **pty**: push the size via `TIOCSWINSZ` immediately.
   - **serial, sitting at the prompt** (`sentinel_seen == 1`): push
     immediately.
   - **serial, output streaming** (`sentinel_seen == 0`, a command is
     running): do *not* inject `stty` into whatever is running;
     `winch_pending` survives, and the next sentinel firing (the next
     prompt) pushes the size instead.
5. `winch_pending` starts **set on a serial channel** (it is set once
   the terminal type resolves to serial), so the very first prompt
   triggers the initial push -- the TERM export and the window size
   together, the "attach and fix the terminal" case. On a pty channel
   the size travels out of band (COMMAND mode seeds it at `openpty()`;
   `--pty` mode pushes it once at attach), so nothing is pending at
   startup.

## Testing

`tests/test_pty_bridge.py` drives the real binary through real ptys
(no mocking of the terminal layer):

- a target pty plays the remote session (`pty.openpty()`),
- `pty.fork()` gives the tool its own controlling terminal,
- the test writes on one master and asserts on the other.

Covered: bidirectional forwarding, raw passthrough of `^C`/`^D`, escape
exit (default and custom, exit status 0), bytes before the escape still
forwarded, the detach newline, refusals (stdin not a tty, non-tty
`--pty` path, `--pty` combined with COMMAND, malformed pattern, two
sentinels, invalid `--term` mode), pattern auto-reply including one-shot
semantics and exact-tail matching, sentinel behavior (retiring stale
patterns, typing nothing), the window-size state machine for both
terminal types (`--term serial`: TERM export + stty push at the first
prompt, immediate push at the prompt, deferred push after streaming
output, plain stty when TERM is unset or unusable in the environment,
fallback to `TIOCSWINSZ` without a sentinel, with the warning; pty: an
initial size push at attach fixing a stale pty, `TIOCSWINSZ` sync while
a sentinel is configured), auto-detection (a `virsh console` command,
played by a fake `virsh`, switches to the serial method; any other
command stays pty), and COMMAND mode (bytes relayed to the child, early
output forwarded while the pty is still canonical, local window size
inherited by the new pty, child exit closing the bridge, exec failure,
the silent-timeout fallback, escape detaching without strays). The test
compiles the binary itself if it is missing and exits non-zero on any
failure:

```sh
python3 tests/test_pty_bridge.py
```
