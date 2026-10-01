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
 *   screen  (stdout) <-- pty      (the pty's mode is its owner's business)
 *
 * so the user operates the remote pty as if it were local. The pty
 * is either a local one (--pty /dev/pts/N) or a remote one reached
 * through a channel command (see usage below).
 *
 * Beyond plain forwarding it also automates the procedure that comes
 * with a remote pty: -p patterns auto-type replies to login prompts,
 * commands -- queued with -s, or a -p pattern sharing the sentinel's
 * match -- run at the prompt, one per prompt, until the queue is empty
 * and the bridge exits by itself, and the remote side is kept in sync
 * with the local terminal -- the window size on every change, TERM
 * once on a serial console (--term).
 *
 * Requires stdin to be the controlling terminal of this process,
 * otherwise it exits immediately.
 *
 * Exit by pressing the escape character (default ^] = Ctrl-]), when
 * the pty peer closes, or by itself once every queued command has
 * run.
 *
 * Build: gcc -Wall -O2 -o pty-bridge pty-bridge.c -lutil
 * Usage: ./pty-bridge [-e CHAR|--escape CHAR] [-p "login: root"] \
 *                     [-s "uptime"] [--term pty|serial|auto] [-v] \
 *                     [--pty /dev/pts/3] [command [arg ...]]
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <poll.h>
#include <pty.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static const char *prog = "pty-bridge";

/* -v: narrate what the bridge does, one line per event, on stderr */
static int verbose;

static int tty_fd = -1;          /* local terminal (stdin) */
static struct termios saved_tio; /* original terminal settings */
static int tio_saved = 0;

/*
 * Auto-reply pattern, from -p "<match> <reply>", split at the FIRST
 * space; the separating space stays with the match. The reply is
 * everything after it, verbatim, so it may itself contain spaces:
 * "login: root" types the username at the "login: " prompt,
 * "Password: secret" the password, "]# ls" runs the command once the
 * "]# " prompt shows up. Each pattern is used at most once. A pattern
 * whose match equals the sentinel's is a COMMAND (see the sentinel
 * comment below): not a stale login prompt to retire when the shell
 * prompt shows up, but a line for the sentinel to type at that prompt.
 * A --send option stores the same thing with no match of its own.
 */
#define MAX_PATTERNS 16
struct pattern {
    const char *match;    /* prompt text compared against the output tail */
    size_t mlen;          /* strlen(match), computed once at parse time */
    const char *response; /* string auto-typed after a match */
    int sentinel;         /* empty response: the command prompt marker */
    int command;          /* match equals the sentinel's: run at the prompt */
    int used;
};
static struct pattern patterns[MAX_PATTERNS];
static int nr_patterns;
static int nr_unused;     /* non-sentinel patterns still waiting to fire */
static int has_sentinel;  /* a sentinel pattern is configured */
static int has_commands;  /* commands are queued: exit once they all ran */
static size_t max_mlen;   /* record window size, see record_and_check() */

/*
 * Sentinel pattern: a pattern with an EMPTY response (e.g. -p "]# ").
 * It recognizes the shell command prompt -- the boundary where the
 * peer sits waiting for the user to type. It stays armed: every time
 * the prompt is seen again it can act again. When it fires nothing is
 * typed for the sentinel itself, and every still-unused normal
 * pattern is marked used (login prompts are stale once a shell prompt
 * showed up) -- except COMMAND patterns, which the prompt is exactly
 * for.
 *
 * A command pattern's match equals the sentinel's: its -p argument
 * starts with the sentinel's ("]# ls" after "]# "), so the split at
 * the first space leaves both with the same match. The -s/--send
 * option queues commands the same way with no match of its own; both
 * spellings share one queue, in command-line order. Each firing of
 * the sentinel types at most one line
 * -- the pending window-size push takes the turn first, else the next
 * command. One line per prompt, never two: a line typed at a prompt
 * comes back as exactly one prompt (the peer ran it and waits again),
 * so two lines typed at one prompt would return two prompts, and the
 * sentinel could not tell the second from the completion of a command
 * that never ran. A firing with nothing left to type therefore means
 * every queued command has run to completion: the session is over,
 * and quit_pending has the bridge leave by itself, the same way the
 * escape key leaves.
 *
 * The accounting rests on the sentinel's own premise: a prompt is the
 * peer waiting for input, and input is what gets typed -- every
 * prompt after a typed line is that line's completion. (The startup
 * Enter is deliberately not accounted: a serial console may swallow
 * it -- the startup phase retries it -- and its answer is the first
 * prompt the main loop sees either way.)
 *
 * winch_pending starts set on a serial channel -- it is set once
 * the terminal type resolves to serial -- so the first time the prompt
 * is seen the window size is pushed, together with the one-time TERM
 * export; afterwards it is pushed only when the local window changed
 * since (SIGWINCH). On a pty channel the size travels out of band --
 * COMMAND mode seeds it at openpty(), --pty mode pushes it once at
 * startup -- so nothing is pending at startup.
 */
