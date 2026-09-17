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
- **`--wait SEC`** (default 30) bounds how long one command is watched; `0`
  means indefinitely. A timeout exits 124 and leaves the session usable — the
  next command stops whatever was still running.
- **Ctrl-C interrupts the remote command**, not your local process, so the
  session survives it. Press it twice to walk away and leave the command
  running.
- **A program that paginates will stall at its pager** until `--interrupt`, so
  turn paging off in the program itself (`gdb -ex 'set pagination off'`).
- **`--session-idle SEC`** ends a session after that much quiet, so a forgotten
  one does not hold a root channel open forever. Default `0`: never.
- The first call keeps your terminal until the chain is up, so a hop that needs
  a password can still ask for one and `-v` still shows each hop. Only then
  does it detach.

Sessions live in `~/.mssh/sessions`, one socket each, mode `0600` inside a
`0700` directory that is re-checked on every use, and the daemon refuses any
peer whose uid is not yours. **Holding one of those sockets is equivalent to
being logged in on the target**, so it is guarded like a private key rather
than like a temp file.

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
| `-t, --force-tty` | Allocate a pty even with `-c` |
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
| `-v, --verbose` | Log each hop to stderr |
| `-V, --version` | Print the version |

Session-only options: `--session NAME`, `--sessions`, `--status`, `--stop`,
`--interrupt`, `--prompt REGEX`, `--stdin`, `--idle SEC`, `--wait SEC`,
`--session-idle SEC`.

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
that dies that way reports it on the next call.

## Legacy servers

Against OpenSSH older than 7.8, RSA keys do not work: those servers accept only
SHA-1 `ssh-rsa` signatures, which paramiko 5 no longer produces. Use an ed25519
key (`ssh-keygen -t ed25519`). This is why ed25519 leads the default key search
order.

## Tests

```bash
python3 tests/test_session_unit.py     # framing, endpoints, flags     (56)
python3 tests/test_session_daemon.py   # daemon, protocol, client      (53)
python3 tests/test_copy_mode.py        # copy permissions vs real scp (51)
```

No network and no sshd: the framing layer is driven against local `bash`,
`gdb` and `python3` behind a real pty, the daemon tests run mssh as separate
processes with only the SSH hops faked, and the copy tests talk to OpenSSH's
own `sftp-server` over a pipe and compare every resulting mode against what
`scp` itself produces for the same input. Some of the permission tests drop to
an unprivileged user, since root ignores permission bits and would pass either
way.
