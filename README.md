# mssh

Multi-hop SSH in one file. Chain any number of jump hosts, copy files across
the whole chain, and — the part plain `ssh` cannot do — keep a remote shell
alive between separate commands.

```bash
mssh -j ops@10.0.0.1 -j dmz@10.1.0.4 root@192.168.1.10
```

Every hop is reached through the previous one over a forwarded channel, so no
credential and no payload is ever exposed to an intermediate host beyond the
TCP forwarding itself.

## Why

`ssh -J` already chains hosts, so `mssh` exists for the things around that:

- **One file, no config.** A single Python script with one dependency. Nothing
  in `~/.ssh/config`, nothing to install on any hop. Drop it on a bastion and
  it works.
- **The whole chain on the command line.** Each hop carries its own user, port
  and (if you must) password, so a chain that exists only in a ticket or a
  script does not need six `Host` stanzas first.
- **Persistent sessions.** `--session NAME` keeps the chain *and* a remote
  shell alive across separate invocations, so a series of commands share one
  working directory, one set of variables, one loaded debugger. This is what
  `ssh -J` has no answer for, and it is the reason this tool exists.

That last point is aimed squarely at automation. A script — or an AI agent —
runs one command per process, so `ssh host 'cd /var/log'` accomplishes nothing:
the `cd`, and the three-hop chain that took seconds to build, are gone before
the next call. `--session` puts the state in a local daemon instead, and each
call becomes a short reconnect to something that is already logged in.

## Requirements

