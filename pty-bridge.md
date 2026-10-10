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
           [-s "<command>"]... [--term pty|serial|auto] \
           [-w "<text>"] [--pty /dev/pts/3] [command [arg ...]]
```

## Overview

```
                       local terminal (tty, raw input)
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
  so no stray channel process survives the detach. The self-exit of
  the command queue (patterns below) leaves the same way.

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

### Waking the serial console (`wake_serial_console()`)

`wake_serial_console()` runs for one case only: a COMMAND mode channel
resolved to the serial method -- a `virsh console` command picked by
auto, or an explicit `--term serial`. The two channel kinds differ in
who speaks first:

- a **pty channel** (ssh) prints its prompts immediately, unasked --
  there is nothing to wait for, so it gets no startup phase at all and
  the main loop starts right away, matching the prompts wherever they
  appear (including before the channel puts the pty into raw mode),
- a **serial console** is silent until spoken to: the guest behind it
  is already sitting waiting for input, and its prompt appears only in
  answer to an Enter.

For the serial console the phase has two parts:

1. **Before the pty is raw** the channel command may print -- a
   banner, an error message, a question it asks on the way up --
   through the still-canonical line discipline (`\n` -> `\r\n`), which
   is exactly what the still-cooked local terminal needs for display.
   Whatever arrives is printed and recorded: `record_and_check()`
   runs here too, so a prompt the command prints before the switch
   (a confirmation it wants before connecting) is matched and its
   reply typed during the wait, exactly as the main loop would match
   it. There is no timeout: the command is expected to put the pty
   into raw mode once it has connected (`virsh console` does), and if
   it exits instead the phase returns and lets the main loop see the
   exit. The poll wakes every 100ms to notice the switch -- the
   termios settings are shared between master and slave, so the check
   is simply `!(c_lflag & ICANON)` from the master side.
2. **Once the pty is raw** the console is connected -- and possibly
   silent at its prompt. An Enter is typed to make the prompt appear,
   but only after the switch is seen, never before: the command *may*
   put the pty into raw mode with `tcsetattr(slave, TCSAFLUSH,
   &raw)` -- not every command uses the flushing variant, but nothing
   promises one that doesn't -- and `TCSAFLUSH` discards input that
   is still pending unread, so a `\r` typed into the still-canonical
   pty would be dropped by the very switch that was supposed to
   deliver it to the console. (A pattern reply typed during the wait
   is safe: it answers a prompt the command is reading right then, so
   it is consumed before the switch; the Enter has no reader until
   the console connects, so it must wait for the poll to see raw
   mode.) With `-w/--wait-for TEXT` the raw switch alone is not
   enough: the Enter also waits until `TEXT` has appeared in the
   output (see below). Then the pty is polled:
   as soon as it answers (or the command exits) the phase returns
   *without reading* -- the output is the main loop's, where the
   patterns match it. While the console stays silent the Enter is
   repeated every `ENTER_RETRY_SEC` (3s): a virtual machine may take
   a while to reach its getty, and a silent console has nothing
   better to offer than another Enter.

#### Waiting for the channel's banner (`-w/--wait-for`)

The raw switch is a good sign that the channel is connected, but it
is not always the right moment: a command may switch the pty to raw
before it has finished coming up, and an Enter typed then is read by
the command rather than passed on to the console. `-w TEXT` makes the
condition explicit: the Enter is typed only once the channel has
printed `TEXT` **and** the pty is raw, in either order. `virsh
console` announces the attach with

```
Connected to domain 078ca7b8-a183-4b5c-bcc4-cd049fa4d09f
Escape character is ^]
```

so `-w "Escape character is ^]"` holds the Enter back until virsh
says it is attached to the console.

Seeing the text is not quite the moment either: the command prints it
on its way into the console and still has the last steps of its setup
to take. So the Enter waits `WAIT_SETTLE_MS` (100ms) more, counted
from the moment the text shows up (and for the raw switch, if that
has not come yet). The wait is not a plain `sleep`: the output is
still read throughout, so nothing the channel prints in those 100ms
is left in the pty for the post-Enter poll to take as the console's
answer.

- The text is searched **anywhere** in the output, not only at its
  tail as the patterns are: it is a banner line followed by a
  newline, not a prompt the peer stops at. The comparison is a
  literal byte match (`^]` is the two characters `^` and `]`, as
  virsh prints them).
- The output is read, printed and recorded through
  `record_and_check()` for the whole wait -- before the switch and
  after it, until the text shows up -- so nothing is lost and a
  prompt printed meanwhile is still answered. The text may be split
  across reads: the last `strlen(TEXT) - 1` bytes are kept and
  searched together with the next chunk (`wait_text_check()`).