static volatile sig_atomic_t winch_pending = 0;
static int sentinel_seen = 0; /* currently sitting at the command prompt */

/*
 * The sentinel fired at a prompt with the command queue empty: every
 * queued command has run to completion, the session is over. Set deep
 * inside record_and_check(); the main loop -- and the serial startup
 * phase -- honor it by leaving the way the escape key leaves.
 */
static int quit_pending;

/*
 * Terminal type of the channel (--term): what the channel can carry
 * to the remote end on its own. A "pty" channel (ssh -tt) allocates
 * its pty out of band -- it relays window changes and exports TERM
 * itself, so the bridge only pushes TIOCSWINSZ and types nothing. A
 * "serial" channel (virsh console) carries a serial console with no
 * out-of-band window-change and no environment: window size and TERM
 * must be typed into the guest as shell commands, which is only safe
 * at a prompt, i.e. guarded by a sentinel. "auto" picks serial for a
 * "virsh console" command and pty for everything else; a serial
 * setting without a sentinel falls back to the pty method.
 */
#define TERM_AUTO   0
#define TERM_PTY    1
#define TERM_SERIAL 2
static int term_mode = TERM_AUTO;
static int term_serial;   /* resolved: type stty/export at the prompt */
static int term_pending;  /* one-time TERM export still pending */
static char term_val[64]; /* TERM value to export, from the environment */

/* Sliding window of recent pty output, for pattern matching */
#define OUTBUF_SIZE 4096
static unsigned char outbuf[OUTBUF_SIZE];
static size_t outlen;

/*
 * How long the serial-console startup phase waits for the console to
 * answer an Enter before it types another one.
 */
#define ENTER_RETRY_SEC 3

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

/*
 * Verbose log (-v), one line per event, prefixed with the program name
 * like every other stderr message. The session runs with the local
 * terminal in raw mode, where a bare "\n" only moves down a line: end
 * the line with "\r\n" when stderr is that terminal, with a plain "\n"
 * when it is a file or pipe.
 */
static void vlog(const char *fmt, ...)
{
    va_list ap;

    if (!verbose)
        return;
    fprintf(stderr, "%s: ", prog);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputs(isatty(STDERR_FILENO) ? "\r\n" : "\n", stderr);
}

/* -v trace line: the verbose guard lives here, not at every call site */
#define VLOG(...) do { if (verbose) vlog(__VA_ARGS__); } while (0)

/*
 * Render n bytes for a verbose log line: printable characters and
 * UTF-8 pass through, the whitespace that would garble the line shows
 * as \r \n \t, anything else invisible or line-breaking as \xHH.
 * Cycles through a few static buffers so a single vlog can
 * interpolate several of these. Long data is cut short: log lines
 * stay readable.
 */
