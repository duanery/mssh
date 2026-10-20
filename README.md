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

## Contents

- [Requirements](#requirements)
- [Endpoint Format](#endpoint-format)
- [Logging In](#logging-in)
- [Authentication](#authentication)
- [Copying Files](#copying-files)
- [Persistent Sessions](#persistent-sessions)
- [Options](#options)
- [Exit Codes](#exit-codes)
- [Tests](#tests)

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

## Endpoint Format

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

## Logging In

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

### Shell Initialization

`--rcfile FILE` reads a local shell script before connecting, uploads it to
a private mode-0600 temporary file directly under the target's `/tmp`, and
loads it in the new shell with `.` (source):

```bash
mssh --rcfile ./init.sh -j ops@10.0.0.1 root@10.0.0.9
```

The bundled `mssh.rc` provides a minimal default initialization script.
It is opt-in, not automatically loaded:

```bash
mssh --rcfile ./mssh.rc root@10.0.0.9
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
including a nonzero return; no additional cleanup channel is opened. If the
script exits or replaces the shell, hangs, or the connection is lost,
temporary files may remain. Files are created exclusively with mode `0600`,
so an existing path is not overwritten. The target needs a sh-compatible
shell, `cat` and `rm`; SFTP is not required.

On Bash, the initialization command removes only its own in-memory history
entry before sourcing the script, without disabling subsequent user history.
Other shells do not have this guarantee. Previously saved initialization
entries are not removed, and remote audit or custom logging hooks are unaffected.

`--rcfile` cannot be combined with `-c`, `--exec`, file copies or session
control operations. For retained initialization scripts and reconnect
behavior, see [Session Initialization](#session-initialization).

Use only trusted scripts compatible with the target shell. This option does
not bypass read-only variables or remote auditing. Relative references inside
the script resolve on the target; neighboring local files are not uploaded.

## Authentication

Client keys authenticate you to each hop; host keys identify the servers
you connect to. Both apply to direct connections and persistent sessions.

### Key Setup

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

### Host Keys

By default an unknown host key is added and the connection proceeds, which is
what makes a one-off chain to a machine you have never met usable. `--strict`
opts into real verification and rejects anything unknown; `--known-hosts FILE`
points at a specific file.

### Legacy Servers

Against OpenSSH older than 7.8, RSA keys do not work: those servers accept only
SHA-1 `ssh-rsa` signatures, which paramiko 5 no longer produces. Use an ed25519
key (`ssh-keygen -t ed25519`). This is why ed25519 leads the default key search
order.

## Copying Files

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

For copies through an already-open connection, see session
[File Transfers](#file-transfers) and
[File Synchronization with rsync](#file-synchronization-with-rsync).

## Persistent Sessions

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
mssh web                                       # nothing to run: a login
```

A word that no live session answers to is read as a host, exactly as before,
so `mssh 10.0.0.9` still logs in to `10.0.0.9` and every existing command line
keeps working.

`mssh web` without a command opens a fresh login shell on a separate channel,
not the persistent shell that receives ordinary commands. See
[Interactive Terminals](#interactive-terminals) for terminal behavior.

Separate processes, one shell: the working directory, the environment and any
background jobs live on between calls. Output streams as it is produced, so a
long-running command (a build, a poller, a `tail`) prints as it goes rather
than arriving all at once at the end.

Once a session is live, every positional is a command — nothing is guessed — so
a repeated start runs the endpoint as one and the shell reports it. `--status`
says whether a session is live, and `-j` or `-c` on a live one is refused.

The first call keeps your terminal until the chain is up, so a hop can ask
for a password and `-vv` can show connection progress. Only then does the
daemon detach. Later calls reuse that authenticated connection.

Session topics:

- [Command Execution](#command-execution)
- [Independent Commands](#independent-commands)
- [Holding a Program Instead of a Shell](#holding-a-program-instead-of-a-shell)
- [Session Initialization](#session-initialization)
- [Jobs and Session Control](#jobs-and-session-control)
- [File Transfers](#file-transfers)
- [File Synchronization with rsync](#file-synchronization-with-rsync)
- [Port Forwarding](#port-forwarding)
- [Surviving a Reboot](#surviving-a-reboot)
- [Session Storage and Security](#session-storage-and-security)
- [Daemon Diagnostics](#daemon-diagnostics)

For client/server message layouts, descriptor ownership and request
lifecycles, see [Session Client/Server Interaction](session-protocol.md).

### Command Execution

Ordinary commands run sequentially in the persistent shell, keeping its
working directory, variables and functions. stdout and stderr are merged
by the shell's PTY; use the exit code to detect failure. Independent
`--exec` commands can run concurrently and keep the streams separate.

When the session is named as the first word (`mssh s ...` rather than
`--session s`), the first non-option after the name starts the command.
Everything from there belongs to the command, including its options:

```bash
mssh s ls -l
mssh s --wait 5 'sleep 3'
mssh s --exec -t top -d 1
```

Put mssh's own options before the command. With explicit `--session s`,
options may also follow the command; use `--` to separate command arguments
that could be mistaken for mssh options.

#### Input and Output

Pipes and regular files are forwarded automatically, and both input and
output stream as they arrive:

```bash
echo hello | mssh --session s cat
mssh --session s 'wc -l' < access.log
tar cf - ./dir | mssh --session s 'tar xf - -C /srv'
```

A terminal is not forwarded for ordinary commands unless `--stdin` is
specified; then programs such as `cat` read until Ctrl-D. `-n` suppresses
stdin forwarding, useful when a local script owns stdin. `/dev/null` is
treated as no input, so a bare `cat` ends instead of waiting indefinitely.
Login shells and `--exec -t` use the interactive terminal rules below.

#### Waiting and Interrupting

`--wait SEC` bounds how long the client watches one command. The default is
30 seconds, or `0` for a login and `--exec -t`; `0` means indefinitely.
A timeout exits `124` and leaves the session usable. A primary-shell
request stays pending until the next request or interruption resynchronizes
the shell; an independent exec request closes its own channel on timeout.
If the primary program ignores Ctrl-C and recovery cannot reach the shell,
the request returns an error and keeps the original job pending instead of
ending the session or sending new work into that program. Independent
`--exec` commands, jobs/status queries and forwards remain available.

Ctrl-C requests interruption of the current task while keeping the session
alive. A second Ctrl-C lets the client leave without waiting; it does not
guarantee that the remote process keeps running. Use `--jobs` and targeted
`--interrupt --job ID` to inspect and cancel a task from another client.

### Independent Commands

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
jobs. Without a PTY:

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

Input forwarding, `--wait` and Ctrl-C use the command rules above. On a
timeout, targeted interruption or client disconnect, mssh closes this
request's channel and removes its job instead of keeping a pending request.
Closing an SSH channel is not a guarantee that every remote process or
descendant is killed; do not use client disconnection as process control.

Use an ordinary command when it depends on earlier shell state; use
`--exec` for isolated commands or binary streams. It also works when the
session holds an interactive program instead of a shell.

#### Interactive Terminals

Some commands need a terminal. `--exec -t` requests a PTY on the command's
own channel; a session name without a command opens a fresh login shell:

```bash
mssh --session s --exec -t 'vim /etc/hosts'
mssh --session s --exec -t top
mssh --session s --exec -t 'mysql -u root -p'
mssh --session s --exec -t 'man ssh'
mssh s
```

The login shell uses the existing connection, but does not inherit the
persistent shell's working directory or current variables. It runs remote
login profiles and any configured session initialization script.

For an interactive login or a command with a PTY:

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

Within a session, explicit `-t` applies to `--exec`, not ordinary commands.
The persistent shell already runs behind a PTY; forwarded input reaches
ordinary commands through a FIFO rather than the interactive terminal.
Its `TERM` is `dumb`, and initialization sets `PAGER`, `MANPAGER`, `GIT_PAGER`
and `SYSTEMD_PAGER` to `cat`, after login profiles. Ordinary `man`, Git and
systemd commands therefore stream output instead of waiting in a pager.
An explicit `--rcfile` or later shell command can override these defaults;
read-only variables are left unchanged without failing initialization.
Explicitly running `less` or enabling a pager still requires an interactive
login or `--exec -t`; changing `TERM` alone does not forward pager keystrokes.
The defaults do not affect independent exec channels or fresh login shells.

### Holding a Program Instead of a Shell

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

Disable paging in the program itself, as in the gdb example, or a command
can stall at its pager until interrupted. Ordinary requests go to the
held program; use `--exec` for independent system commands. A `-c` session
cannot rebuild the program's state after a connection loss.

### Session Initialization

Apply a local initialization script when creating a shell session:

```bash
mssh --session web --rcfile init.sh root@target
mssh --session web --rcfile mssh.rc root@target
```

These are alternative starts, not commands to run on an existing session.
See [Shell Initialization](#shell-initialization) for script contents,
hidden output, history handling and the bundled `mssh.rc` markers.

Shell sessions start a login Bash, falling back to a login `/bin/sh`, so
remote login profiles run before the local script. A session retains the
uploaded rcfile: fresh logins source that path without uploading again.
Ordinary commands do not rerun initialization, and independent `--exec`
commands do not inherit the persistent shell's initialization environment.

An explicit `--rcfile` on a fresh login overrides the retained script for
that login only and is removed after sourcing:

```bash
mssh web --rcfile login-init.sh
```

Each reconnect uploads the original script snapshot to a new path; changes
to the local file after session creation are not picked up. `--stop` and
other normal shutdowns attempt to remove retained files, including copies
from earlier reconnects. Cleanup is not guaranteed when the target is
unreachable. `mssh web --status` shows the current path on the `rcfile:`
line, or `none` when no script is configured.

### Jobs and Session Control

```bash
mssh web --status             # session/link state, activity and job counts
mssh web --jobs               # individual tasks and forwarding listeners
mssh web --interrupt          # only the primary shell/program's current task
mssh web --interrupt --job 3  # interrupt one specific task
mssh web --job 3 --stop       # same cancellation, keep the session alive
mssh web --job 3 --connections # connections and channels for one forward
mssh web --job 3 --connection 2 --stop # close only this connection
mssh web --stop               # end the whole session and close its resources
mssh --sessions              # list every live session and its job count
```

#### Job Selection

`mssh NAME --jobs` lists mssh-managed tasks with their ID, type, channel,
state, elapsed time and command or transfer paths. It includes active shell
or interactive requests, exec/login channels, SFTP transfers and forwards.
When forwards are present, `CONNS` shows each listener's current accepted
connection count; non-forward rows leave that column blank. Pending SOCKS
handshakes, SSH channel opens and local destination connects are included.
It is not a cumulative count or a count of remote service connections.
The idle persistent shell or `-c` program itself is not a job. Each request
on its primary channel creates a default job, removed when the reply completes.
A timed-out or detached request remains `pending` until resynchronized.
If the SSH connection drops, its default job is removed rather than carried
through reconnection. Internal shell initialization is not listed as a job;
the replacement shell starts with no default job.
This is a daemon snapshot, not a remote process list: background processes
started by the shell are not tracked individually. Finished tasks disappear;
jobs are not saved as history, and IDs restart when the daemon is recreated.

`--interrupt` without `--job` leaves independent exec/login/SFTP tasks and
all forwarding listeners alone.
For a `-c` session it interrupts the current interactive request.
With no default job, it reports that nothing is running.
Pending-shell recovery makes two bounded attempts (about 10 seconds total).
If no synchronization marker arrives, `--interrupt` reports an error rather
than success and retains the pending job. Use `--exec` for independent work
or stop and recreate the session when the foreground program cannot be
recovered. A genuine channel/transport failure still follows normal
session termination or reconnect rules.
`--job ID` requires `--interrupt`, `--stop` or `--connections`; an unknown
or completed ID reports an error. `--stop --job ID` uses the same targeted
cancellation as `--interrupt --job ID`, leaving the session and other jobs alive.
Only `--stop` without `--job` ends the entire session.
Interrupting a default shell or interactive task keeps the primary channel
alive; interrupting an exec/login/SFTP task closes only that task's channel.
Client-side Ctrl-C on an exec/login request targets its own task, not the
default shell or other jobs. Stopping or exiting the session closes the
default channel and all job channels.

#### Forward Connections

`mssh NAME --job ID --connections` lists that forward's accepted connections
with their connection ID, SSH channel ID, state, elapsed time, source,
listener and destination. It works for `-L`, `-R` and `-D`, with TCP or Unix
endpoints. States are `handshake`, `connecting` and `active`. A pending
channel open shows `-` for its channel; unnamed Unix peers show `-` for their
source. SOCKS destinations are shown after the CONNECT request is parsed.
For `-R`, the source/listener are on the target and the destination is local.

`mssh NAME --job ID --connection ID --stop` closes only that connection's
socket and channel, including one still negotiating or connecting.
`--interrupt` can replace `--stop` here. The listener, other connections
and the session remain usable. Unknown or finished connection IDs and
non-forward jobs report an error without stopping anything else.

Connection IDs are scoped to each job, increase without reuse through
reconnect, and disappear when closed. Inspection and targeted cancellation
also work while the session is reconnecting, without triggering reconnect
or refreshing its activity timer. Older daemons need to be stopped and
recreated before these options are available.

#### Status and Listings

The `JOBS` column in `--sessions` uses the same selection rules as `--jobs`,
including pending requests and persistent forwards but excluding the idle
primary shell. It shows `?` when a count is unavailable from an older or
unresponsive daemon.

`--status` distinguishes idle, forwarding-only, running, pending,
interrupting, disconnected, reconnecting, stopping and ended sessions.
Connection and lifecycle states take precedence over surviving listeners.
The `default:` line reports the primary shell or interactive request
independently of exec/login tasks, transfers and proxies.

Status includes total jobs with a type breakdown (`forward` for `-L`,
`reverse` for `-R`, `socks` for `-D`), time since last activity and the idle
expiry setting. Commands, channel IDs and forwarding addresses are shown
only by `--jobs`; an unused listener does not mean traffic is flowing.
Queries do not wait for the shell, consume pending results, refresh
activity or start reconnect attempts. Older daemons show only the fields
they provide; missing task details are not reported as zero jobs.

For lightweight probe/list requests and full status message layouts, see
[Session Client/Server Interaction](session-protocol.md#jobs-status-and-cancellation).

### File Transfers

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
An interrupted download removes its partial local file.
`--interrupt --job ID` or `--stop --job ID` stops the selected transfer;
plain `--interrupt` does not stop transfers. Some Paramiko versions can
still print a client-side background-thread traceback on Ctrl-C; this is
separate from daemon channel cleanup. See
[Rcfile and Teardown Safety](session-protocol.md#rcfile-and-teardown-safety)
for implementation details.

Connection failures are reported without automatically retrying transfers.
`--timeout` bounds socket inactivity, not total transfer duration.

### File Synchronization with rsync

Use rsync's remote-shell mode to synchronize files through an existing
session. mssh provides the session's SSH connection; rsync handles file
comparison, incremental transfers, exclusions and resuming partial files.
Both the local machine and the target must have `rsync` installed and
available in `PATH`; SFTP is not required.

```bash
# Create the session once, then use its name as rsync's remote host.
mssh --session web root@target
rsync -a -e 'mssh --exec' tests web:/tmp/

# Synchronize build directory contents; allow transfers longer than 30s.
rsync -a --info=progress2 -e 'mssh --exec --wait 0' build/ web:/data/build/

# Download test results through the same session.
rsync -a -e 'mssh --exec --wait 0' web:/data/results/ results/

# Preview changes and exclude intermediate object files.
rsync -ani --exclude='*.o' -e 'mssh --exec --wait 0' build/ web:/data/build/

# Retain partial files; rerun the same command after an interruption.
rsync -a --partial-dir=.rsync-partial -e 'mssh --exec --wait 0' \
      build/ web:/data/build/
```

`web` is the name of a live session, not a new SSH host or login.
These examples assume `mssh` is in the local `PATH`; otherwise use its
absolute path in `-e` and when creating the session. The default command wait
is 30 seconds; `--wait 0` disables that limit, not rsync's own timeout handling.
Keep `--exec` and do not add `-t` or `-n`: rsync needs an unmodified,
bidirectional binary stream on stdin/stdout. Its remote process runs as an
independent exec job and can be inspected with `mssh web --jobs`.
The session shell's working directory and rcfile environment do not apply.

A trailing slash on `build/` copies its contents, while `tests` copies the
directory itself, producing `/tmp/tests`. Add rsync's `--delete` only when
destination files absent from the source should be removed, and preview
with `--dry-run` first. Partial files are managed by rsync, not mssh's
`--put/--get` cleanup. A dropped SSH connection still fails the current
transfer; after the session reconnects, rerun rsync to continue synchronizing.

### Port Forwarding

Forwarding is session-only. Configure listeners when creating a session or
add them later; the daemon retains them after the configuring client exits.

| Option | Listener | Destination |
| --- | --- | --- |
| `-L` | Local TCP or Unix socket | TCP or Unix service reached from the target |
| `-R` | Target-side TCP or Unix socket | TCP or Unix service reached from the local daemon |
| `-D` | Local TCP SOCKS proxy | Per-connection TCP destination reached from the target |

Repeat or combine `-L/-R/-D` in one request. All new listeners must bind
before success; failure rolls back that addition without changing existing
jobs. An existing-session configuration takes no command, login or copy
options. Each listener is one `forward` job; its accepted connections are
not separate jobs. Targeted stop/interrupt closes the listener and its
owned connections. Default `--interrupt` leaves it alone; whole-session
stop, exit and idle expiry close all listeners.

Traffic refreshes session activity, but an unused listener alone does not
prevent idle expiry. Connections do not survive a transport replacement.
The sections below describe endpoint-specific binding and reconnect rules.

#### Local Port Forwarding

`-L` uses SSH's four local forwarding forms, with a TCP or Unix-domain
listener owned by the session daemon:

```text
-L [bind_address:]port:host:hostport
-L [bind_address:]port:remote_socket
-L local_socket:host:hostport
-L local_socket:remote_socket
```

Configure forwards when creating a session or afterward:

```bash
mssh --session web -L 8080:127.0.0.1:80 -j ops@jump root@target
mssh web -L 15432:db.internal:5432
mssh --session web -L 127.0.0.1:9090:127.0.0.1:9090
mssh web -L 8080:/run/service.sock
mssh web -L /tmp/db.sock:db.internal:5432
mssh web -L /tmp/client.sock:/run/service.sock
mssh web --jobs
mssh web --interrupt --job 3
```

By default, the daemon listens only on `localhost` (IPv4 and IPv6 when
available). Specify `127.0.0.1` or `[::1]` to choose one family.
An explicit `*` or empty bind address listens on all interfaces; use that
only when exposing the forwarded service to other machines is intended.
IPv6 addresses require brackets, for example
`-L '[::1]:8080:[::1]:80'` or `-L '[::1]:8080:/run/service.sock'`.
TCP ports must be between 1 and 65535; dynamically assigned local ports
are not supported.

Socket paths must contain `/` to distinguish them from host names and
ports; write `./name.sock` for a relative path. Local relative paths are
resolved from the configuring client's working directory, even when adding
to an already-detached daemon. Remote socket paths are passed literally to
the target's SSH server: shell variables and `~` are not expanded.
Quote paths containing spaces. Escape literal `:` or `\` inside a path
with a backslash, for example `-L '/tmp/client\:1.sock:/run/service.sock'`.

Local socket files have mode `0600`. An existing path, including a stale
socket, causes a bind failure and is never overwritten. Stopping a forward,
session shutdown or a failed batch removes only the socket file created by
that forward; a replaced local path is preserved. Remote socket files are
never removed. Parent directories must already exist, and paths must fit
the operating system's Unix socket path limit.

The destination is reached through the target's SSH transport, so its host
name is resolved from the target, not from the local machine or jump host.
The SSH server must allow TCP forwarding for TCP destinations, or OpenSSH
`direct-streamlocal@openssh.com` forwarding for remote sockets
(`AllowStreamLocalForwarding`). Configuration success
confirms the local bind, not destination reachability: each incoming
connection opens its own SSH channel, and a refused destination closes only
that connection. Diagnostic failures go to the session log under `-v`.

With `--reconnect`, listeners and job IDs remain in the daemon. Existing
connections are lost when their SSH transport drops; new connections use
the rebuilt transport. Connections received while the link is down are
closed rather than queued or replayed.

#### Remote Port Forwarding

`-R` uses the same four endpoint forms as `-L`, but reverses their location:
the left endpoint listens on the target, and the right endpoint is connected
from the local session daemon.

```text
-R [bind_address:]port:host:hostport
-R [bind_address:]port:local_socket
-R remote_socket:host:hostport
-R remote_socket:local_socket
```

Configure at creation or afterward, for example to let a development target
reach your local artifact HTTP server or debugger:

```bash
mssh --session dev -R 8080:127.0.0.1:8000 -j ops@jump root@target
mssh dev -R 8081:/tmp/local-service.sock
mssh dev -R /tmp/target.sock:127.0.0.1:8000
mssh dev -R /tmp/target.sock:/tmp/local-service.sock
mssh dev --jobs
mssh dev --stop --job 3
# On the target, localhost:8080 now reaches the local machine's port 8000.
```

The configuring client exits after the target acknowledges the remote binds.
`--jobs` labels them `remote LISTENER -> local DESTINATION`; `--status`
counts them as `reverse`, without listing addresses. Cancellation is sent
without waiting for an SSH reply.

The default remote bind is `localhost`. Explicit `*` or an empty address
requests all interfaces, subject to the target's `GatewayPorts` policy:
sshd may restrict a wildcard to loopback or override the requested address.
TCP listeners require `AllowTcpForwarding` to permit remote forwarding,
and Unix listeners require `AllowStreamLocalForwarding`. Bracket IPv6
addresses, and use TCP ports from 1 to 65535; port zero and reverse dynamic
SOCKS forwarding are not supported.

Destination host names are resolved locally, not on the target or jump host.
Relative local destination socket paths are resolved in the configuring
client's directory. Remote listener paths are literal, with no shell or
`~` expansion. The same path escaping rules as `-L` apply.
mssh never removes or overwrites a remote listener path. sshd controls
permissions and stale-path replacement through `StreamLocalBindMask` and
`StreamLocalBindUnlink`; cancellation may leave a socket pathname behind.
Parent directories must already exist.

If binding is denied, the error names the remote listener and suggests
checks; SSH's failure reply does not carry a detailed reason. Check the
listener path/port on the target, its parent directory permissions and
the target's effective sshd forwarding policy. For example, if
`/tmp/echo.sock` already exists on the target and `StreamLocalBindUnlink`
is disabled, use a different remote listener path:

```bash
mssh dev -R /tmp/mssh-echo.sock:/tmp/echo.sock
```

Here `/tmp/mssh-echo.sock` is created on the target; `/tmp/echo.sock` is
the local destination. The destination is connected only after someone
connects to the remote listener, not during configuration. Never remove an
existing remote socket without first checking its owner and active users.

With `--reconnect`, remote listener jobs and IDs remain, but all connections
using the old transport are lost. The daemon reissues remote binds before
declaring the rebuilt session ready. A denied/conflicting bind fails that
rebuild and is reported through reconnect status and verbose diagnostics.
Unix listeners may need the target's `StreamLocalBindUnlink yes` policy to
rebind a retained path. Adding `-R` requires a live SSH transport; stopping an
existing job still works while disconnected. Destination failures affect only
that incoming connection. Older daemons must be stopped and recreated
before they can accept `-R`.

#### Dynamic SOCKS Proxies

`-D [bind_address:]port` creates a SOCKS proxy owned by the session daemon,
at startup or through a later client:

```bash
mssh --session web -D 1080 -j ops@jump root@target
mssh web -D 127.0.0.1:1081
mssh web -D '[::1]:1082'
curl --socks5-hostname 127.0.0.1:1080 http://service.internal/
mssh web --jobs
mssh web --stop --job 3
```

SOCKS4, SOCKS4a and SOCKS5 support TCP `CONNECT`. SOCKS4a and SOCKS5
domain names are passed to the target's SSH server for resolution, not
resolved on the local machine. Use a client option such as
`curl --socks5-hostname` when remote DNS is needed. SOCKS5 offers only
the no-authentication method; authentication, `BIND` and `UDP ASSOCIATE`
are not supported. The SSH server must permit `direct-tcpip` forwarding.
CONNECT is acknowledged only after the SSH channel has opened; a refused
destination affects that client connection, not the listener or session.

Default binding is `localhost`, with IPv4 and IPv6 when available.
Bracket explicit IPv6 addresses. Ports must be between 1 and 65535.
`*` or an empty bind address exposes the proxy on all interfaces:
there is no SOCKS authentication, so anyone who can reach that listener
can use the session's access to TCP services on the target's network.
Keep the default loopback binding unless this exposure is intended.

`--jobs` labels a proxy `bind -> SOCKS4/5`. Targeted stop/interrupt closes
pending handshakes as well as its listener and active connections.

With `--reconnect`, listeners and job IDs survive transport replacement;
connections do not. Requests made while disconnected fail rather than
being queued. Handshakes have a total `--timeout` limit, with a minimum
of one second, and bounded fields. Successful requests update activity.
Older session daemons must be stopped and recreated to accept `-D`.

### Surviving a Reboot

A jump host or target restarting, or a network connection dropping, can end
the shared SSH connection. Without reconnect, the session ends when that
loss is detected. Set `--reconnect N` when creating a shell session to
rebuild the whole chain instead:

```bash
mssh --session s --reconnect 20 -j ops@10.0.0.1 root@10.0.0.9
```

Attempts run in the background after delays of 2, 4, 8, 16 and then at most
30 seconds. Each connection attempt adds its own duration, bounded by
`--timeout` per hop; attempts do not overlap. Counting begins when the
daemon detects the loss, which can be delayed if the peer disappears
without closing TCP.

`--status` shows the link and current attempt. Queries and targeted stop
remain available, but commands arriving during reconnect exit `1` instead
of waiting for the whole reboot. Commands that were running when the link
failed are not replayed.

**The connection comes back; the old shell state does not.** A reconnect
starts a new login shell and reruns the retained rcfile snapshot, if any.
The old working directory, runtime environment changes and background jobs
are not restored. Remote login profiles and the rcfile may establish their
own environment and working directory in the new shell.

The first subsequent shell/exec command receives a notice on stderr before
it runs:

```text
mssh: session s reconnected to root@10.0.0.9:22 after the connection to
root@10.0.0.9:22 dropped.  This is a new shell: the working directory, the
environment and any background jobs of the old one are gone.
```

Automatic reconnect is refused in two cases:

- **Authentication required an interactively typed password.** The detached
  daemon cannot prompt again. Use a key, an agent, or an explicit endpoint
  password that remains available, while considering command-line exposure.
- **The session holds a `-c` program.** Restarting it would not restore the
  debugger, interpreter or other program state that the session held.

A session that cannot reconnect does not disappear quietly: it stays up,
broken, until a command asks it for something, and that command gets the
reason and exit 1 as the session ends. The same is true once the attempts run
out — one more is tried when the next command arrives, since a command
arriving is evidence that somebody expects the target to be back.

Other resources have different lifetimes:

| Resource | After reconnect |
| --- | --- |
| Primary shell/request | New shell; old default job is cleared |
| Session rcfile | Original local snapshot uploaded and sourced again |
| `-L/-D` listeners | Remain locally bound with the same job IDs |
| `-R` listeners | Rebound on the target with the same job IDs; bind failure fails that attempt |
| Accepted forwarding connections | Lost, not queued or replayed |
| Exec/login commands and SFTP transfers | Not automatically restarted |
| rsync synchronization | Rerun rsync after the session is ready |

#### Keepalive and Idle Expiry

Keepalive packets go out every 30 seconds by default (`-k SEC`, `0` to
disable), helping maintain idle connections through NATs and firewalls.
They do not override server-enforced disconnect policies. Session shell
setup unsets writable `TMOUT`; read-only settings cannot be bypassed.

`--session-idle SEC` ends the daemon after that much inactivity. Default
`0` disables expiry. Commands, forwarding configuration and forwarded
traffic refresh activity; status/jobs queries, keepalives and unused
forwarding listeners do not.
This is separate from `--idle`, which detects pauses in an interactive
program's output, and `--wait`, which limits one command's waiting time.

### Session Storage and Security

Sessions live in `~/.mssh/sessions`, one socket each, mode `0600` inside a
`0700` directory that is re-checked on every use, and the daemon refuses any
peer whose uid is not yours. **Holding one of those sockets is equivalent to
being logged in on the target**, so it is guarded like a private key rather
than like a temp file.

Remote temporary artifacts use `/tmp/.mssh-<kind>-<random>`: `rc` for
initialization files and `in` for forwarded-stdin FIFOs. Suffixes contain
16 ASCII letters/digits, generated locally. Files and FIFOs are created
exclusively with mode `0600`, so an existing path is not overwritten.
Cleanup is best-effort when a remote shell exits or its connection is lost.

For descriptor ownership, cleanup races and message formats, see
[Session Client/Server Interaction](session-protocol.md).

### Daemon Diagnostics

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
state. Events cover startup, requests, shell/exec/SFTP I/O, forwarding,
channel cleanup, rcfile cleanup, reconnect attempts and daemon exit.

Failures preserve original exception tracebacks. Paramiko ERROR records
capture the current exception, and link-failure events also report the
transport's saved exception without consuming it. This distinguishes a TCP
failure from an internal channel event-pipe error.

Logging does not change command stdout/stderr forwarding or wire replies.
Structured events omit command text, stdin and rcfile contents. Exception
messages, filenames, remote errors and hop diagnostics can still contain
sensitive information; treat these logs as private. Logs are not
automatically rotated or deleted by `--stop`.

## Options

| Option | Meaning |
| --- | --- |
| `-j, --jump SPEC` | Add a jump host; repeat for each hop, in order |
| `-c, --command CMD` | Run `CMD` instead of opening a shell |
| `--rcfile FILE` | Source a local initialization script in a new remote shell |
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
`--interrupt`, `--job ID`, `--jobs`, `--connections`, `--connection ID`,
`-L SPEC`, `-R SPEC`,
`-D [BIND:]PORT`, `--exec`,
`--put LOCAL REMOTE`, `--get REMOTE LOCAL`,
`--prompt REGEX`, `--stdin`, `--idle SEC`,
`--wait SEC`, `--reconnect N`, `--session-idle SEC`.

`mssh --help` carries the same detail plus worked examples.

## Exit Codes

| Code | Meaning |
| --- | --- |
| *remote* | The remote command's own exit code (`-c`, or a session send) |
| `0` | Success — a login that ended cleanly, a copy, `--copy-id` |
| `1` | mssh could not do it: auth failed, host unreachable, no such file, no such session, or `--sessions` with none live |
| `2` | Bad usage — unknown flag, missing target, an invalid session name |
| `3` | paramiko is not installed |
| `124` | A session command did not finish within `--wait` |
| `130` | Interrupted (Ctrl-C) |

## Tests

```bash
python3 tests/test_session_unit.py     # framing, endpoints, flags
python3 tests/test_session_daemon.py   # daemon, protocol, client
python3 tests/test_copy_mode.py        # copy permissions vs real scp
python3 -m unittest discover -s tests -p test_session_forward.py
python3 -m unittest discover -s tests -p test_session_unix_forward.py
python3 -m unittest discover -s tests -p test_session_remote_forward.py
python3 -m unittest discover -s tests -p test_session_socks.py
python3 -m unittest discover -s tests -p test_session_connections.py
python3 -m unittest discover -s tests -p test_session_pager.py
```

No external network: the framing layer is driven against local `bash`,
`gdb` and `python3` behind a real pty, the daemon tests run mssh as separate
processes with only the SSH hops faked, and the copy tests talk to OpenSSH's
own `sftp-server` over a pipe and compare every resulting mode against what
`scp` itself produces for the same input. Forwarding tests use local TCP
listeners and a real Paramiko client/server transport over a socketpair.
Unix forwarding tests also use local Unix sockets. When `sshd` is available
and the tests run as root, additional socketpair-based tests check actual
OpenSSH streamlocal channels; otherwise only those tests are skipped.
Remote forwarding tests cover all four TCP/Unix combinations, late bind
replies, cancellation races, atomic rollback and reconnect restoration.
SOCKS tests exercise fragmented requests, remote DNS, failures, cancellation,
reconnects and binary half-closes, including a real curl client when available.
Some of the permission tests drop to
an unprivileged user, since root ignores permission bits and would pass either
way.