- There is no timeout, the same as for the raw switch: everything
  read is printed, so a channel that never prints the text shows on
  screen, and `^C` ends the wait (the local tty is still cooked and
  untouched at this point).
- `TEXT` is 1 to 256 bytes; an empty one is refused (exit 2). Outside
  a serial channel COMMAND (`--pty`, a pty channel, or the serial
  method falling back for want of a sentinel) there is no startup
  phase to gate: `-w` warns and is ignored.

The phase runs before any terminal change and before the signal
handlers are installed, which is safe: the local tty has not been
touched yet (nothing to restore on a kill), and SIGWINCH needs no
handling either (the size is pushed at the first prompt -- the prompt
the typed Enter is there to produce).

### The simulated Enter

Which end gets an unsolicited Enter typed into it, and when:

- **`--pty`**: one Enter right before the main loop. A session may
  have sat at its prompt since long before the attach -- the Enter
  makes the prompt (re)appear where the patterns can see it.
- **Serial channel COMMAND**: typed inside the startup phase, right
  after the console is connected (the pty turns raw -- and not a
  moment earlier: the switch may be a `TCSAFLUSH`, which would drop
  an Enter typed before it; see above -- and, with `-w`, not before
  the channel has printed the given text either), and repeated while
  it stays silent -- see the section above.
- **Pty-channel COMMAND**: never. Its prompts are printed immediately
  and matched by the main loop as they come; a bare `\r` would only
  wait in the input queue to be consumed as an empty answer by the
  first prompt the command prints -- the ssh host-key confirmation,
  typically, which then fails with "Host key verification failed."
  before anyone can answer.

## Raw mode on both ends

The local tty is put into raw mode with `cfmakeraw()` (`VMIN=1,
VTIME=0`) -- in COMMAND mode after the serial-console startup phase,
if any (above). The original local settings are saved and restored on
every exit path -- including `SIGINT`,
`SIGTERM` and `SIGHUP`, whose handler restores the tty and then
re-raises the signal so the process dies with its true status.

One `cfmakeraw()` side effect is undone: it clears `OPOST`, and the
output flags of the local terminal are restored verbatim
(`raw.c_oflag = saved_tio.c_oflag`). Raw applies to the input side
only. The bytes the bridge relays are already display-ready -- a
serial console sends `\r\n` by itself, and the channel pty is created
with `OPOST` off (`open_command_pty()`), so a cooked command's `\n`
is not completed twice. The one line discipline that renders newlines
is the user's own terminal: its `ONLCR` turns a relayed `\r\n` into
`\r\r\n`, where the second `\r` repeats a return the line already
made. And with the output flags untouched, every other writer on that
terminal -- `od` at the end of a pipe, the shell after us -- keeps
the line ends the user configured.

A piped or redirected stdout has no line discipline to hand a `\r\n`
to: `write_stdout()` translates `\r\n` into `\n` there, so
line-oriented tools downstream (`grep`'s `$` anchor, `awk`'s last
field) see clean line ends. A `\r` at the end of a chunk is held
back until the next byte decides whether it was the first half of a
`\r\n`; one still held when the stream ends is flushed. Pattern
matching is unaffected -- it runs on the raw bytes, before any
translation. `--pty` mode's pty belongs to the session behind it and
is not touched; whatever line endings it emits go through the same
`write_stdout()`.

- **Local tty raw**: every keystroke (including `^C`, `^D`, arrows,
  escape sequences) is forwarded byte-by-byte; the line discipline does
  no editing, no signals, no echo. The remote session already provides
  line editing and echo, so doing it twice would double every character.