#define ESC_MAX 512
static const char *esc_bytes(const void *data, size_t n)
{
    static char bufs[3][ESC_MAX * 4 + 8];
    static unsigned rot;
    const unsigned char *s = data;
    char *p = bufs[rot];
    const char *ret = bufs[rot];
    int trunc = n > ESC_MAX;

    rot = (rot + 1) % 3;
    if (trunc)
        n = ESC_MAX;
    for (size_t i = 0; i < n; i++) {
        unsigned char c = s[i];

        if (c == '\r') {
            *p++ = '\\'; *p++ = 'r';
        } else if (c == '\n') {
            *p++ = '\\'; *p++ = 'n';
        } else if (c == '\t') {
            *p++ = '\\'; *p++ = 't';
        } else if (c == '\\') {
            *p++ = '\\'; *p++ = '\\';
        } else if (c >= 0x20 && c != 0x7f) {
            *p++ = (char)c;
        } else {
            sprintf(p, "\\x%02x", c);
            p += 4;
        }
    }
    if (trunc) {
        *p++ = '.'; *p++ = '.'; *p++ = '.';
    }
    *p = '\0';
    return ret;
}

/* esc_bytes() for a NUL-terminated string */
static const char *esc_str(const char *s)
{
    return esc_bytes(s, strlen(s));
}

static void usage(FILE *out)
{
    fprintf(out,
        "Usage: %s [OPTION]... [--pty PTY] [COMMAND [ARG]...]\n"
        "\n"
        "Attach the local terminal to a pty: keyboard input is written to the pty, and\n"
        "pty output is printed on the screen. With --pty the existing pty PTY (e.g.\n"
        "/dev/pts/3) is attached; otherwise a new pty is created and COMMAND runs as the\n"
        "channel to the remote pty, such as \"ssh -tt host\" or \"virsh console vm\".\n"
        "\n"
        "Options:\n"
        "  -e, --escape CHAR   exit character, default ^] (Ctrl-]); CHAR is a single\n"
        "                      character or ^X form\n"
        "  -p, --pattern SPEC  auto-reply. SPEC is \"<match> <reply>\", split at space:\n"
        "                      when the pty output ends exactly with <match> (trailing\n"
        "                      space included), type <reply> plus Enter;\n"
        "                      <reply> may contain spaces. E.g. \"login: root\" types the\n"
        "                      username, \"Password: secret\" the password, \"]# cd /tmp/\"\n"
        "                      runs a command. Repeatable; each pattern fires at most\n"
        "                      once. An empty reply (e.g. \"]# \") is a sentinel for the\n"
        "                      command prompt: nothing is typed, and all still-unused\n"
        "                      patterns are marked used. A pattern with the sentinel's\n"
        "                      own match (e.g. \"]# ls\" beside \"]# \") queues a command\n"
        "                      too -- see --send\n"
        "  -s, --send CMD      queue CMD to be typed at the sentinel prompt, one per\n"
        "                      prompt, in the order given; repeatable. The bridge exits\n"
        "                      by itself once every queued command has run.\n"
        "                      Needs a sentinel pattern (a -p with an empty reply)\n"
        "      --pty PATH      attach to an existing pty (e.g. /dev/pts/3) instead of\n"
        "                      running COMMAND on a new pty\n"
        "  -t, --term pty|serial|auto\n"
        "                      how the window size and TERM are synced with the remote\n"
        "                      end. pty: the channel carries window changes itself\n"
        "                      (ssh -tt); the size is pushed with TIOCSWINSZ, nothing\n"
        "                      is typed. serial: the channel is a serial console\n"
        "                      (virsh console); \"stty rows R columns C\" is typed at\n"
        "                      the prompt -- needs a sentinel pattern, otherwise pty\n"
        "                      applies -- and TERM is exported with the first push.\n"
        "                      auto (default): a \"virsh console\" COMMAND means serial,\n"
        "                      anything else pty.\n"
        "  -v, --verbose       trace what happens on stderr: the parsed patterns, the\n"
        "                      output tail checked against them, which pattern matched\n"
        "                      and what was typed, window-size pushes, and the\n"
        "                      serial-console startup phase (Enter pokes until the\n"
        "                      console answers)\n"
        "  -h, --help          show this help\n"
        "\n"
        "Options must precede COMMAND; use -- to separate them.\n"
        "\n"
        "Examples:\n"
        "  %s --pty /dev/pts/3\n"
        "  %s -e ^q --pty /dev/pts/3     # exit with Ctrl-Q\n"
        "  %s -p \"login: root\" -p \"]# \" -- ssh -tt admin@10.0.0.1\n"
        "  %s -p \"]# \" -s \"ls\" -- ssh -tt host   # run ls at the prompt,\n"
        "                                                # exit when it returns\n"
        "  %s -p \"]# \" --term serial -- virsh console vm1\n",
        prog, prog, prog, prog, prog, prog);
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
 * "virsh console VM" -- the channel command carries a serial console.
 * The one command shape auto mode recognizes.
 */
static int is_virsh_console(char **cmd, int ncmd)
{
    const char *base;

    if (ncmd < 2)
        return 0;
    base = strrchr(cmd[0], '/');
    base = base ? base + 1 : cmd[0];
    return strcmp(base, "virsh") == 0 && strcmp(cmd[1], "console") == 0;
}

/*
 * Is TERM safe to splice into the "export TERM=..." line typed into
 * the guest? Only what a terminfo name can contain passes -- letters,
 * digits and "+-._". Anything else (a space, a quote, a metacharacter)
 * would be mangled or executed by the remote shell, so the export is
 * skipped instead.
 */
static int term_val_ok(const char *t)
{
    for (; *t; t++) {
        char c = *t;
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') ||
              c == '+' || c == '-' || c == '.' || c == '_'))
            return 0;
    }
    return 1;
}

