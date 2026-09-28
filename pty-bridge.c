/*
 * pty-bridge -- attach the local terminal to a pty, local or remote
 *
 * The terminal a user wants to work on is not always a local
 * /dev/pts/N. Very often it is a REMOTE pty: the shell of an ssh
 * session, the console of a virtual machine, a serial console behind
 * a gateway. Such a pty cannot be opened directly -- it is only
 * reachable through a channel command that connects to it, such as
 * "ssh", "virsh console" or "telnet".
 *
 * pty-bridge bridges the local terminal and that pty:
 *
 *   keyboard (stdin) --> pty      (local terminal in raw mode)
 *   screen  (stdout) <-- pty      (pty in raw mode too)
 *
 * so the user operates the remote pty as if it were local. The pty
 * is either a local one (--pty /dev/pts/N) or a remote one reached
 * through a channel command (see usage below).
 *
 * Beyond plain forwarding it also automates the procedure that comes
 * with a remote pty: -p patterns auto-type replies to login prompts,
 * and the window size of the remote side is kept in sync with the
 * local terminal.
 *
 * Requires stdin to be the controlling terminal of this process,
 * otherwise it exits immediately.
 *
 * Exit by pressing the escape character (default ^] = Ctrl-]),
 * or when the pty peer closes.
 *
 * Build: gcc -Wall -O2 -o pty-bridge pty-bridge.c -lutil
 * Usage: ./pty-bridge [-e CHAR|--escape CHAR] [-p "login: root"] \
 *                     [--pty /dev/pts/3] [command [arg ...]]
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <poll.h>
#include <pty.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static const char *prog = "pty-bridge";

static int tty_fd = -1;          /* local terminal (stdin) */
static struct termios saved_tio; /* original terminal settings */
static int tio_saved = 0;

/*
 * Auto-reply pattern, from -p "<prompt> <reply>", split at the LAST
 * space: the space stays with the prompt, so "login: root" compares
 * the full prompt "login: " (colon and trailing space included)
 * against the exact tail of the pty output, and types "root" plus
 * Enter on a match. Each pattern is used at most once.
 */
#define MAX_PATTERNS 16
struct pattern {
    const char *match;    /* prompt text compared against the output tail */
    size_t mlen;          /* strlen(match), computed once at parse time */
    const char *response; /* string auto-typed after a match */
    int sentinel;         /* empty response: the command prompt marker */
    int used;
};
static struct pattern patterns[MAX_PATTERNS];
static int nr_patterns;
static int nr_unused;     /* non-sentinel patterns still waiting to fire */
static int has_sentinel;  /* a sentinel pattern is configured */
static size_t max_mlen;   /* record size: longest pattern match text */

/*
 * Sentinel pattern: a pattern with an EMPTY response (e.g. -p "]# ").
 * It recognizes the shell command prompt -- the boundary where the
 * peer sits waiting for the user to type. It stays armed: every time
 * the prompt is seen again it can act again. When it fires nothing is
 * typed (no response, no Enter), and every still-unused normal pattern
 * is marked used (login prompts are stale once a shell prompt showed
 * up). winch_pending starts set (at parse time), so the first time the
 * prompt is seen the window size is pushed; afterwards it is pushed
 * only when the local window changed since (SIGWINCH).
 */
static volatile sig_atomic_t winch_pending = 0;
static int sentinel_seen = 0; /* currently sitting at the command prompt */

/* Sliding window of recent pty output, for pattern matching */
#define OUTBUF_SIZE 4096
static unsigned char outbuf[OUTBUF_SIZE];
static size_t outlen;

/*
 * How long the COMMAND-mode startup phase waits for the channel
 * command to put the pty into raw mode before giving up. The timeout
 * counts silent time: every pty output restarts it.
 */
#define STARTUP_TIMEOUT_SEC 3

/* Restore original terminal settings. Must be called before exit. */
static void restore_tty(void)
{
    if (tio_saved)
        tcsetattr(tty_fd, TCSANOW, &saved_tio);
}

/*
 * Signal handler: restore the terminal, then exit.
 * tcsetattr is a syscall, safe to use here.
 */
static void on_signal(int sig)
{
    restore_tty();
    signal(sig, SIG_DFL);
    raise(sig);
}

