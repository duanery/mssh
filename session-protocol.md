# Session Client/Server Interaction

This document describes the current implementation in `mssh`, including
the local IPC protocol, remote channels, resource ownership and failure
handling. The session server is a **local daemon**, not a program installed
on the target. The target only runs SSH services, shells, commands and
SFTP subsystems.

This is an implementation reference, not a versioned public API.
For CLI examples, see [Persistent sessions](README.md#persistent-sessions).

## Contents

- [Architecture](#architecture)
- [Session Discovery and Startup](#session-discovery-and-startup)
- [Control Protocol](#control-protocol)
- [Primary Channel Requests](#primary-channel-requests)
- [Independent Commands and Logins](#independent-commands-and-logins)
- [File Transfers](#file-transfers)
- [Jobs, Status and Cancellation](#jobs-status-and-cancellation)
- [Persistent Forwarding](#persistent-forwarding)
- [Concurrency and Backpressure](#concurrency-and-backpressure)
- [Timeouts, Disconnects and Reconnect](#timeouts-disconnects-and-reconnect)
- [Rcfile and Teardown Safety](#rcfile-and-teardown-safety)
- [Compatibility, Security and Diagnostics](#compatibility-security-and-diagnostics)
- [Code and Test Map](#code-and-test-map)

## Architecture

```text
Local machine                                         Remote machines

mssh client --- Unix control socket --- session daemon --- SSH jump chain
     |                                      |                    |
     +--- output pipes / SFTP socketpair ----+             target transport
                                            |                    |
                                            +--- primary PTY shell/program
                                            +--- exec/login channels
                                            +--- SFTP channels
                                            +--- stdin writer channels
                                            +--- direct forwarding channels
```

The daemon owns the SSH chain and keeps one primary channel alive.
Later CLI processes connect to the local daemon instead of authenticating
again. Each request gets its own control connection and worker thread.

There are three separate communication layers:

| Layer | Carries | Lifetime |
| --- | --- | --- |
| Local Unix control socket | JSON requests, replies, notices and resize messages | One request |
| Local pipe or socketpair | Command output or binary SFTP traffic; optional stdin is passed as an FD | One command or transfer |
| SSH channel | Remote shell, command, SFTP or forwarded traffic | Primary channel, individual job or individual forwarded connection |

Command output and file contents do not travel inside JSON. The daemon's
own stderr is diagnostic output, separate from a remote command's stderr.

## Session Discovery and Startup

### Local Namespace

Sessions are stored under the client's `~/.mssh/sessions/`:

| Entry | Purpose |
| --- | --- |
| `NAME.sock` | Unix-domain `SOCK_STREAM` listener for that session |
| `.spawn.lock` | Shared lock protecting probe, stale-socket removal and bind |
| `NAME.log` | Appended daemon stderr when verbosity was enabled at creation |

Session names contain up to 64 characters from `A-Z`, `a-z`, `0-9`,
`.`, `_` and `-`, starting with a letter, digit or `_`.
The session directory is owner-only (`0700`); sockets and logs are `0600`.
The daemon also checks the peer UID when `SO_PEERCRED` is available.

`session_alive()` probes with `{"op": "status", "view": "probe"}` and gets
`{"ok": true}` without inspecting link state or jobs. A response means the
local daemon exists, not necessarily that its target connection is healthy.
`--sessions` scans socket names and requests `view: "list"` from each daemon;
there is no separate session registry server. These lightweight requests
avoid sending full status or job metadata.

The shorthand `mssh NAME ...` first checks whether `NAME` names a live
session. Otherwise it remains a target-host argument. Additional liveness
probes may precede the actual operation, so one CLI invocation can make
several short connections even though each connection carries one request.

### Startup Sequence

```text
Parent CLI                         Forked child / future daemon
    |                                           |
    | lock, probe, remove stale socket, bind     |
    | create readiness pipe                     |
    | fork ------------------------------------>|
    | close listener and readiness write end    |
    |                                           | keep controlling terminal
    |                                           | build SSH chain
    |                                           | start primary PTY program
    |                                           | initialize and check shell
    |                                           | upload/source retained rcfile
    |                                           | bind initial -L/-R/-D listeners
    |                                           | detach and redirect stdio
    |                                           | start listener workers
    |<---------- readiness JSON over pipe -------|
    | show startup output / forwarding job IDs   | accept local requests
    | exit                                      | remain alive
```

The listener is bound before SSH setup. The child keeps the controlling
terminal until authentication and initialization finish, allowing password
prompts and startup diagnostics. Readiness is sent only after detachment,
so the parent's return to the shell does not race with late terminal output.

The fork happens before creating Paramiko transports and their threads.
There is no second fork after SSH setup.

The private readiness pipe carries one JSON line:

```json
{"ok": true, "banner": "<base64 bytes>", "forwards": [{"id": 1, "command": "localhost:1080 -> SOCKS4/5"}]}
```

On failure the child reports the cause on its still-attached stderr and
sends `{"ok": false}`. The parent avoids printing the same error twice.
This exchange is not an `op` on `NAME.sock`.
Shell-mode setup consumes its probe output and returns an empty banner;
interactive-mode setup can return the program's startup output.

## Control Protocol

### Message Framing

Every control message is a UTF-8 JSON object followed by a literal newline.
Examples below omit that trailing newline for readability.
Stream reads can split or combine messages; receivers buffer until a whole
line is available. Binary bytes are never mixed into the JSON stream.

There is one initial request per control connection. `exec` can additionally
receive resize lines on that same connection while running.
Command requests can receive a notice before their final reply.

| Operation | Client half-closes after request? | Server response |
| --- | --- | --- |
| `status`, `jobs`, `connections`, `stop`, `interrupt`, `interrupt_connection`, `reset_shell`, `forward`, `remote_forward`, `dynamic_forward` | Yes, `shutdown(SHUT_WR)` | One JSON reply, then half-close |
| `send`, `exec` | No | Optional notice, then terminal reply |
| `sftp` | No | Setup acknowledgement; connection remains open as a lifetime guard |

Do not half-close a streaming request immediately after sending it:
the daemon interprets control EOF as an abandoned client.
Stdin EOF is communicated through the passed input FD, not control EOF.

Typical replies are:

```json
{"status": 0, "timed_out": false, "end": true}
```

```json
{"error": "mssh: ..."}
```

`status` may be `null` when an interactive program has no command exit code
or a wait expires. An SSH exec channel without a usable exit status can
return `-1`. `end` identifies command completion; it is not used by every op.

A reconnect notice has the form:

```json
{"notice": "mssh: session s reconnected ... This is a new shell ..."}
```

The command client writes notices to local stderr and continues waiting.
Queries, cancellation and forwarding configuration do not receive notices.
The current SFTP setup reader also expects its setup
acknowledgement directly.

### File Descriptor Passing

For command and SFTP requests the client uses `sendmsg()` with `SCM_RIGHTS`.
The ancillary data contains native integer FDs, not JSON field values.
The received descriptors refer to the same kernel resources, but their
numeric values in the daemon need not equal the client's values.

| Operation | Descriptor order |
| --- | --- |
| `send` | `fds[0]`: merged output pipe write end; optional `fds[1]`: stdin |
| `exec` | `fds[0]`: stdout pipe write end; `fds[1]`: stderr pipe write end; optional `fds[2]`: stdin |
| `sftp` | `fds[0]`: daemon end of a duplex socketpair |
| Other operations | No descriptors |

FDs accompany the first bytes of the request. If `sendmsg()` sends only
part of a long JSON line, the client sends the remaining bytes normally,
without resending the descriptors.

The daemon accepts at most three FDs. Unexpected received FDs are closed;
operations that do not use descriptors discard them. Dispatch takes
ownership of the FDs it needs, and the request worker's `finally` closes
anything not taken. Missing required FDs produce an error reply.

After transfer, the client closes its duplicate output write ends or
daemon-side socketpair end. Keeping those duplicates open would prevent
the client from detecting EOF after the daemon closes its end.

## Primary Channel Requests

### Persistent Shell: `send`

```bash
mssh s 'cd /var/log; echo "$PWD"'
```

```json
{"op": "send", "line": "cd /var/log; echo \"$PWD\"", "wait": 30.0}
```

```text
Client                      Daemon                        Target
  | send JSON + output FD      |                             |
  |-------------------------->| resolve link, acquire lock  |
  |                           | resync any pending request  |
  |                           | create default job          |
  |                           | write command frame ------>|
  |<---- output pipe bytes ---|<--------- PTY output --------|
  |                           |<----- marker + status -------|
  |                           | remove default job          |
  |                           | drain and close output FD   |
  |<----- terminal JSON ------|                             |
  | wait for reply and EOF    |                             |
```

The primary shell is a login Bash, falling back to a POSIX `sh`.
Its PTY starts with `TERM=dumb`, 200 columns and 50 rows.
Echo, canonical buffering and newline expansion are disabled; signal
processing remains enabled with Ctrl-C as the interrupt character.
Setup clears shell prompts and attempts `unset TMOUT` separately so a
readonly `TMOUT` failure does not prevent the other initialization.

The user command is base64-encoded and placed inside an `eval` frame.
The target decodes it and executes it **in the existing shell**, preserving
working directory, variables, functions and other shell state.
Without forwarded stdin, the frame reads from `/dev/null`.

A locally generated `__MSSH_...__` marker followed by the command's exit
status ends the frame. This marker belongs to the daemon-to-target shell
protocol; it is not a client/server JSON message and is removed from output.
The marker is written as `printf` escapes so an echoed input line cannot
look like a completed frame.

The shell framer retains enough trailing output to remove the frame's own
newline and handle output that has no final newline. `--idle` can release
quiet partial output; it does not complete a shell command without a marker.
PTY stdout and stderr are merged and forwarded to the client's stdout.
Use `--exec` without a PTY for separate streams and unmodified binary output.

### Forwarding Stdin

```bash
echo hello | mssh s cat
```

```json
{"op": "send", "line": "cat", "wait": 30.0, "stdin": true}
```

The request passes both the output FD and the producer's stdin FD.
Pipes and regular files are forwarded automatically; a terminal requires
`--stdin` for ordinary primary-shell commands. `-n` suppresses forwarding,
and `/dev/null` or missing stdin is not forwarded.

```text
Client stdin FD --> daemon --> separate SSH writer channel
                                    |
                                    v
                              remote temporary FIFO
                                    |
                                    v
                            primary shell command stdin
```

Before framing the command, the daemon opens a writer channel that creates
a unique `/tmp/.mssh-in-<16 alphanumeric characters>` FIFO with mode `0600`.
The writer prints a readiness byte before waiting for the shell's reader.
After both ends open the FIFO, the writer removes its pathname while
keeping the open inode. This avoids accumulating files once the handshake
has completed; setup failure before that point can leave a pathname.

Each request uses a new FIFO so a previous timed-out reader cannot consume
the next command's input. Input bytes never pass through the primary PTY,
where control bytes could otherwise trigger terminal signals.
Producer EOF becomes SSH write EOF, allowing the remote reader to finish.
Request cleanup closes the writer even if the command ignored its input.

### Interactive Program: `send`

For a session created with `-c`, `send.line` is written directly to the
program on the primary PTY, followed by a newline. There is no shell eval
frame and no per-command exit status.

`--prompt` matches a prompt at the end of an output line or partial line.
Without it, `--idle` uses a quiet-output heuristic to complete a reply.
Terminal escape sequences are stripped for this interactive framing.

These requests still create a default job and take the primary lock.
Forwarded stdin is rejected because the interactive program already owns
the primary PTY input. Independent exec and SFTP requests remain possible.

## Independent Commands and Logins

### `exec`

```bash
mssh s --exec 'cat /etc/app.conf' 2>errors.log
```

```json
{"op": "exec", "line": "cat /etc/app.conf", "wait": 30.0, "job_token": "<random token>"}
```

The daemon opens a new SSH session channel, registers an `exec` job and
runs the command with `chan.exec_command()`. Optional stdin goes directly
to that channel. Output travels through two passed pipe write ends.
No FIFO or shell completion marker is needed: the channel provides the
exit status.

The client waits for both output-pipe EOFs and the terminal JSON reply.
On the normal completion path, the daemon drains buffered output and
closes the pipes before replying. It also drains received SSH output
before accepting the channel's exit status, avoiding lost trailing bytes.
Early failure paths can send an error before pipe cleanup; the client
still watches the pipes through EOF.

Exec requests do not acquire the primary shell lock and can run
concurrently. They share authentication and transport, not the primary
shell's current directory, variables, functions or background jobs.

### PTY and Login

An exec request may additionally contain:

```json
{"pty": {"term": "xterm-256color", "cols": 120, "rows": 40}}
```

For `--exec -t`, the CLI requests a PTY only when both its input and output
are terminals. The local terminal stays raw while the remote program runs.
With a remote PTY, stderr is merged into stdout by the remote terminal;
the protocol still passes two output FDs.

`mssh s` is a separate login, not an attachment to the persistent shell:

```json
{"op": "exec", "line": "", "shell": true, "wait": 0.0, "stdin": true, "job_token": "<random token>"}
```

The daemon calls `chan.invoke_shell()` and registers a `login` job.
An explicit login rcfile is included as a base64 `rcfile` field.
Without that field, login initialization reuses the session's retained
remote rcfile path, if present.

While a PTY job runs, window changes are sent on its existing connection:

```json
{"op": "resize", "cols": 140, "rows": 45}
```

Resize messages have no reply or job ID: the connection identifies the
channel. Bytes arriving immediately after the initial request are preserved
and processed by the same control parser. Invalid resize messages are
dropped rather than failing the command.

## File Transfers

```bash
mssh s --put ./app.conf /tmp/app.conf
mssh s --get /var/log/app.log ./app.log
```

```json
{"op": "sftp", "job_token": "<random token>", "transfer": {"kind": "get", "source": "/var/log/app.log", "destination": "./app.log"}}
```

```text
Client                      Daemon                         Target
  | create duplex socketpair  |                              |
  | JSON + daemon endpoint -->| open channel, register job    |
  |                           | invoke SFTP subsystem ------>|
  |<---- {"ok": true} ---------|                              |
  |                                                          |
  | SFTPClient <== socketpair ==> binary relay <== SSH ==> SFTP|
  |                                                          |
  | close data/control sockets| close channel, remove job     |
```

`open_session_sftp()` returns a normal Paramiko `SFTPClient` backed by
`_SessionSFTPChannel`, which adapts the local duplex socket.
The client runs the existing copy logic, directory traversal, progress,
permissions and timestamp handling. The daemon relays binary SFTP traffic;
it does not execute the file-copy operation from the JSON metadata.

`transfer` labels the job as `put` or `get`; without a recognized kind,
the job type is `sftp`. Source and destination become the job description.
These fields do not control SFTP path access or limit what the client can do.

After `{"ok": true}`, the control connection stays open solely for request
lifetime and possible error reporting. Successful completion has no
terminal `{"status": ...}` reply. Closing either the relay or its control
connection leads to channel and job cleanup.

SFTP paths are interpreted by the subsystem, not the persistent shell.
Transfers do not inherit a prior shell `cd`, and shell variables or `~`
are not expanded. Transfers can run alongside other jobs.

Client-side Ctrl-C uses the copy abort handler and closes the local
SFTP adapter's data and control sockets, not a default-job interrupt request.
The daemon detects the disconnect and closes only that transfer's channel.
An interrupted download removes its partial local file.
Transfers are not automatically retried or resumed after connection loss.

## Jobs, Status and Cancellation

### Job Registry

Jobs are daemon-managed requests or listeners, not remote OS processes.
The idle primary shell/program is excluded. Shell background processes
are not individually registered.

| Kind | Created for | Completion/cancellation resource |
| --- | --- | --- |
| `shell`, `interactive` | Current request on the primary channel | Default job; keep primary channel |
| `exec`, `login` | Independent command or login | Its own SSH channel |
| `sftp`, `put`, `get` | SFTP relay | Its channel and relay endpoints |
| `forward` | One `-L`, `-R` or `-D` listener | Listener plus all owned connections |

IDs increase within a daemon lifetime and restart when the daemon is
recreated. Completed jobs disappear; there is no job history.
`job_token` is stored explicitly on independent exec/login/SFTP jobs,
allowing cancellation before a client has queried the numeric ID.
It is not a substitute for authentication or a session-wide identifier.

### `jobs` and `status`

```json
{"op": "jobs"}
```

```json
{"jobs": [{"id": 2, "kind": "shell", "command": "sleep 20", "channel": 0, "started": 1792324800.0, "default": true, "forward_type": null, "state": "pending"}]}
```

Snapshots are sorted by job ID. Job states are `running`, `pending`
or `stopping`. `forward_type` is `local` for `-L`, `remote` for `-R`,
`socks` for `-D`, and `null` otherwise; all listeners retain `kind: "forward"`.
Listeners have a `null` channel because each accepted connection opens
its own channel.
Only forward rows include `connections`, the current accepted connection
count (including handshakes and pending opens/connects). Non-forward rows
omit that field. Lightweight `probe` and `list` status replies do not collect
connection metadata or counts.

```json
{"op": "status"}
```

Without a `view` field, status returns the following groups in one reply
(`view: "full"` explicitly requests the same reply):

| Fields | Meaning |
| --- | --- |
| `target`, `mode`, `command`, `pid`, `rcfile` | Session identity and configuration |
| `started`, `served` | Startup time and command/transfer counter, not completed-job history |
| `running` | Whether the primary shell lock is held; not a total-job indicator |
| `jobs`, `job_details` | Numeric count and rows from one job snapshot |
| `last_activity`, `idle_limit` | Recorded activity time and configured inactivity expiry |
| `dead`, `stopping` | Lifecycle flags/reason |
| `link`, `broken`, `attempt`, `retries`, `reconnects` | Stored connection and reconnect state |
| `refusal`, `last_error` | Why reconnect is unavailable or why the last attempt failed |

For discovery and listing, use lightweight views of the same operation:

```json
{"op": "status", "view": "probe"}
```

```json
{"ok": true}
```

```json
{"op": "status", "view": "list"}
```

```json
{"target": "root@host:22", "mode": "shell", "started": 1792324800.0, "served": 4, "jobs": 2}
```

Probe does not inspect jobs or connection state. List counts jobs using
the same live-job selection as `--jobs`, but does not build/sort job
metadata or inspect link/reconnect state. It returns only the five fields
shown above. Unknown views receive an error reply.
Older daemons ignore `view` and return their full status; clients accept
those replies for discovery and listing without an additional request.

`run_session_status()` renders a summary, not the detailed job table.
It derives running/pending/interrupting/forwarding state from the snapshot,
then gives lifecycle and connection states precedence.
The default-job state is shown independently. Type counts are sorted
alphabetically by name; `-D` counts as `socks`, `-L` as `forward`,
and `-R` as `reverse`.
Use `--jobs` for commands, channel IDs and forwarding addresses.

These queries do not acquire the primary lock, refresh activity, consume a
pending result or start reconnect. Link metadata is the daemon's last
observed state, not a fresh network test on every query.

### Interrupt and Stop

```json
{"op": "interrupt"}
```

```json
{"op": "interrupt", "job": 2}
```

```json
{"op": "interrupt", "job_token": "<random token>"}
```

With no selector, interrupt reaches only the current default job;
idle returns `interrupted: false`. A numeric job selector takes precedence
over a token. Numeric IDs must be nonnegative integers; unknown or finished
jobs return an error.

A successful reply is:

```json
{"interrupted": true, "status": null, "timed_out": false}
```

For a default job, cancellation sets its abort flag; the owning pump sends
Ctrl-C and resynchronizes the primary channel. A pending job can be
resynchronized by the interrupt worker when the primary lock is free.
When recovery times out on a live channel, the worker returns an error rather
than an interruption-completed reply. The job remains pending and its abort
flag is reset; independent channels and listeners are not closed. A running
job's interrupt reply acknowledges the cancellation request; any subsequent
recovery failure is returned to the client streaming that job.
For exec/login/SFTP jobs, cancellation closes that job's channel.
For forwards it closes the listener, pending opens/handshakes and all
accepted connections. Targeted cancellation works while reconnecting.

`--stop --job ID` uses the targeted `interrupt` operation above.
There is no separate per-job `stop` op.

### Default Shell Reset: `reset_shell`

```json
{"op": "reset_shell"}
```

```json
{"ok": true, "reset": true}
```

This descriptor-free operation replaces the primary shell on the existing
transport. It is explicit recovery for programs that disable terminal signals
or ignore SIGINT, not a stronger interpretation of `interrupt`.
It rejects `-c` sessions. A distinct operation makes old daemons reject it
instead of acknowledging an unknown selector.

`reset_lock` excludes simultaneous resets. A candidate PTY/login shell is
opened and initialized before the existing reader is disturbed; retained
rcfile paths are sourced without another upload. Initialization errors close
only the candidate and return an error. Then `resetting` asks `_write()`,
`_pump()` and `_await_token()` to raise `_SessionReset`, releasing primary
ownership without waiting for remote signals or synchronization markers.
The reset acquires the primary lock with a five-second deadline and checks
that shutdown/reconnect has not replaced the transport.

With that lock held it swaps the channel/marker, clears the default job and
safely closes the old channel. A reset is not `_SessionDead`, so an active
send/interrupt returns an error without ending the daemon or reconnecting.
The next primary send receives a notice about lost shell state before its
output. Exec/login/SFTP channels, forwards and existing forwarded connections
are not changed. Job numbering is not reset. Closing the old PTY cannot
guarantee termination of every remote descendant.

### Forward Connections

The descriptor-free connection query selects one forwarding job:

```json
{"op": "connections", "job": 3}
```

```json
{"job": 3, "connections": [{"id": 1, "channel": 7, "started": 1792324800.0, "state": "active", "origin": "127.0.0.1:45678", "listener": "127.0.0.1:8080", "destination": "localhost:80"}]}
```

Rows are sorted by connection ID. IDs increase within each job without reuse,
including reconnect, and closed connections disappear. States are `handshake`
for pending SOCKS negotiation, `connecting` for destination/channel setup,
and `active` for a relay. Channel IDs are local to the daemon's SSH transport;
`channel` is `null` before a local/SOCKS channel opens. Unnamed Unix peers use
`origin: "-"`; pending SOCKS requests have `destination: "-"`. For `-R`, the
origin/listener are on the target and the destination is on the daemon host.

To close only one stream:

```json
{"op": "interrupt_connection", "job": 3, "connection": 1}
```

```json
{"interrupted": true}
```

Both IDs must be positive integers (booleans are rejected). Unknown/finished
connections and non-forward jobs return an error. The daemon closes the
selected socket and channel, leaving its listener and siblings intact.
Cancellation covers pending handshakes/channel opens/local connects too:
workers cannot publish a socket/channel after the record is cancelled,
and late channel-open success closes that unowned channel.

`--job ID --connection ID --stop` and `--interrupt` use the distinct
`interrupt_connection` op, never `interrupt` or `stop`. An old daemon must
reject an unknown op rather than ignore a new selector and stop the whole
job/session. Both operations are answered before SSH link resolution, work
while reconnecting, and do not refresh activity or resynchronize default jobs.

### Whole-Session Stop

Session-wide stop is:

```json
{"op": "stop"}
```

The daemon sets `stopping`, attempts rcfile cleanup and replies
`{"ok": true}` before unwinding the serve loop. Teardown closes the local
listener, unlinks the control socket, closes the primary and job channels,
stops forwards and closes the SSH chain.
The CLI waits up to 15 seconds for the socket pathname to disappear.
The acknowledgement, and even pathname removal, is not a guarantee that
every remote process has already exited or every cleanup step succeeded.

## Persistent Forwarding

### Configuration Requests

```json
{"op": "forward", "specs": ["127.0.0.1:8080:service.internal:80", "/tmp/client.sock:/run/service.sock"]}
```

```json
{"op": "dynamic_forward", "dynamic_specs": ["127.0.0.1:1080"], "specs": ["127.0.0.1:8080:service.internal:80"]}
```

```json
{"op": "remote_forward", "remote_specs": ["8080:127.0.0.1:8000", "/tmp/target.sock:/tmp/local.sock"], "specs": ["8081:service.internal:80"], "dynamic_specs": ["1080"]}
```

```json
{"ok": true, "forwards": [{"id": 3, "command": "127.0.0.1:1080 -> SOCKS4/5"}]}
```

The configuring CLI may exit immediately; the daemon owns the listeners.
At creation, forwarding settings arrive through inherited startup options
and the readiness reply, not a second control configuration request.
Later additions use the operations above.

The daemon validates and binds all listeners in a batch before completing
registration. Bind/start failure rolls back the additions, not existing
forwards. Configuration success confirms listener binding (and the target's
acknowledgement for `-R`), not destination reachability. No descriptors are
passed by the configuring CLI.

`-L` supports TCP/Unix listeners and TCP/Unix destinations.
The configuring client makes relative local socket paths absolute before
sending them. Remote paths are literal. Existing local paths are never
overwritten; cleanup removes only the Unix socket inode created by that job.

`-R` uses the same four forms but listens on the target and connects the
destination from the daemon's machine:

```text
-R [bind_address:]port:host:hostport
-R [bind_address:]port:local_socket
-R remote_socket:host:hostport
-R remote_socket:local_socket
```

The client normalizes relative
destination socket paths, not target listener paths. `remote_specs` is
required and nonempty; `specs` and `dynamic_specs` are optional batch members.
Adding remote listeners requires a live transport; targeted cancellation
remains possible during reconnect. sshd owns remote socket permissions,
stale-path replacement and cleanup; mssh does not unlink those paths.

### Forwarded Data

```text
Application --> local TCP/Unix listener owned by daemon
                         |
                         +--> one accepted socket
                                  |
                                  +--> one target SSH forwarding channel
                                             |
                                             +--> destination TCP/Unix service
```

TCP destinations use `direct-tcpip`; remote Unix destinations use
`direct-streamlocal@openssh.com`. Neither application data nor channel
contents pass through the CLI control socket.

Remote TCP listeners use SSH global requests `tcpip-forward` and
`cancel-tcpip-forward` with address and port. Unix listeners use
`streamlocal-forward@openssh.com` and `cancel-streamlocal-forward@openssh.com`
with a socket path. Binds request an acknowledgement; cancellation does not.
Incoming `forwarded-tcpip` channels include listener and origin addresses;
`forwarded-streamlocal@openssh.com` includes listener path and a reserved
string. `_RemoteForwardManager` installs handlers on this transport alone
and routes channels to jobs by listener endpoint. It avoids Paramiko's
single remote-TCP callback and adds its missing incoming Unix support.

Global replies have no request IDs. The manager serializes bind requests;
a timed-out request stays pending until its reply or transport replacement.
A late successful bind is cancelled, preventing orphaned target listeners
and preventing its reply from completing a subsequent bind. Unrelated
incoming channel types and unowned global replies retain Paramiko's handlers.
mssh's other global requests (keepalives) do not request replies.

Each remote incoming channel starts a local connect worker, then uses the
same bounded, half-close-preserving relay as `-L`. The transport dispatch
thread never performs local DNS/connect or a rekey-waiting channel close.
Remote listener access is subject to sshd's forwarding and GatewayPorts
policies; destination names are resolved on the daemon's machine.

`-D` first parses a SOCKS4/4a/5 TCP CONNECT handshake on the accepted socket.
SOCKS4a/5 domain names are sent to the target for resolution, supporting
hostname-proxy clients such as `socks5h`. Success is returned only after
the SSH channel opens. Authentication, BIND and UDP ASSOCIATE are unsupported.

The common relay buffers both directions and preserves independent
half-closes: one side's input EOF does not discard a remaining response.
An individual destination failure closes that connection, not the listener.
Default binding is loopback; wildcard listeners expose session access
without SOCKS authentication.

## Concurrency and Backpressure

The accept loop creates a worker per control connection. A long command
therefore does not block status, job inspection or independent channels.
The primary lock serializes requests sharing one PTY: interleaved input
and output would make command framing ambiguous.

| Lock/state | Protects |
| --- | --- |
| `_Session.lock` | Primary command execution, pending resynchronization and replacement-shell setup |
| `_Session.reset_lock` | Explicit shell reset and idle channel polling; acquired without waiting by poll/other resets |
| `execs_lock` | Independent job registry, default-job identity and job IDs |
| `state_lock` | Stored link/reconnect state and one-time reconnect notice |
| `forward_lock` | Atomic forward additions and remote rebind during reconnect |
| Forward job lock | Connection registry, monotonic connection IDs and listener lifetime |
| Forward connection lock | Socket/channel publication, metadata and cancellation ownership |

Job snapshots copy the registry/metadata briefly, then count forwarding
connections outside `execs_lock` to avoid lock inversion with teardown.
They do not wait for a command to release the primary lock. Shutdown marks
the session stopping before taking the job snapshot, preventing late registration
from leaving an unclosed job. Independent jobs and forwarding resources
are closed outside the registry lock so cleanup workers can remove them.

Daemon-side command output FDs are nonblocking. `_FdSink` stores bytes
that a full local pipe cannot accept, and the pump pauses remote reads
when the backlog reaches `SESSION_BACKLOG` (64 KiB).
The same threshold pauses stdin reads when the SSH send window is full.
It is a scheduling threshold, not a strict limit on all buffered bytes:
data already drained from SSH and internal library buffers also exist.

Forwarding uses separate buffers in both directions and bounds reads
by the remaining buffer capacity. In both cases, pauses propagate
backpressure instead of allowing an indefinitely growing daemon buffer.
`SESSION_TICK` (50 ms) lets pumps observe cancellation and deadlines even
when no watched FD wakes them.

After command completion, output draining waits up to 30 seconds by
default for a slow client. A client that never resumes reading can
therefore lose remaining buffered output; cleanup does not wait forever.

## Timeouts, Disconnects and Reconnect

### Request Lifetimes

| Event | Primary `send` | Independent `exec` / login | SFTP / forwards |
| --- | --- | --- | --- |
| Wait expires | Return timeout; retain pending default job | Close own channel and remove job | SFTP has no total `--wait`; forwards persist |
| Client disconnects | Stop delivery; retain pending default job on a live channel | Close own channel and remove job | SFTP closes relay; configuring CLI disconnect does not remove forwards |
| Targeted cancellation | Ctrl-C and resync; keep primary channel | Close own channel | Close selected relay/listener resources |
| Shared transport drops | Clear old default job | Existing channels cannot survive | Transfers fail; listeners survive only if daemon remains for reconnect |

Ordinary commands default to `--wait 30`; `--wait 0` means no execution
deadline. Login and `--exec -t` default to no deadline unless explicitly
overridden. The primary wait deadline starts after acquiring its lock and
resynchronizing previous work, so it does not bound time queued behind
another command. Initial control-request reads use a 30-second socket timeout.

`--timeout` bounds connection/channel setup and SFTP socket inactivity,
not total transfer duration. SOCKS handshakes have a total timeout, with
a minimum of one second. `--session-idle` is a separate daemon expiry.
Status queries do not refresh it. Activity is updated at command/transfer
cleanup and forwarding configuration/traffic; it is not a continuous
heartbeat for every active primary or exec request.

For non-PTY command clients, the first local SIGINT sends a separate
interrupt request and keeps draining output; a second lets the client leave.
For exec/login it includes that request's token. In raw-terminal mode,
keyboard Ctrl-C reaches the remote PTY as input instead of triggering the
local Python signal handler. File-copy SIGINT uses its separate abort path.

The CLI maps an error reply to exit `1`, a wait timeout to `124`, a handled
interrupt/detach to `130`, and otherwise uses the reported command status.
Interactive replies with no status normally exit `0`.

### Pending Default Requests

A timeout or abandoned primary-shell client does not close the primary
channel. The default job remains `pending` after its lock is released,
even if the remote command later finishes without a worker consuming
its marker. `--status` and `--jobs` do not consume that output.

Before a later primary request starts, `_resync()` interrupts leftover
work and drains output to a one-shot token marker. That token distinguishes
the synchronization point from late completion markers.
Interactive mode instead sends Ctrl-C and drains until output is quiet.
If shell resynchronization cannot complete, new work is not framed onto
the unresolved request. Shell recovery makes two attempts with a five-second
token wait each, below the Ctrl-C client's 15-second control-request timeout.
`_SessionBusy` distinguishes this live-but-unresponsive shell from
`_SessionDead`: return an error and retain the old default job, without
ending the daemon or starting reconnect. Actual read/EOF/channel failures
still propagate as `_SessionDead`, and shutdown interrupts token waits.
Ctrl-C alone cannot guarantee recovery: programs may disable `ISIG` or handle
SIGINT without exiting. `reset_shell` bypasses that dependency by replacing
only the shell channel, explicitly discarding its state.

The default shell uses `TERM=dumb` and sets `PAGER`, `MANPAGER`, `GIT_PAGER`
and `SYSTEMD_PAGER` to `cat` during setup, after profiles and before rcfile
initialization. Some pagers reopen `/dev/tty` despite `/dev/null` stdin;
waiting for terminal input can swallow recovery commands as keystrokes.
Plain-text defaults prevent that for cooperating tools. Explicit pagers,
rcfile overrides or read-only settings may still require `--exec -t` or a
fresh login; independent channels do not inherit these shell defaults.

Independent channels are closed on timeout/disconnect. Closing a channel
is not proof that every remote descendant has terminated, so applications
must not rely on disconnect as a universal process-kill mechanism.

### Rebuilding the Shared Connection

Without `--reconnect`, a detected shared connection loss ends the daemon.
With it, the daemon clears the old default job and runs background attempts
with delays of 2, 4, 8, 16 and then at most 30 seconds between attempts.
Attempt duration is additional to those delays.

```text
link up
   |
   +--> failure observed --> clear old default job --> link reconnecting
                                                        |
                         new commands get an error <----+
                         status/jobs/stop still work    |
                                                        +--> rebuild succeeds
                                                        |      |
                                                        |      +--> new shell
                                                        |      +--> new rcfile
                                                        |      +--> notice pending
                                                        |
                                                        +--> attempts exhausted
                                                               |
                                                               +--> link down
                                                               +--> next work request:
                                                                    one final attempt
```

Stateful `-c` sessions and chains authenticated by an interactively typed
password refuse automatic reconnect. The daemon has no terminal for a
new password prompt. Status reports the refusal; a later work request
receives the reason and can end the broken session.

A rebuild uses the same setup path as startup. It restores connectivity,
not the previous working directory, environment, processes or program
state. The first subsequent send/exec request receives a notice before
its command runs. Work is not automatically replayed.

Remote listeners are rebound before a rebuild reports success; any failed
bind fails that attempt and cancels the listeners newly restored in it.
Their job IDs remain. Unix path rebinding can require sshd's
`StreamLocalBindUnlink` policy when a previous listener path remains.
`-L/-D` listeners stay bound on the local daemon during transport replacement.

Forward listener jobs and IDs remain, but individual accepted connections
do not survive transport replacement. New connections while disconnected
fail rather than queue; later ones use the rebuilt transport.
Forward configuration and targeted cancellation do not trigger link
resolution. `-L/-D` additions and targeted cancellation remain available
during reconnect; `-R` additions need a live transport.

## Rcfile and Teardown Safety

Session creation reads the local rcfile before forking. The daemon retains
its bytes, uploads to `/tmp/.mssh-rc-<16 alphanumeric characters>`, and
sources it in the initialized shell. Upload creates the private file
exclusively so a collision is not overwritten.

The file persists for that connection; later logins source the same path
without uploading again. A successful reconnect uploads the retained
bytes to a new path and sources them in its new shell.
An explicit login rcfile overrides this for one login only and is removed
inline after sourcing. The rcfile completion token is separate from normal
shell-command framing; source output and echoed initialization lines are
filtered.

Daemon stop/exit attempts to remove all remembered remote rcfile paths.
Cleanup is best-effort: a broken transport, forced daemon kill or target
replacement can leave files behind. A reconnect reuses the bytes loaded
at creation, not a fresh read of a subsequently edited local file.

Session-channel cleanup uses `_close_session_channel()` rather than blindly
calling `chan.close()` on polling channels. In affected Paramiko versions,
`PosixPipe.close()` closes its FDs before setting `_closed`; a concurrent
receive notification can then write to a closed FD and terminate the shared
transport. The helper replaces stdout/stderr buffer notifications under
their locks with ordinary events before closing the channel.
Workers must not reinstall polling notifications after cancellation.
SIGPIPE is also ignored in the daemon, so a disconnected local reader
does not terminate the process via that signal.

This workaround is daemon-local. It does not fix the client's separate
Paramiko SFTP prefetch-thread cancellation race; some versions can still
print a background traceback after transfer interruption.

## Compatibility, Security and Diagnostics

There is no explicit protocol-version or capability handshake.
Optional fields extend existing replies. Older status daemons may expose
only the primary lock and a numeric count; clients keep that fallback and
show unknown counts when absent, rather than treating missing details as
zero jobs. Missing output FDs and unsupported operations produce errors
that may require stopping and recreating the daemon.

`dynamic_forward` and `remote_forward` are intentionally distinct ops.
Sending a `-D` or `-R` field
inside an old `forward` request could otherwise be silently ignored.
Job-targeted stop remains compatible by using the existing `interrupt` op.

Control socket access grants the session owner's remote privileges,
including arbitrary commands and SFTP operations. Filesystem permissions
and the peer-UID check form the local boundary; there is no per-operation
credential exchange. Do not expose or share the session directory.
The protocol is not hardened as a public, untrusted network service.

Set daemon logging at creation: no verbosity disables it, `-v` enables
WARNING, `-vv` INFO and `-vvv` DEBUG. Startup stderr goes to the terminal;
after detachment it appends to `NAME.log`. Later CLI verbosity does not
change the daemon's level. Request IDs are thread-local diagnostic context,
not protocol IDs or job selectors.

See [Daemon diagnostics](README.md#daemon-diagnostics) for tracebacks and
log fields. Logs omit structured command/input/rcfile payloads, but
exception messages and paths can contain sensitive data. `--stop` does
not remove or rotate logs.

## Code and Test Map

| Area | Main implementation |
| --- | --- |
| CLI dispatch and discovery | `run_session()`, `session_alive()`, `run_session_list()` |
| Spawn and readiness | `run_session_spawn()`, `serve_session()`, `_await_session()` |
| Local socket and peer checks | `session_dir()`, `_session_listen()`, `_peer_uid()` |
| Control requests and replies | `session_request()`, `_reply()`, `_notify()`, `_session_serve()` |
| Command FD passing and output | `_session_pipes()`, `session_send()`, `session_exec()`, `_FdSink` |
| Primary shell/program | `_Session.run()`, `session_frame()`, `_pump()`, `_resync()` |
| Independent channels | `_Session.exec_run()`, `_pump_exec()`, `_exec_control()` |
| SFTP adapter and relay | `open_session_sftp()`, `_SessionSFTPChannel`, `run_session_copy()` |
| Jobs and cancellation | `_ExecJob`, `jobs_snapshot()`, `interrupt_job()`, `close_channels()` |
| Forward connections | `_ForwardConnection`, `connections_snapshot()`, `interrupt_connection()` |
| Forwarding | `_LocalForwardJob`, `_RemoteForwardJob`, `_RemoteForwardManager`, `_SocksRequest`, `add_forwards()` |
| Reconnect and initialization | `failed()`, `resolve()`, `rebuild()`, `setup_rcfile()` |
| Safe channel close and logging | `_close_session_channel()`, `_session_log()`, `_setup_logging()` |

The source's `wire protocol` comment is the inline reference for message
layouts. Tests exercise these interactions with local fake SSH channels
and, for forwarding, real SSH fixtures:

- `tests/test_session_daemon.py`: lifecycle, commands, streams and stdin.
- `tests/test_session_unit.py`: framing and resynchronization.
- `tests/test_session_pager.py`: pager defaults, busy recovery and live
  session preservation when a pager ignores Ctrl-C.
- `tests/test_session_reset.py`: shell reset during execution/recovery, real
  Vim, rcfile reuse, state-loss notices and surviving independent jobs/forwards.
- `tests/test_session_jobs.py` and `tests/test_session_status.py`: snapshots,
  targeted cancellation, status summaries and legacy replies.
- `tests/test_session_copy.py`: SFTP relay and copy lifecycle.
- `tests/test_session_forward.py`, `tests/test_session_unix_forward.py`,
  `tests/test_session_remote_forward.py` and
  `tests/test_session_socks.py`: listener configuration and forwarded data.
- `tests/test_session_reconnect.py`: disconnect detection and rebuild.
- `tests/test_session_channel_close.py` and `tests/test_session_logging.py`:
  channel-close races and diagnostics.