/*
 * Resolve --term into the method actually used. "auto" recognizes a
 * single command shape -- "virsh console", a serial console channel --
 * and picks the serial method for it, the pty method for everything
 * else. The serial method types into the session, which is only safe
 * at a prompt: without a sentinel pattern there is no prompt boundary
 * to wait for, so it warns and falls back to the pty method (window
 * size via TIOCSWINSZ only, no TERM). TERM is taken from the
 * environment once, and only if it is a plain terminfo-style name --
 * anything else would not survive the shell command line it is spliced
 * into.
 */
static void resolve_term_mode(const char *pty_path, char **cmd, int ncmd)
{
    const char *t;

    switch (term_mode) {
    case TERM_SERIAL:
        term_serial = 1;
        break;
    case TERM_PTY:
        term_serial = 0;
        break;
    default:
        term_serial = !pty_path && is_virsh_console(cmd, ncmd);
        break;
    }

    if (term_serial && !has_sentinel) {
        /*
         * No prompt boundary: nothing to type at. The user asked for
         * the serial method -- explicitly, or auto picked it for a
         * virsh console command -- so say what the session loses
         * instead of failing silently.
         */
        fprintf(stderr,
                "%s: serial mode needs a sentinel pattern; "
                "falling back to TIOCSWINSZ sync (no TERM export)\n",
                prog);
        term_serial = 0;
    }

    term_pending = 0;
    t = getenv("TERM");
    if (term_serial && t != NULL && *t != '\0') {
        size_t tl = strlen(t);
        if (tl < sizeof(term_val) && term_val_ok(t)) {
            memcpy(term_val, t, tl + 1);
            term_pending = 1;
        } else {
            fprintf(stderr,
                    "%s: not exporting unusable TERM '%s' to the guest\n",
                    prog, t);
        }
    }

    /*
     * The serial method can only push the window size (and the
     * one-time TERM export) at a prompt: queue the initial push so
     * the first prompt fires it. The pty method carries the size out
     * of band -- COMMAND mode seeds the new pty with the local size
     * at openpty(), and --pty mode pushes it once at startup (main) --
     * so nothing is pending at startup; resizes are pushed as they
     * happen.
     */
    winch_pending = term_serial;
}

/*
 * Push the local window size into the session behind the pty by
 * typing shell commands. Typed in the open: hiding it with
 * "stty -echo" would leave a "stty -echo" string on screen anyway, so
 * just let the user see the resize happen. The first push also
 * exports TERM, once: a serial channel carries no environment, so the
 * guest learns TERM the same way it learns the window size -- by
 * being told.
 */