- **Pty settings**: in COMMAND mode the channel pty starts as a copy
  of the local terminal -- the same window size and termios, except
  `OPOST`, cleared so the channel does not post-process output a
  second time (the user's own terminal owns newline rendering). From
  there the command owns the settings and may switch them
  (`wake_serial_console()` waits for that). In `--pty` mode the
  session behind the pty owns them entirely -- the pty was already
  configured for its own use, and forcing raw mode under a running
  shell would break its echo and line editing. Whatever settings the
  owner chose are what the bridge works with.

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
the command once the `]# ` shell prompt shows up. A pattern whose
match equals the sentinel's (below) is a *command*: the sentinel types
it at the prompt -- the pattern never fires on its own. `-s/--send`
says the same thing explicitly, with just the command.

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
needed; chunks longer than the window contribute only their tail. Once
the sentinel fires, the window shrinks to the sentinel's own length:
every normal pattern is retired or used by then, and commands share
the sentinel's match, so that is the only match text still able to
fire. When nothing is left to match (no `-p` at all, or every normal
pattern fired and no sentinel exists), recording stops for good and
forwarding runs with zero matching overhead.

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
  showed up, `login:`/`Password:` prompts are stale and must never
  fire. Command patterns (below) are the exception -- they are *for*
  the prompt, not stale because of it.
- The sentinel itself **stays armed** -- the shell returns to its prompt
  after every command, so the sentinel can fire again and again.
- `sentinel_seen` tracks whether we are *currently* at the prompt: set
  when the sentinel matches, cleared by `record_and_check()` as soon as
  any other pty output arrives (a command is running, output is
  streaming).

On a serial channel (`--term serial`) the sentinel doubles as the
injection point for the window size and the one-time TERM export: both
are typed in only while the peer sits at the prompt (see below).

### Commands at the prompt, and the self-exit

A non-sentinel pattern whose **match equals the sentinel's** is a
*command*. Its `-p` argument starts with the sentinel's (`"]# ls"`
beside `"]# "`), so the split at the first space leaves both with the
same match -- the prompt -- and the reply is what to run there.
`-s/--send CMD` says it explicitly: it queues CMD for the sentinel
prompt with no match of its own -- no prompt prefix to repeat, and
nothing implicit to remember. The two spellings share one queue, in
command-line order; commands never fire on their own -- the sentinel
types them:

- one command per prompt, in `-p` order (a queue), and at most **one
  line per prompt, the pending window-size push first**: a line typed
  at a prompt comes back as exactly one prompt (the peer ran the line
  and waits again), so two lines typed into one prompt would return
  two prompts -- and the second would read as the completion of a
  command that never ran. The same rule closes the main loop's
  at-prompt push: with commands queued, a resize waits for the
  sentinel's own firing instead of typing `stty` right after the
  sentinel typed a command into the same prompt.
- when the sentinel fires with the queue empty, every command has run
  to completion -- that prompt is the last command's completion -- and
  the bridge **exits by itself** (`quit_pending`), with the same
  cleanup as the escape key: `\r\n` printed, the terminal restored, the
  master closed, the channel child hung up and reaped.

So `-p "login: root" -p "Password: xx" -p "]# " -s "ls" -- ssh -tt
host` answers the login prompts, runs `ls` at the first shell prompt,
and detaches when the prompt returns (`-p "]# ls"` queues the same
command implicitly). `--send` without a sentinel is refused with exit
code 2: there is no prompt boundary to type at, and the command would
go in blind.

The accounting rests on the sentinel's own premise: a prompt is the
peer waiting for input, and input is what gets typed -- every prompt
after a typed line is that line's completion. The startup Enter is
deliberately not accounted: a serial console may swallow it (the
startup phase retries it), and its answer is the first prompt the main
loop sees either way.

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
     immediately -- unless commands are queued, where the push waits
     for the sentinel's own firing (one line per prompt, above).
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

- a target pty plays the remote session (`open_target()`: a pty
  pre-configured raw, the way the session behind it would have it),
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
command stays pty), the serial-console startup phase (a fake `virsh`
that answers the first Enter, and one that stays silent through it and
answers the retried Enter; a prompt printed before the pty goes raw
matched during the wait; a channel that switches with `TCSAFLUSH`
receiving the Enter only after the switch; `-w` holding the Enter
until a banner split across two reads has appeared, and for at least
the 100ms settle time after it, both when the
channel goes raw before printing it and when it prints it before a
`TCSAFLUSH` switch; `-w` warned about and ignored on a pty channel,
an empty `-w` refused), and COMMAND mode (bytes
relayed to the
child, a prompt printed before the channel goes raw matched by the
main loop, with the simulated Enter typed for `--pty` only and
withheld from pty channels, the attached pty's settings left untouched
in `--pty` mode, local window size inherited by the new pty, child
exit closing the bridge, exec failure, escape detaching without
strays), the command queue (a login flow ended by a command run at the
prompt with the bridge exiting by itself, commands running in `-p`
order one per prompt, a command listed before the sentinel still
queued -- traced by `-v` --, the serial ordering with the TERM+stty
push taking the first prompt before any command, the `--pty`
counterpart, and the explicit `-s/--send` spelling with its
sentinel-less refusal), the raw-mode output flags (the local terminal
keeps them, piped and interactive alike -- the channel pty is the one
created with `OPOST` off), the piped CRLF translation (an inline
`\r\n` pair dropped, a lone `\r` kept, a `\r`/`\n` split across
chunks merged, a trailing `\r` flushed at exit, and the whole piped
stream -- the echoed command included -- free of `\r` while patterns
and the self-exit still work), and the
verbose trace (`-v`: parsed patterns,
matching tail, matches and replies on stderr). The test
compiles the binary itself if it is missing and exits non-zero on any
failure:

```sh
python3 tests/test_pty_bridge.py
```