/*
 * SIGWINCH handler: only record that the window changed. SIGWINCH is
 * blocked everywhere except inside ppoll, so the handler runs while
 * ppoll waits -- ppoll then returns EINTR and the main loop picks up
 * the flag with no race window.
 */
static void on_winch(int sig)
{
    (void)sig;
    winch_pending = 1;
}

/* Install handlers for the signals that must restore the terminal. */
static void setup_signals(void)
{
    struct sigaction sa;

    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    sa.sa_handler = on_winch;
    sigaction(SIGWINCH, &sa, NULL);
}

static void die(const char *msg)
{
    fprintf(stderr, "%s: %s\n", prog, msg);
    exit(2);
}

static void die_errno(const char *what)
{
    fprintf(stderr, "%s: %s: %s\n", prog, what, strerror(errno));
    exit(2);
}

static void usage(FILE *out)
{
    fprintf(out,
        "Usage: %s [OPTION]... [--pty PTY] [COMMAND [ARG]...]\n"
        "\n"
        "Attach the local terminal to a pty: keyboard input is written to\n"
        "the pty, and pty output is printed on the screen. With --pty the\n"
        "existing pty PTY (e.g. /dev/pts/3) is attached; otherwise a new\n"
        "pty is created and COMMAND runs as the channel to the remote pty\n"
        "to be attached, such as \"ssh -tt host\" or \"virsh console vm\".\n"
        "\n"
        "Options:\n"
        "  -e, --escape CHAR   exit character, default ^] (Ctrl-])\n"
        "                      CHAR is a single character or ^X form\n"
        "  -p, --pattern SPEC  auto-reply. SPEC is \"<prompt> <reply>\", split at\n"
        "                      the last space: when the pty output ends exactly\n"
        "                      with <prompt> (e.g. \"login: \" with its trailing\n"
        "                      space), type <reply> plus Enter. Repeatable;\n"
        "                      each pattern fires at most once. An empty reply\n"
        "                      (e.g. \"]# \") is a sentinel for the command\n"
        "                      prompt: nothing is typed, and all still-unused\n"
        "                      patterns are marked used\n"
        "      --pty PATH      attach to an existing pty (e.g. /dev/pts/3)\n"
        "                      instead of running COMMAND on a new pty\n"
        "  -h, --help          show this help\n"
        "\n"
        "Options must precede COMMAND; use -- to separate them.\n"
        "\n"
        "Examples:\n"
        "  %s --pty /dev/pts/3\n"
        "  %s -e ^q --pty /dev/pts/3     # exit with Ctrl-Q\n"
        "  %s -p \"login: root\" -p \"]# \" -- ssh -tt admin@10.0.0.1\n"
        "  %s -- virsh console vm1\n",
        prog, prog, prog, prog, prog);
}

/*
 * Parse the escape character:
 *   "^X"  -> control character X&0x1f, e.g. "^]" = 0x1d (Ctrl-])
 *   "^?"  -> 0x7f (DEL)
 *   a single literal character -> itself
 */
static unsigned char parse_escape(const char *s)
{
    size_t len = strlen(s);

    if (len == 2 && s[0] == '^') {
        char c = s[1];
        if (c == '?')
            return 0x7f;
        if (c >= '@' && c <= '_')
            return (unsigned char)(c & 0x1f);
        if (c >= 'a' && c <= 'z')
            return (unsigned char)((c - 'a' + 'A') & 0x1f);
    }
    if (len == 1)
        return (unsigned char)s[0];

    fprintf(stderr,
            "%s: invalid escape character '%s' (use a single char or ^X form, e.g. ^])\n",
            prog, s);
    exit(2);
}

/* Format the escape character in caret notation: 0x1d -> "^]", 0x7f -> "^?" */
static void escape_name(unsigned char c, char *out, size_t sz)
{
    if (c == 0x7f)
        snprintf(out, sz, "^?");
    else if (c < 0x20)
        snprintf(out, sz, "^%c", c + 64);
    else
        snprintf(out, sz, "%c", c);
}

/* Write exactly n bytes, exiting on error. */
static void write_all(int fd, const unsigned char *buf, size_t n)
{
    while (n > 0) {
        ssize_t w = write(fd, buf, n);
        if (w < 0) {
            if (errno == EINTR)
                continue;
            die_errno("write");
        }
        buf += w;
        n -= (size_t)w;
    }
}