static void sync_winsize(int pty_fd)
{
    struct winsize ws;
    char cmd[192];

    if (ioctl(STDIN_FILENO, TIOCGWINSZ, &ws) < 0)
        return;

    if (term_pending) {
        term_pending = 0;
        snprintf(cmd, sizeof(cmd),
                 "export TERM=%s; stty rows %u columns %u\r",
                 term_val, (unsigned)ws.ws_row, (unsigned)ws.ws_col);
    } else {
        snprintf(cmd, sizeof(cmd), "stty rows %u columns %u\r",
                 (unsigned)ws.ws_row, (unsigned)ws.ws_col);
    }
    VLOG("typing '%s'", esc_str(cmd));
    write_all(pty_fd, (const unsigned char *)cmd, strlen(cmd));
}

/*
 * Push the local window size into the pty directly with TIOCSWINSZ.
 * Used for a pty channel (--term pty, or auto): the channel passes
 * window changes through, and the ioctl makes the kernel deliver
 * SIGWINCH to the foreground process group behind the pty -- what a
 * real terminal does on a resize. Nothing is typed.
 */
static void push_winsize(int pty_fd)
{
    struct winsize ws;

    if (ioctl(STDIN_FILENO, TIOCGWINSZ, &ws) == 0) {
        ioctl(pty_fd, TIOCSWINSZ, &ws);
        VLOG("window size %dx%d pushed via TIOCSWINSZ", ws.ws_row, ws.ws_col);
    }
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
 * keeps it alive. The record size starts at the longest pattern text;
 * once the sentinel fires -- every normal pattern is retired or used
 * by then, and commands share the sentinel's own match -- it shrinks
 * to that length, the only match text still able to fire.
 */
static void record_and_check(int pty_fd, const unsigned char *buf, size_t n)
{
    size_t chunk_len = n; /* the read size, for the log; n is trimmed below */

    if (nr_unused == 0 && !has_sentinel) {
        /*
         * Nothing left to match: never record again. With -v say so
         * once -- every later chunk would repeat it.
         */
        static int logged_done;

        if (!logged_done) {
            logged_done = 1;
            VLOG("nothing left to match; recording stopped");
        }
        return;
    }

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

    VLOG("pty: %zu bytes, tail: '%s'", chunk_len, esc_bytes(outbuf, outlen));

    for (int j = 0; j < nr_patterns; j++) {
        struct pattern *p = &patterns[j];

        /*
         * Commands never fire here: they share the sentinel's match,
         * so it is the sentinel's firing below that types them, one
         * per prompt, in order.
         */
        if (p->used || p->command || p->mlen == 0 || p->mlen > outlen)
            continue;
        if (memcmp(outbuf + outlen - p->mlen, p->match, p->mlen) != 0) {
            VLOG("  pattern[%d] '%s': no match", j, esc_str(p->match));
            continue;
        }

        if (p->sentinel) {
            /*
             * Sentinel fired: at the command prompt. Nothing is typed
             * for the sentinel itself, and every still-unused normal
             * pattern is stale now -- except commands, which the
             * prompt is exactly for. What happens at the prompt
             * happens here, one line at most (see the sentinel
             * comment above): the pending window-size push takes the
             * turn first, else the next queued command. A firing with
             * neither left means every command ran to completion:
             * the session is over.
             */
            struct pattern *cmd = NULL;
            int retired = 0;

            sentinel_seen = 1;
            VLOG("  pattern[%d] sentinel '%s': matched", j, esc_str(p->match));
            for (int k = 0; k < nr_patterns; k++) {
                struct pattern *q = &patterns[k];

                if (!q->sentinel && !q->command && !q->used) {
                    q->used = 1;
                    retired++;
                }
                if (q->command && !q->used && !cmd)
                    cmd = q;
            }
            if (retired) {
                nr_unused -= retired;
                VLOG("  retiring %d stale pattern(s)", retired);
            }
            /*
             * Only the sentinel can still match (commands share its
             * match): shrink the record window to its own length.
             */
            if (max_mlen > p->mlen) {
                max_mlen = p->mlen;
                VLOG("  recording trimmed to the last %zu bytes", max_mlen);
            }
            if (winch_pending) {
                winch_pending = 0;
                sync_winsize(pty_fd);
            } else if (cmd) {
                cmd->used = 1;
                nr_unused--;
                VLOG("  typing command '%s' + Enter", esc_str(cmd->response));
                write_all(pty_fd, (const unsigned char *)cmd->response,
                          strlen(cmd->response));
                write_all(pty_fd, (const unsigned char *)"\r", 1);
            } else if (has_commands) {
                VLOG("  every command ran; exiting");
                quit_pending = 1;
            }
        } else {
            p->used = 1;
            nr_unused--;
            VLOG("  pattern[%d] '%s': matched, typing reply '%s' + Enter",
                 j, esc_str(p->match), esc_str(p->response));
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
 * Wake the serial console: wait for it to connect, then poke it into
 * showing its prompt. Runs for a COMMAND mode channel resolved to the
 * serial method -- a "virsh console" command picked by auto, or an
 * explicit --term serial. A pty channel (ssh) does not run this at
 * all: its prompts are printed immediately, unasked, and the main
 * loop matches them wherever they appear.
 *
 * A serial console is silent until spoken to: the guest behind it is
 * already sitting waiting for input, and its prompt appears only in
 * answer to an Enter. So:
 *
 * 1. Before the pty is raw, the channel command may print -- a
 *    banner, an error message, a question it asks on the way up --
 *    through the still-canonical line discipline (\n -> \r\n), which
 *    is what the still-cooked local terminal needs for display.
 *    Whatever arrives is printed and recorded: record_and_check()
 *    runs here too, so a prompt printed before the switch is matched
 *    and answered during the wait, exactly as the main loop would
 *    match it. There is no timeout: the command is expected to put
 *    the pty into raw mode once it has connected (virsh console
 *    does), and if it exits instead the phase returns and lets the
 *    main loop see the exit. The poll wakes every 100ms to notice
 *    the switch.
 * 2. Once the pty is raw the console is connected -- and possibly
 *    silent at its prompt. An Enter is typed, but only after the
 *    poll has seen raw mode, never before: the command may make its
 *    switch with tcsetattr(slave, TCSAFLUSH, &raw) -- not every
 *    command uses the flushing variant, but nothing promises one
 *    that doesn't -- and TCSAFLUSH discards input that is still
 *    pending unread, so a \r typed into the still-canonical pty
 *    would be dropped by the very switch that was supposed to
 *    deliver it to the console. (A pattern reply typed in 1 runs
 *    no such risk, a queued command included: it answers a prompt
 *    that is being read right then, so it is consumed before the
 *    switch happens; the Enter has no reader until the console is
 *    connected, and must wait.) After the Enter the pty is polled:
 *    as soon as it answers, or the command exits, the phase returns WITHOUT
 *    reading -- the output is the main loop's, where the patterns
 *    match it. While the console stays silent the Enter is
 *    repeated every ENTER_RETRY_SEC: a virtual machine may take a
 *    while to reach its getty, and a silent console has nothing
 *    better to offer than another Enter.
 *
 * Called before any terminal change and before the signal handlers
 * are installed -- which is fine: the local tty has not been touched
 * yet, so a kill needs no restore, and SIGWINCH needs no handling
 * either (the size is pushed at the first prompt -- the prompt the
 * typed Enter is there to produce).
 */
static void wake_serial_console(int pty_fd)
{
    struct pollfd pfd = { .fd = pty_fd, .events = POLLIN };
    struct timespec ts = { .tv_nsec = 100 * 1000 * 1000 }; /* 100ms */
    unsigned char buf[4096];
    ssize_t n;

    while (!pty_is_raw(pty_fd)) {
        if (ppoll(&pfd, 1, &ts, NULL) > 0 &&
            (pfd.revents & (POLLIN | POLLERR | POLLHUP))) {
            n = read(pty_fd, buf, sizeof(buf));
            if (n <= 0) {
                VLOG("startup: command gone");
                return; /* command gone: let the main loop see it too */
            }
            VLOG("startup: %zu bytes, pty not raw yet", (size_t)n);
            write_all(STDOUT_FILENO, buf, (size_t)n);
            record_and_check(pty_fd, buf, (size_t)n);
            if (quit_pending)
                return; /* every command ran before the pty went raw */
        }
    }

    /* the console is connected; poke it and wait for it to answer */
    write_all(pty_fd, (const unsigned char *)"\r", 1);
    VLOG("startup: pty is raw; typed Enter");
    ts.tv_sec = ENTER_RETRY_SEC;
    ts.tv_nsec = 0;
    for (;;) {
        if (ppoll(&pfd, 1, &ts, NULL) > 0 &&
            (pfd.revents & (POLLIN | POLLERR | POLLHUP))) {
            if (pfd.revents & POLLIN)
                VLOG("startup: console responded");
            else
                VLOG("startup: command gone");
            return; /* the main loop reads the output, or sees the exit */
        }
        write_all(pty_fd, (const unsigned char *)"\r", 1);
        VLOG("startup: console silent for %d s; typed Enter again",
             ENTER_RETRY_SEC);
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
    int nr_sends = 0;
    char name[4];

    static const struct option long_options[] = {
        { "escape",  required_argument, NULL, 'e' },
        { "pattern", required_argument, NULL, 'p' },
        { "send",    required_argument, NULL, 's' },
        { "term",    required_argument, NULL, 't' },
        { "verbose", no_argument,       NULL, 'v' },
        { "pty",     required_argument, NULL, 1 },
        { "help",    no_argument,       NULL, 'h' },
        { NULL, 0, NULL, 0 },
    };

    /*
     * The leading '+' stops at the first non-option argument: the
     * channel command's own options (e.g. "ssh -tt host") belong to
     * the command, not to us.
     */
    while ((opt = getopt_long(argc, argv, "+e:p:s:t:vh", long_options, NULL)) != -1) {
        switch (opt) {
        case 'e':
            escape_str = optarg;
            break;
        case 'p': {
            struct pattern *p;
            /*
             * Split at the first space; the separating space stays with
             * the match, the reply keeps every space it has (a command
             * to run, e.g. "cd /tmp/"). An empty reply makes the
             * pattern a sentinel (see below).
             */
            char *sep = strchr(optarg, ' ');
            if (!sep || sep == optarg) {
                fprintf(stderr,
                        "%s: invalid pattern '%s' (expected \"<match> <reply>\")\n",
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
            } else {
                nr_unused++;
            }
            if (p->mlen > max_mlen)
                max_mlen = p->mlen;
            break;
        }
        case 's': {
            struct pattern *p;

            if (!*optarg) {
                fprintf(stderr, "%s: empty --send command\n", prog);
                return 2;
            }
            if (nr_patterns >= MAX_PATTERNS)
                die("too many patterns");
            p = &patterns[nr_patterns++];
            /*
             * No match of its own: a --send command is typed at the
             * sentinel's prompt, whenever that shows up. command is
             * set here rather than by the classification pass below,
             * so the entry joins the queue in command-line order with
             * the -p commands around it.
             */
            p->match = NULL;
            p->mlen = 0;
            p->response = strdup(optarg);
            p->sentinel = 0;
            p->command = 1;
            p->used = 0;
            nr_unused++;
            has_commands = 1;
            nr_sends++;
            break;
        }
        case 't':
            if (!strcmp(optarg, "auto"))
                term_mode = TERM_AUTO;
            else if (!strcmp(optarg, "pty"))
                term_mode = TERM_PTY;
            else if (!strcmp(optarg, "serial"))
                term_mode = TERM_SERIAL;
            else {
                fprintf(stderr,
                        "%s: invalid --term mode '%s' (pty|serial|auto)\n",
                        prog, optarg);
                return 2;
            }
            break;
        case 1: /* --pty */
            pty_path = optarg;
            break;
        case 'v':
            verbose = 1;
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

    /*
     * --send types at the sentinel's prompt: without a sentinel there
     * is no prompt boundary to wait for, and the command would be
     * typed blind. (The serial method refuses the same way, for the
     * same reason.)
     */
    if (nr_sends && !has_sentinel) {
        fprintf(stderr,
                "%s: --send needs a sentinel pattern (e.g. -p \"]# \")\n",
                prog);
        return 2;
    }

    /*
     * Classify the commands: a non-sentinel pattern whose match equals
     * the sentinel's. Its -p argument starts with the sentinel's
     * ("ls" after "]# "), so the split at the first space leaves both
     * with the same match -- the prompt -- and the reply is what to
     * run there. The sentinel types them one per prompt, in -p order,
     * and exits the bridge once they all ran (see the sentinel comment
     * above record_and_check()). A --send entry is already a command
     * and carries no match, so it takes no part in this.
     */
    if (has_sentinel) {
        const char *smatch = NULL;

        for (int i = 0; i < nr_patterns; i++)
            if (patterns[i].sentinel)
                smatch = patterns[i].match;
        for (int i = 0; i < nr_patterns; i++) {
            if (!patterns[i].sentinel && !patterns[i].command &&
                strcmp(patterns[i].match, smatch) == 0) {
                patterns[i].command = 1;
                has_commands = 1;
            }
        }
    }

    /* -v: show the patterns as parsed, before anything runs */
    if (verbose) {
        for (int i = 0; i < nr_patterns; i++) {
            struct pattern *p = &patterns[i];

            if (p->sentinel)
                VLOG("pattern[%d] sentinel: match '%s'", i, esc_str(p->match));
            else if (p->command) {
                if (p->match)
                    VLOG("pattern[%d] match '%s' -> command '%s'",
                         i, esc_str(p->match), esc_str(p->response));
                else
                    VLOG("pattern[%d] -> command '%s'",
                         i, esc_str(p->response));
            } else
                VLOG("pattern[%d] match '%s' -> reply '%s'",
                     i, esc_str(p->match), esc_str(p->response));
        }
        if (nr_patterns)
            VLOG("matching the last %zu bytes of pty output", max_mlen);
    }

    /* resolve the terminal type now that the command (if any) is known */
    resolve_term_mode(pty_path, argv + optind, argc - optind);

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
         * Serial channel -- a "virsh console" command picked by auto,
         * or an explicit --term serial; both carry a console that is
         * silent until spoken to: wait for it to connect (the command
         * puts the pty into raw mode) and poke it into showing its
         * prompt. A pty channel (ssh) gets neither: its prompts are
         * printed immediately, and the main loop matches them wherever
         * they appear.
         */
        if (term_serial) {
            wake_serial_console(pty_fd);
            if (quit_pending)
                goto out; /* every command ran during the startup wait */
        }
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
     * --pty mode: the attached pty keeps whatever size the previous
     * session left behind, and nothing carries the local size to it
     * out of band -- push it once now, so attaching fixes a stale
     * window (COMMAND mode had the size seeded at openpty() instead).
     */
    if (pty_path && !term_serial)
        push_winsize(pty_fd);

    /*
     * --pty mode: simulate pressing Enter so the session behind the
     * pty prints a fresh prompt -- it may have sat at its prompt since
     * long before the attach, silent, where no pattern could see it. A
     * COMMAND channel must NOT get it here: a serial console was
     * already poked by its startup phase (Enter, retried until it
     * answered), and a pty channel prints its prompts immediately -- a
     * bare "\r" would only wait in the input queue to be consumed as
     * an empty answer by the first prompt (the ssh host-key
     * confirmation, typically).
     */
    if (pty_path)
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
         * Window changed. pty channel: push the new size into the pty
         * via TIOCSWINSZ right away -- the kernel delivers SIGWINCH to
         * the foreground process group behind it, which is what a real
         * terminal does on a resize. Serial channel: the size must be
         * typed in as stty, which is only safe at a prompt -- output
         * streaming (not at a prompt) clears sentinel_seen, so the
         * push defers to the next prompt instead of injecting into a
         * running program. With commands queued even the at-prompt
         * push waits for the sentinel's own firing: a prompt gets at
         * most one typed line (see record_and_check), and a push
         * typed here -- right after the sentinel typed a command into
         * the same prompt -- would come back as an extra prompt the
         * queue's accounting never asked for.
         */
        if (winch_pending) {
            if (!term_serial) {
                winch_pending = 0;
                push_winsize(pty_fd);
            } else if (sentinel_seen && !has_commands) {
                winch_pending = 0;
                sync_winsize(pty_fd);
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
            if (quit_pending)
                goto out; /* every command ran: leave like the escape key */
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