- Python 3.6 or newer
- [paramiko](https://www.paramiko.org/) — `pip3 install paramiko`

```bash
chmod +x mssh
sudo cp mssh /usr/local/bin/
```

The shebang is `#!/usr/bin/env python3`. If the interpreter that owns your
paramiko install is not the one `python3` resolves to, run it explicitly
(`python3.12 mssh ...`) or edit the first line.

## Endpoint format

Every host, jump or target, is written the same way:

```
[user[:password]@]host[:port]
```

- Port defaults to 22: `user@host`
- The user defaults to the local one, so the credential part may go too:
  `10.0.0.9`, exactly as `ssh` would take it
- The password goes **before** the `@`: `user:secret@host`
- The credential part splits on the **last** `@`, and the password is
  everything after the first `:`, so both characters are fine inside it:
  `user:p:a@ss@host:22`
- Omit the password to use a key, `ssh-agent`, or an interactive prompt
- IPv6 must be bracketed: `user:pw@[::1]:22`

> **Passwords on a command line are visible in `ps` and in your shell
> history.** Prefer keys, or omit the password and let `mssh` prompt for it.

## Logging in

```bash
# no jump at all: a direct login as the local user
mssh 10.0.0.9

# one jump host
mssh -j ops@10.0.0.1 root@192.168.1.10

# several, applied in the order given
mssh -j a@1.1.1.1:2222 -j b@10.0.0.5 app@172.16.0.9

# run a command instead of opening a shell
mssh -j ops@10.0.0.1 root@10.0.0.9 -c 'uptime; df -h'
```

`-c` exits with the remote command's own exit code, and stdin is forwarded, so
`mssh host -c 'cat > /tmp/f' < local` works as you would expect. Add `-t` to
force a pty for something that insists on one.

### Shell initialization

`--rcfile FILE` reads a local shell script before connecting, uploads it to
a private mode-0600 temporary file directly under the target's `/tmp`, and
loads it in the new shell with `.` (source):

```bash
mssh --rcfile ./init.sh -j ops@10.0.0.1 root@10.0.0.9
mssh --session web --rcfile ./init.sh root@10.0.0.9
```

The bundled `mssh.rc` provides a minimal default initialization script.
It is opt-in, not automatically loaded:

```bash
mssh --rcfile ./mssh.rc root@10.0.0.9
mssh --session web --rcfile ./mssh.rc root@10.0.0.9
```

It exports `MSSH=1` to identify an initialized mssh shell,
`MSSH_RC_LOADED=1` to indicate the script was loaded, and
`MSSH_RC_VERSION=1` to identify the bundled script format. These are
informational markers, not authentication or proof of session identity.
It also unsets writable `TMOUT`, silently tolerating a read-only variable.
The script leaves PATH, prompts, history settings and working directory
unchanged, and can be customized for the target environment.

Variables, functions, shell options and working-directory changes remain in
that shell. For example, `init.sh` can contain:

```sh
unset TMOUT || return 1
export EDITOR=vi
umask 027
```

Initialization input echoes, markers and script stdout/stderr are hidden;
the login banner and subsequent prompt are retained. The script reads
`/dev/null`, so it must not prompt for input. A nonzero return or an
initialization timeout (using `--timeout`) fails the new shell. Cleanup runs
in that same shell for direct logins immediately after the script returns,
including a nonzero return; no additional cleanup channel is opened. If the script exits or
replaces the shell, hangs, or the connection is lost, temporary files may remain.
Remote artifacts share the naming convention `/tmp/.mssh-<kind>-<random>`:
`rc` for initialization files and `in` for stdin FIFOs. Names use locally
generated 16-character suffixes using ASCII letters (both cases) and digits.
Files and FIFOs are created exclusively with mode
0600, so an existing path is not overwritten.
The target needs a sh-compatible shell, `cat` and `rm`;
SFTP is not required.

On Bash, the initialization command removes only its own in-memory history
entry before sourcing the script, without disabling subsequent user history.
Other shells do not have this guarantee. Previously saved initialization
entries are not removed, and remote audit or custom logging hooks are unaffected.

Shell sessions start a login Bash, falling back to a login `/bin/sh`, so
remote login profiles run before the local script. Reconnects rerun the
original script snapshot, not a newly read local file. A session retains its
remote rc file: fresh logins source that existing path without uploading again.
Each reconnect uploads a new copy. `--stop` removes the session's retained
files, including earlier copies from reconnects; other normal session shutdowns
also attempt cleanup. Cleanup cannot be guaranteed while the target is unreachable.
`mssh NAME --status` displays the current remote path on the `rcfile:` line,
or `none` when no rc file is configured.
An explicit `--rcfile` overrides the retained file for that login only and
is deleted after source. Ordinary commands in an existing session do not
rerun initialization. `--rcfile` cannot be combined with `-c`, `--exec`, file
copies or session control operations.

Use only trusted scripts compatible with the target shell. This option does
not bypass read-only variables or remote auditing. Relative references inside
the script resolve on the target; neighboring local files are not uploaded.

## Copying files

The same argument shape as `scp`: `SOURCE... DEST`, where the local side is a
plain path and the remote side is an endpoint with `:` and a path appended:

```
[user[:password]@]host[:port]:path
```

`path` may be absolute or relative to the remote `$HOME`, and a bare `:` with
nothing after it means that home directory.

```bash
mssh -j ops@10.0.0.1 ./app.tar root@10.0.0.9:/tmp/        # upload
mssh -j ops@10.0.0.1 root@10.0.0.9:/var/log/syslog ./     # download
mssh ./app.tar 10.0.0.9:/tmp/            # the user defaults here too
mssh ./a ./b root@10.0.0.9:/opt/pkg/     # several sources; DEST must be a dir
mssh -r ./dist root@10.0.0.9:/srv/www/   # -r recurses into directories
mssh -rp root@10.0.0.9:/etc/nginx ./     # -p also keeps the exact mode + mtime
mssh ./x root@10.0.0.9:                  # bare ':' means the remote $HOME
mssh ./x root@10.0.0.9:36001:/tmp/       # with a port
```

An argument is remote when whatever sits before its first `:` can be a host
name — scp's rule, and what makes `10.0.0.9:/tmp/` work without a user. Since
no host name holds a `/`, `./x` and `/tmp/a:b` stay local; but a *local* file
whose name holds a colon needs a `./` in front, or `notes:2024.txt` reads as
host `notes`.

Two spellings are ambiguous enough that `mssh` refuses them instead of
guessing, each naming the way round it:

- `10.0.0.9:8900` — host and port, as everywhere else in `mssh`, so it carries
  no path to copy. Write `10.0.0.9:8900:8900` for that remote file, or
  `./10.0.0.9:8900` for a local one.
- `10.0.0.9::8900` — the port field is empty, so this is either a port left out
  or a path starting with `:`. Write `10.0.0.9:./:8900` for the path, or fill
  the port in.

Transfers run over SFTP on the target and show progress on a tty. `-C`
compresses, which is worth it on a slow link and not otherwise.

**Permissions travel with the file, as under `scp`:** an executable arrives
executable. A file that already exists keeps its own mode, so overwriting a
`0600` config on the target never widens it. `-p` asks for the exact mode
instead, umask and all, plus the modification time.

The mode a new copy gets is the source's, filtered by **your** umask. `scp`
filters by the *receiving* side's, which SFTP gives no way to query; the two
agree whenever both ends are configured alike, and `-p` is there when the exact
bits matter.

Ctrl-C during a download removes the partial file rather than leaving something
that looks complete to whatever runs next.

## Persistent sessions

A normal run connects, does one thing, and tears everything down. `--session
NAME` leaves a small local daemon holding the chain and one long-lived remote
shell; later calls hand it one command each.

```bash
# first call carries the chain, then detaches
mssh --session s -j ops@10.0.0.1 root@10.0.0.9

# later calls need only the name
mssh --session s 'cd /var/log && ls'
mssh --session s 'grep -c error messages'      # exit code is grep's
mssh --session s 'X=1'; mssh --session s 'echo $X'      # prints 1
```

Once the session is live, `--session` can be left out: the name of a session
is also the name of the machine it reached — the whole chain, under an alias.

```bash
mssh -j ops@10.0.0.1 -j dmz@10.1.0.4 root@192.168.1.10 --session web
mssh web 'cd /var/log && ls'                   # = mssh --session web '...'
mssh web --status                              # and --stop, --interrupt
mssh web --exec -t 'vim /etc/nginx/nginx.conf'
mssh web                                       # nothing to run: a login
```

A word that no live session answers to is read as a host, exactly as before,
so `mssh 10.0.0.9` still logs in to `10.0.0.9` and every existing command line
keeps working.

That last line is the nearest thing to `ssh web`: it opens a shell on the
target, on **a channel of its own** rather than in the session's shell, which
is busy being a session — the framing around every command above is what makes
it one. Your terminal drives that shell the way it drives an `--exec -t`
program (forwarded as its input, raw until the shell ends, resized along with
your window, no `--wait`), and `mssh` exits with the shell's own exit code.

Separate processes, one shell: the working directory, the environment and any
background jobs live on between calls. Output streams as it is produced, so a
long-running command (a build, a poller, a `tail`) prints as it goes rather
than arriving all at once at the end.

Once a session is live, every positional is a command — nothing is guessed — so
a repeated start runs the endpoint as one and the shell reports it. `--status`
says whether a session is live, and `-j` or `-c` on a live one is refused.

Piping into a session works the way it does with `ssh`:

```bash
echo hello | mssh --session s cat               # prints hello
mssh --session s 'wc -l' < access.log           # counts the local file
tar cf - ./dir | mssh --session s 'tar xf - -C /srv'
```

Managing them:

```bash
mssh --session s --status      # target, uptime, commands served, idle/running
mssh --session s --interrupt   # Ctrl-C into the session, to unstick something
mssh --session s --stop        # end it and close the connection
mssh --sessions                # list every live session
```

### Surviving a reboot

A session is only as alive as its connection. A jump host restarting, the
target restarting, a firewall dropping the flow — any of them ends it, and the
next call reports it and exits 1. `--reconnect N`, on the call that *starts* a
session, builds the whole chain again instead:

```bash
mssh --session s --reconnect 20 -j ops@10.0.0.1 root@10.0.0.9
```

Attempts are 2 s, 4 s, 8 s, 16 s and then 30 s *apart* — a gap between
attempts, not a period. An attempt that has to time out adds its own cost on
top (up to `-o SEC`, 20 s by default, per hop), so attempts never overlap and
never pile up, and `--reconnect 20` spans at least the eight minutes of
waiting plus however long twenty failures take. In a reboot that is usually
one SYN timeout per attempt while the kernel is down and an instant
`connection refused` once the host is up but sshd is not, so the real window
is comfortably longer than the gaps alone — which is the right way round for
a physical machine.

They run in the background: `--status` shows the link and which attempt is
running, and the session keeps answering while they go on. A command that
arrives mid-attempt is told so and exits 1 rather than being held for the
length of a reboot; a command that was *running* when the link went is told
the same thing, since its output is lost either way.

The clock starts when the drop is noticed, which for a host that went away
without closing its TCP connection means when a write to it finally fails —
that is what the 30 s keepalive is for. A reset (an sshd restarting, a hop
rebooting) is noticed at once.

**The connection comes back; the shell does not.** A reconnect starts a *new*
shell, in the home directory, with none of the old environment and none of the
background jobs — so a `cd /var/log` from before the reboot is gone. That is
why it is never silent: the first command after a reconnect is told, on stderr,
before it runs.

```
mssh: session s reconnected to root@10.0.0.9:22 after the connection to
root@10.0.0.9:22 dropped.  This is a new shell: the working directory, the
environment and any background jobs of the old one are gone.
```

Two things it will not do:

- **Authenticate with a credential it does not have.** The daemon is detached
  from your terminal, so a password you typed at the prompt cannot be typed
  again. A key, an agent, or a password written into the endpoint
  (`user:secret@host`) reconnects; anything else is refused.
- **Rebuild a `-c` session.** There the state *is* the program — the gdb that
  was attached to pid 1234 does not exist after a reboot — so a new one would
  be a different thing wearing the same name.

A session that cannot reconnect does not disappear quietly: it stays up,
broken, until a command asks it for something, and that command gets the
reason and exit 1 as the session ends. The same is true once the attempts run
out — one more is tried when the next command arrives, since a command
arriving is evidence that somebody expects the target to be back.

Without `--reconnect` nothing changes: a dropped connection ends the session
immediately, as it always has.

### Borrowing the connection without the shell

Files can also use the existing session's connection, without rebuilding
the jump chain or authenticating again:

```bash
mssh web --put ./app.conf /tmp/app.conf
mssh web --get /var/log/app.log ./app.log
mssh web -r -p --put ./release /tmp/
mssh --session web --get /tmp/app.conf ./app.conf
```

`--put LOCAL REMOTE` and `--get REMOTE LOCAL` transfer one file or tree.
An existing destination directory receives the source's basename. `-r`
allows directories and `-p` preserves permissions and timestamps, following
the normal copy rules. Progress goes to stderr when it is a terminal.
Remote paths are SFTP paths: relative paths start at the SFTP subsystem's
initial directory, not the persistent shell's current directory; shell
variables and `~` are not expanded. The target must provide SFTP.

Transfers use their own channel and can run alongside shell commands or
other transfers, including in a session holding an interactive program.
An interrupted download removes its partial local file. `--interrupt`
also stops active transfer channels. Session channels detach their receive
notifications before closing, preventing late remote data from writing to
a closed Paramiko event pipe and disconnecting the shared transport.
Client-side prefetch cancellation hardening remains separate work; some
Paramiko versions can still print a background-thread traceback on Ctrl-C.
Connection failures are reported without automatically retrying transfers.
`--timeout` bounds socket inactivity, not total transfer duration.

Plenty of commands want the chain but nothing else: a `grep` over a log, a
`systemctl status`, a `cat` of a config. `--exec` runs those on a channel of
their own instead of in the session's shell.

```bash
mssh --session s --exec 'grep -c error /var/log/messages'
mssh --session s --exec 'cat /etc/nginx/nginx.conf' 2>/dev/null
mssh --session s --exec 'tar cf - /etc' > etc.tar
```

It shares the connection, not the shell: a `cd` from an earlier call does
**not** apply, and neither do that shell's variables, functions or background
jobs. What you get instead is everything the pty was costing —

- **stdout and stderr stay apart**, since nothing merges them, so
  `2>/dev/null` and `2>errors.log` do what they say.
- **The bytes are untouched.** No pty means no line discipline, so no `\r`
  before a `\n`, no 4095-byte line limit, and no control byte in the data
  being read as a signal. Binary output survives.
- **Nothing is framed.** The channel's own exit status ends the command, so
  there is no marker printed into the output and no `base64`, `printf` or
  POSIX shell needed on the target.
- **Several run at once**, including while the session's shell is busy with
  something else.

`--wait`, `--interrupt`, Ctrl-C, stdin and the exit code behave as they do for
a normal send, with one deliberate difference: a command that ends any way but
normally — a `--wait` timeout, an `--interrupt`, a client that walked away — is
**killed** rather than left running, because closing its channel is what ends
it and nothing else is waiting on that channel. A shell command in the same
position keeps running, since the next send has to share the same pty with it.

`--exec` works against a `-c` session too, where it is the only way to reach a
plain shell on the target.

#### A terminal for the programs that need one

Some commands refuse to run without one. `-t` asks for a pty on the `--exec`
channel, and then the call is a login rather than a command:

```bash
mssh --session s --exec -t 'vim /etc/hosts'
mssh --session s --exec -t top
mssh --session s --exec -t 'mysql -u root -p'
```

When the session is named as the first word (`mssh s ...` rather than
`--session s`), the first word after it that is not an option starts the command, and
everything from there is the command's, options included: `mssh s ls -l` runs
`ls -l`, `mssh s --exec -t top -d 1` runs `top -d 1`. Put mssh's own options
before the command (`mssh s --wait 5 'sleep 3'`); after it they belong to the command.
With `--session s` the options may still follow the command, and a command that
starts with a dash needs `--` in front of it.

With `-t`:

- **Your terminal is forwarded as the program's input**, which is the one case
  the default rule holds back — here it is exactly what the program reads.
- **Your terminal goes raw** for as long as the command runs and is restored
  after. The remote pty is the line discipline now; a local one still echoing
  and editing would do all of it twice.
- **Resizing your window resizes the remote one**, so a full-screen program
  keeps painting the right rectangle.
- **`--wait` defaults to 0** — indefinite. Time spent in an editor is not
  evidence that anything is stuck.

All of that needs a terminal on **both** ends. Redirect either one and the
command runs without a pty, as it would without `-t`: there would be nothing
to put in raw mode, no size to send, and `--exec -t top > log` would otherwise
fill the file with escape sequences.

`-t` applies to `--exec` only. The session's own shell already runs behind a
pty, and a command sent to it reads a fifo rather than your terminal.

Use a normal send when the command depends on what came before it, and
`--exec` when it does not.

### Holding a program instead of a shell

With `-c`, the session holds an interactive program, and `--prompt` gives the
regex that marks the end of one of its replies:

```bash
mssh --session g --prompt '\(gdb\) $' -j ops@10.0.0.1 root@10.0.0.9 \
     -c 'gdb -q -ex "set pagination off" -p 1234'
mssh --session g 'break do_work'
mssh --session g 'bt'
```

Verified against `gdb -q` (`'\(gdb\) $'`), `python3 -i` (`'>>> $'`) and
`sqlite3` (`'sqlite> $'`), with nothing injected into the program. Without
`--prompt`, `mssh` falls back to treating a pause in the output as the end
(`--idle SEC`, default 0.4) — workable, but a guess; name the prompt if you
can.

### Things worth knowing

- **Output is stdout and stderr merged**, in the order the program wrote them,
  because the session runs behind a pty. The exit code, not the stream, is what
  tells you whether a command failed. (A pty is not optional here: without one
  the two streams arrive unordered, and Ctrl-C cannot be delivered correctly.)
  `--exec` has no pty and keeps them apart, at the cost of the shell's state.
- **stdin is forwarded when it is a pipe or a file**, so `echo hello | mssh
  --session s cat` behaves as it would through `ssh`, and it streams — a filter
  fed by a slow producer prints as it goes. The command still runs in the
  session's own shell, so a `cd` from an earlier call still applies. A terminal
  is *not* forwarded by default: with `--stdin` the command reads until you
  type Ctrl-D, which is what you want for `cat` and not for a plain
  `mssh --session s ls`.
  `-n` never forwards, for when mssh's stdin belongs to something else, such as
  a script being read from a pipe. `/dev/null` is never forwarded — it cannot be
  told apart from having no input, so a bare `cat` still ends instead of wedging
  the session.
- **`--wait SEC`** (default 30, or 0 for a login and with `--exec -t`) bounds how long one
  command is watched; `0` means indefinitely. A timeout exits 124 and leaves
  the session usable — the next command stops whatever was still running.
- **Ctrl-C interrupts the remote command**, not your local process, so the
  session survives it. Press it twice to walk away and leave the command
  running.
- **A program that paginates will stall at its pager** until `--interrupt`, so
  turn paging off in the program itself (`gdb -ex 'set pagination off'`).
- **`--session-idle SEC`** ends a session after that much quiet, so a forgotten
  one does not hold a root channel open forever. Default `0`: never.
- The first call keeps your terminal until the chain is up, so a hop that needs
  a password can still ask for one and `-vv` shows each hop. Only then
  does it detach.

Sessions live in `~/.mssh/sessions`, one socket each, mode `0600` inside a
`0700` directory that is re-checked on every use, and the daemon refuses any
peer whose uid is not yours. **Holding one of those sockets is equivalent to
being logged in on the target**, so it is guarded like a private key rather
than like a temp file.

### Daemon diagnostics

Choose the log level when creating a session:

```bash
mssh --session web -v TARGET     # WARNING and above
mssh --session web -vv TARGET    # INFO and above
mssh --session web -vvv TARGET   # DEBUG and above
```

The same thresholds apply to structured mssh and Paramiko records. Without
`-v`, logging is disabled. Daemon lifecycle events use INFO; request tracing
and expected client disconnects use DEBUG. Existing connection progress
notices are printed with any verbosity flag, independently of these levels.

Startup logs remain on the terminal; after detaching, stderr is appended
to `~/.mssh/sessions/web.log`, with mode `0600`. Restarting the same session
appends to that file. Without `-v` at creation, the daemon's stderr goes to
`/dev/null` and no log file is created. Later client verbosity does not
change the running daemon's log level.

With any verbosity flag, a stderr notice before detaching names the log
file. After detaching, the file receives a timestamped service-started
notice with the PID and configured log level, using the structured log
format without repeating the path. This startup notice is always emitted,
even with a single `-v`; its INFO severity does not change the threshold
for subsequent records.

Structured daemon and Paramiko records include timestamps, severity, PID,
thread name and session name. Request workers also carry a request ID and
operation; channel events include channel ID, EOF/closed flags and transport
state. Events cover startup, requests, shell/exec/SFTP I/O, channel cleanup,
rcfile cleanup, reconnect attempts and daemon exit.

Failures preserve original exception tracebacks. Paramiko ERROR records
capture the current exception, and link-failure events also report the
transport's saved exception without consuming it. This distinguishes a TCP
failure from an internal channel event-pipe error.

Logging does not change command stdout/stderr forwarding or wire replies.
Structured events omit command text, stdin and rcfile contents. Exception
messages, filenames, remote errors and hop diagnostics can still contain
sensitive information; treat these logs as private. Logs are not
automatically rotated or deleted by `--stop`.

## Key setup

Password prompts on every hop get old quickly. Install a key once:

```bash
ssh-keygen -t ed25519                              # if you have none yet
mssh --copy-id -j ops:pw1@10.0.0.1 root:pw2@10.0.0.9
mssh -j ops@10.0.0.1 root@10.0.0.9                 # key auth from now on
```

`--copy-id` appends the key to `~/.ssh/authorized_keys` on **every** hop, then
exits without opening a shell. It defaults to `~/.ssh/id_ed25519.pub` or
`id_rsa.pub`; name a file to pick another. It refuses to upload anything that
looks like a private key.

`-i KEYFILE` offers a specific key (repeatable, applied to every hop) and
prompts once for the passphrase if it is encrypted. An OpenSSH certificate is
used automatically when `<key>-cert.pub` sits next to the private key.

Supported: **ed25519** (recommended), **ecdsa** (nistp256/384/521) and **rsa**.
DSA is gone — paramiko 5 dropped it, as did OpenSSH 9.8.

## Host keys

By default an unknown host key is added and the connection proceeds, which is
what makes a one-off chain to a machine you have never met usable. `--strict`
opts into real verification and rejects anything unknown; `--known-hosts FILE`
points at a specific file.

## Options

| Option | Meaning |
| --- | --- |
| `-j, --jump SPEC` | Add a jump host; repeat for each hop, in order |
| `-c, --command CMD` | Run `CMD` instead of opening a shell |
| `-t, --force-tty` | Allocate a pty even with `-c`; with `--session --exec`, run the command on a terminal |
| `-n, --no-stdin` | Never forward stdin; the remote command reads `/dev/null` |
| `-r, --recursive` | Copy directories recursively |
| `-p, --preserve` | Keep the exact mode and mtime on copied files |
| `-i, --identity KEYFILE` | Private key to authenticate with; repeatable |
| `--copy-id [PUBKEY]` | Install a public key on every hop, then exit |
| `-o, --timeout SEC` | Per-hop connect timeout (default 20) |
| `-k, --keepalive SEC` | Keepalive interval, 0 to disable (default 30) |
| `-C, --compress` | Enable compression |
| `--strict` | Reject unknown host keys |
| `--known-hosts FILE` | `known_hosts` file to load |
| `--no-agent` | Do not use `ssh-agent` |
| `--no-keys` | Do not look for keys in `~/.ssh` |
| `--no-prompt` | Never prompt for a password; fail instead |
| `-v, --verbose` | Log to stderr: `-v` WARNING, `-vv` INFO, `-vvv` DEBUG |
| `-V, --version` | Print the version |

Session-only options: `--session NAME`, `--sessions`, `--status`, `--stop`,
`--interrupt`, `--exec`, `--prompt REGEX`, `--stdin`, `--idle SEC`,
`--wait SEC`, `--reconnect N`, `--session-idle SEC`.

`mssh --help` carries the same detail plus worked examples.

## Exit codes

| Code | Meaning |
| --- | --- |
| *remote* | The remote command's own exit code (`-c`, or a session send) |
| `0` | Success — a login that ended cleanly, a copy, `--copy-id` |
| `1` | mssh could not do it: auth failed, host unreachable, no such file, no such session, or `--sessions` with none live |
| `2` | Bad usage — unknown flag, missing target, an invalid session name |
| `3` | paramiko is not installed |
| `124` | A session command did not finish within `--wait` |
| `130` | Interrupted (Ctrl-C) |

## Idle connections

A keepalive goes out every 30 s (`-k SEC`, `0` to disable), so an idle session
survives NAT and firewall timeouts. It cannot override a server-side
`ClientAliveInterval` or a shell `TMOUT` — those still apply, and a session
that dies that way reports it on the next call, or rebuilds itself if it was
started with [`--reconnect`](#surviving-a-reboot).

## Legacy servers

Against OpenSSH older than 7.8, RSA keys do not work: those servers accept only
SHA-1 `ssh-rsa` signatures, which paramiko 5 no longer produces. Use an ed25519
key (`ssh-keygen -t ed25519`). This is why ed25519 leads the default key search
order.

## Tests

```bash
python3 tests/test_session_unit.py     # framing, endpoints, flags     (61)
python3 tests/test_session_daemon.py   # daemon, protocol, client      (62)
python3 tests/test_copy_mode.py        # copy permissions vs real scp (51)
```

No network and no sshd: the framing layer is driven against local `bash`,
`gdb` and `python3` behind a real pty, the daemon tests run mssh as separate
processes with only the SSH hops faked, and the copy tests talk to OpenSSH's
own `sftp-server` over a pipe and compare every resulting mode against what
`scp` itself produces for the same input. Some of the permission tests drop to
an unprivileged user, since root ignores permission bits and would pass either
way.