/*
 * Push the local window size into the shell behind the pty. Typed in
 * the open: hiding it with "stty -echo" would leave a "stty -echo"
 * string on screen anyway, so just let the user see the resize happen.
 */
static void sync_winsize(int pty_fd)
{
    struct winsize ws;
    char cmd[128];

    if (ioctl(STDIN_FILENO, TIOCGWINSZ, &ws) < 0)
        return;

    snprintf(cmd, sizeof(cmd), "stty rows %u columns %u\r",
             (unsigned)ws.ws_row, (unsigned)ws.ws_col);
    write_all(pty_fd, (const unsigned char *)cmd, strlen(cmd));
}

/*
 * Push the local window size into the pty directly with TIOCSWINSZ.
 * Used when no sentinel is configured: there is no prompt boundary to
 * wait for, and the ioctl also makes the kernel deliver SIGWINCH to
 * the foreground process group behind the pty -- what a real terminal
 * does on a resize.
 */
static void push_winsize(int pty_fd)
{
    struct winsize ws;

    if (ioctl(STDIN_FILENO, TIOCGWINSZ, &ws) == 0)
        ioctl(pty_fd, TIOCSWINSZ, &ws);
}

/*
 * Record bytes of pty output and check the unused patterns. A pattern
 * is a prompt: once printed, the peer stops and waits for input, so a
 * prompt always ends up as the TAIL of the output stream. The full
 * prompt text -- colon and trailing space included -- is compared
 * against the exact tail; a prompt buried in earlier output never
 * fires. Each normal pattern fires at most once; the buffer is reset
 * after a reply so the echoed response cannot trigger another pattern.
 *
 * Recording continues while any pattern can still match: without -p it
 * never starts, after every normal pattern has fired only a sentinel
 * keeps it alive. The record size is fixed at parse time: only the
 * last max_mlen bytes (the longest pattern) are ever kept -- enough to
 * see any prompt as the output tail.
 */
static void record_and_check(int pty_fd, const unsigned char *buf, size_t n)
{
    if (nr_unused == 0 && !has_sentinel)
        return; /* nothing left to match: never record again */

    /*
     * New pty output means we are no longer sitting at the command
     * prompt -- unless this very output ends with the sentinel, which
     * sets the flag again below.
     */
    sentinel_seen = 0;

    /* record only the tail of the chunk, at most max_mlen bytes */
    if (n > max_mlen) {
        buf += n - max_mlen;
        n = max_mlen;
    }
    for (size_t i = 0; i < n; i++) {
        if (outlen == OUTBUF_SIZE) {
            memmove(outbuf, outbuf + 1, outlen - 1);
            outlen--;
        }
        outbuf[outlen++] = buf[i];
    }
    if (outlen > max_mlen) {
        memmove(outbuf, outbuf + outlen - max_mlen, max_mlen);
        outlen = max_mlen;
    }

    for (int j = 0; j < nr_patterns; j++) {
        struct pattern *p = &patterns[j];

        if (p->used || p->mlen == 0 || p->mlen > outlen)
            continue;
        if (memcmp(outbuf + outlen - p->mlen, p->match, p->mlen) != 0)
            continue;

        if (p->sentinel) {
            /*
             * Sentinel fired: at the command prompt. Nothing to type;
             * every remaining normal pattern is stale now. The window
             * size is pushed the first time (pending is set at parse);
             * afterwards the main loop pushes it as soon as the local
             * window changed.
             */
            sentinel_seen = 1;
            if (nr_unused) {
                for (int k = 0; k < nr_patterns; k++)
                    if (!patterns[k].sentinel)
                        patterns[k].used = 1;
                nr_unused = 0;
            }
            if (winch_pending) {
                winch_pending = 0;
                sync_winsize(pty_fd);
            }
        } else {
            p->used = 1;
            nr_unused--;
            write_all(pty_fd, (const unsigned char *)p->response, strlen(p->response));
            write_all(pty_fd, (const unsigned char *)"\r", 1);
        }
        outlen = 0;
        return;
    }
}

/*
 * Create a pty and run the channel command on its slave side.
 * Returns the master fd and stores the child pid in *pidp.
 *
 * The child becomes a session leader with the slave as its
 * controlling terminal and stdin/stdout/stderr, then execs the
 * command: the command (ssh, virsh console, ...) is the channel that
 * carries the remote pty.
 *
 * The new pty is created as a copy of the local terminal: the same
 * termios settings and the same window size, so the command -- and
 * the remote pty it reaches -- sees the terminal the user is sitting
 * at from the very first byte. "ssh -tt" for instance relays that
 * window size to the remote pty right away, and the local line
 * settings are what the command would have seen without a bridge.
 */
static int open_command_pty(char **cmd, pid_t *pidp)
{
    int master, slave;
    struct termios tio, *tiop = NULL;
    struct winsize ws, *wsp = NULL;
    pid_t pid;

    if (tcgetattr(STDIN_FILENO, &tio) == 0)
        tiop = &tio;
    if (ioctl(STDIN_FILENO, TIOCGWINSZ, &ws) == 0)
        wsp = &ws;

    if (openpty(&master, &slave, NULL, tiop, wsp) < 0)
        die_errno("openpty");

    pid = fork();
    if (pid < 0)
        die_errno("fork");
    if (pid == 0) {
        /* child: make the slave the controlling terminal and run the command */
        close(master);
        if (setsid() < 0)
            _exit(127);
        if (ioctl(slave, TIOCSCTTY, NULL) < 0)
            _exit(127);
        dup2(slave, STDIN_FILENO);
        dup2(slave, STDOUT_FILENO);
        dup2(slave, STDERR_FILENO);
        if (slave > STDERR_FILENO)
            close(slave);
        execvp(cmd[0], cmd);
        fprintf(stderr, "%s: exec %s: %s\n", prog, cmd[0], strerror(errno));
        _exit(127);
    }

    close(slave);
    *pidp = pid;
    return master;
}

/*
 * Is the pty in raw mode yet? The termios settings are shared between
 * the master and the slave, so checking from the master sees the
 * channel command's own "stty raw"/cfmakeraw on the slave side. The
 * canonical flag is what distinguishes cooked from raw here.
 */
static int pty_is_raw(int fd)
{
    struct termios t;

    if (tcgetattr(fd, &t) < 0)
        return 1; /* error: stop waiting */
    return !(t.c_lflag & ICANON);
}

/*
 * COMMAND-mode startup phase: the channel command (ssh, virsh console,
 * ...) puts the pty into raw mode itself, but it may print -- a banner,
 * an error message -- before doing so. While the pty is still
 * canonical its line discipline converts \n to \r\n, so the local
 * terminal must stay cooked and the output is simply forwarded.
 *
 * This keeps polling the pty: reading the master and printing whatever
 * arrives, until the pty turns raw or the command exits. Every output
 * restarts the timeout, so the phase only gives up (and lets the main
 * loop proceed anyway) once the command has been silent for
 * STARTUP_TIMEOUT_SEC -- a command that is alive is expected to go
 * raw, and its output keeps the wait alive with it. The caller then
 * switches the local terminal to raw and enters the normal forwarding
 * loop.
 *
 * Called before any terminal change and before the signal handlers are
 * installed -- which is fine: the local tty has not been touched yet,
 * so a kill needs no restore, and SIGWINCH needs no handling either
 * (the first sentinel firing always pushes the window size).
 */
static void wait_pty_raw(int pty_fd)
{
    struct pollfd pfd = { .fd = pty_fd, .events = POLLIN };
    struct timespec ts = { .tv_nsec = 100 * 1000 * 1000 }; /* 100ms */
    time_t deadline = time(NULL) + STARTUP_TIMEOUT_SEC;
    unsigned char buf[4096];
    ssize_t n;

    while (!pty_is_raw(pty_fd)) {
        if (ppoll(&pfd, 1, &ts, NULL) > 0 && (pfd.revents & POLLIN)) {
            n = read(pty_fd, buf, sizeof(buf));
            if (n <= 0)
                return; /* command gone: let the main loop see it too */
            write_all(STDOUT_FILENO, buf, (size_t)n);
            deadline = time(NULL) + STARTUP_TIMEOUT_SEC;
        }
        if (time(NULL) >= deadline)
            return; /* silent for a while and still not raw: proceed */
    }
}

int main(int argc, char **argv)
{
    const char *escape_str = "^]";
    const char *pty_path = NULL;
    unsigned char escape_char;
    int pty_fd;
    pid_t child_pid = -1;
    struct termios raw;
    sigset_t winch_set, orig_set;
    int opt;
    char name[4];

    static const struct option long_options[] = {
        { "escape",  required_argument, NULL, 'e' },
        { "pattern", required_argument, NULL, 'p' },
        { "pty",     required_argument, NULL, 1 },
        { "help",    no_argument,       NULL, 'h' },
        { NULL, 0, NULL, 0 },
    };

    /*
     * The leading '+' stops at the first non-option argument: the
     * channel command's own options (e.g. "ssh -tt host") belong to
     * the command, not to us.
     */
    while ((opt = getopt_long(argc, argv, "+e:p:h", long_options, NULL)) != -1) {
        switch (opt) {
        case 'e':
            escape_str = optarg;
            break;
        case 'p': {
            struct pattern *p;
            /*
             * Split at the last space; the space belongs to the prompt,
             * so the full prompt text (e.g. "login: ") is compared.
             * An empty reply makes the pattern a sentinel (see below).
             */
            char *sep = strrchr(optarg, ' ');
            if (!sep || sep == optarg) {
                fprintf(stderr,
                        "%s: invalid pattern '%s' (expected \"<prompt> <reply>\")\n",
                        prog, optarg);
                return 2;
            }
            if (nr_patterns >= MAX_PATTERNS)
                die("too many patterns");
            p = &patterns[nr_patterns++];
            p->match = strndup(optarg, sep - optarg + 1);
            p->mlen = strlen(p->match);
            p->response = strdup(sep + 1);
            p->sentinel = p->response[0] == '\0';
            p->used = 0;
            if (p->sentinel) {
                if (has_sentinel)
                    die("only one sentinel pattern is allowed");
                has_sentinel = 1;
                winch_pending = 1; /* push the window size at the first prompt */
            } else {
                nr_unused++;
            }
            if (p->mlen > max_mlen)
                max_mlen = p->mlen;
            break;
        }
        case 1: /* --pty */
            pty_path = optarg;
            break;
        case 'h':
            usage(stdout);
            return 0;
        default:
            usage(stderr);
            return 2;
        }
    }

    if (pty_path) {
        if (optind != argc) {
            fprintf(stderr, "%s: COMMAND cannot be combined with --pty\n", prog);
            return 2;
        }
    } else if (optind >= argc) {
        usage(stderr);
        return 2;
    }

    escape_char = parse_escape(escape_str);

    /* stdin must be the controlling terminal */
    if (!isatty(STDIN_FILENO))
        die("stdin is not a terminal");
    if (tcgetsid(STDIN_FILENO) != getsid(0))
        die("stdin is not the controlling terminal of this process");

    /*
     * Our own message first -- the command output follows below. In
     * COMMAND mode announce the command being run, so the escape hint
     * the command itself may print later (e.g. virsh console prints
     * "Escape character is ^]" for the virtual machine console) is
     * not mistaken for ours.
     */
    escape_name(escape_char, name, sizeof(name));
    if (pty_path) {
        printf("Escape character is %s\n", name);
    } else {
        printf("Running: ");
        for (int i = optind; i < argc; i++)
            printf("%s%s", i > optind ? " " : "", argv[i]);
        printf("\n");
        printf("Escape character is %s (exits %s)\n", name, prog);
    }
    fflush(stdout);

    /*
     * Acquire the pty to bridge over. With --pty it is an existing
     * device (O_NOCTTY: do not steal the controlling terminal);
     * otherwise a fresh pty is created and COMMAND runs on its slave
     * side (see open_command_pty).
     */
    if (pty_path) {
        pty_fd = open(pty_path, O_RDWR | O_NOCTTY);
        if (pty_fd < 0)
            die_errno(pty_path);
        if (!isatty(pty_fd)) {
            close(pty_fd);
            die("specified path is not a terminal device");
        }
    } else {
        pty_fd = open_command_pty(argv + optind, &child_pid);

        /*
         * COMMAND mode: forward the command's early output while the
         * local terminal is still cooked, until the command puts the
         * pty into raw mode.
         */
        wait_pty_raw(pty_fd);
    }

    /* save the original local terminal settings, for restore on exit */
    tty_fd = STDIN_FILENO;
    if (tcgetattr(tty_fd, &saved_tio) < 0)
        die_errno("tcgetattr");
    tio_saved = 1;

    /*
     * Block SIGWINCH for the whole session and let ppoll unblock it
     * only while waiting: the signal is then delivered inside ppoll,
     * which returns EINTR -- no missed-wakeup race around the wait.
     */
    sigemptyset(&winch_set);
    sigaddset(&winch_set, SIGWINCH);
    sigprocmask(SIG_BLOCK, &winch_set, &orig_set);

    /* restore the terminal when killed by a signal */
    setup_signals();

    /* now switch the local terminal to raw */
    raw = saved_tio;
    cfmakeraw(&raw);
    raw.c_cc[VMIN] = 1;
    raw.c_cc[VTIME] = 0;
    if (tcsetattr(tty_fd, TCSANOW, &raw) < 0)
        die_errno("tcsetattr");

    /*
     * Set the pty to raw too -- only in --pty mode: in COMMAND mode
     * the channel command owns the pty settings and puts it into raw
     * mode itself (which the startup phase above waits for).
     */
    if (pty_path) {
        if (tcgetattr(pty_fd, &raw) < 0)
            die_errno("tcgetattr(pts)");
        cfmakeraw(&raw);
        raw.c_cc[VMIN] = 1;
        raw.c_cc[VTIME] = 0;
        if (tcsetattr(pty_fd, TCSANOW, &raw) < 0)
            die_errno("tcsetattr(pts)");
    }

    /* simulate pressing Enter so the shell behind the pty prints a fresh prompt */
    write_all(pty_fd, (const unsigned char *)"\r", 1);

    /* main loop: forward both directions */
    for (;;) {
        struct pollfd pfd[2] = {
            { .fd = STDIN_FILENO, .events = POLLIN },
            { .fd = pty_fd,       .events = POLLIN },
        };
        unsigned char buf[4096];
        ssize_t n;

        /*
         * Window changed. With a sentinel: push the new size only while
         * sitting at the command prompt -- output streaming (not at a
         * prompt) clears sentinel_seen, so the stty push defers to the
         * next prompt instead of injecting into a running program.
         * Without a sentinel there is no prompt boundary to wait for:
         * push the size into the pty via TIOCSWINSZ right away.
         */
        if (winch_pending) {
            if (sentinel_seen) {
                winch_pending = 0;
                sync_winsize(pty_fd);
            } else if (!has_sentinel) {
                winch_pending = 0;
                push_winsize(pty_fd);
            }
        }

        /*
         * SIGWINCH delivers only inside ppoll: the wait runs under the
         * original mask (without SIGWINCH), so the signal always wakes
         * it, and the blocked mask is back on return.
         */
        if (ppoll(pfd, 2, NULL, &orig_set) < 0) {
            if (errno == EINTR || errno == EAGAIN)
                continue;
            die_errno("ppoll");
        }

        /* keyboard -> pty */
        if (pfd[0].revents & POLLIN) {
            n = read(STDIN_FILENO, buf, sizeof(buf));
            if (n < 0) {
                if (errno == EINTR)
                    continue;
                break;
            }
            if (n == 0)
                break; /* stdin EOF */

            /* check the escape character: forward what came before, then exit */
            for (ssize_t i = 0; i < n; i++) {
                if (buf[i] == escape_char) {
                    if (i > 0)
                        write_all(pty_fd, buf, (size_t)i);
                    goto out;
                }
            }
            write_all(pty_fd, buf, (size_t)n);
        }

        /* pty -> screen */
        if (pfd[1].revents & (POLLIN | POLLERR | POLLHUP)) {
            n = read(pty_fd, buf, sizeof(buf));
            if (n < 0) {
                if (errno == EINTR || errno == EAGAIN)
                    continue;
                break; /* EIO etc.: pty peer is gone */
            }
            if (n == 0)
                break;
            write_all(STDOUT_FILENO, buf, (size_t)n);
            record_and_check(pty_fd, buf, (size_t)n);
        }
    }

out:
    /* move to a fresh line: the remote prompt we were sitting on has no \n */
    write_all(STDOUT_FILENO, (const unsigned char *)"\r\n", 2);
    restore_tty();
    close(pty_fd);

    /*
     * With a channel command the child holds the slave end: closing
     * the master above makes its reads fail, which normally terminates
     * it. Make sure it is gone, then reap it so it does not stay a
     * zombie.
     */
    if (child_pid > 0) {
        kill(child_pid, SIGHUP);
        waitpid(child_pid, NULL, 0);
    }
    return 0;
}
