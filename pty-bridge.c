/*
 * pty-bridge -- attach the local terminal to a pty (e.g. /dev/pts/N)
 *
 *   keyboard (stdin) --> /dev/pts/N      (stdin set to raw mode)
 *   screen (stdout)  <-- /dev/pts/N      (pts set to raw mode too)
 *
 * Requires stdin to be the controlling terminal of this process,
 * otherwise it exits immediately.
 *
 * Exit by pressing the escape character (default ^] = Ctrl-]),
 * or when the pty peer closes.
 *
 * With -p, the pty output is watched: when it ends exactly with a
 * pattern's prompt text (e.g. "login: "), the reply (plus Enter) is
 * typed automatically. Each pattern fires at most once. A pattern
 * with an empty reply (e.g. "]# ") is the sentinel for the command
 * prompt: it types nothing and retires all remaining patterns.
 *
 * Build: gcc -Wall -O2 -o pty-bridge pty-bridge.c
 * Usage: ./pty-bridge [-e CHAR|--escape CHAR] [-p "login: root"] /dev/pts/3
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <termios.h>
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
        "Usage: %s [OPTION]... <pty>\n"
        "Attach the local terminal to a pty: keyboard input is written to\n"
        "the pty, and pty output is printed on the screen.\n"
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
        "  -h, --help          show this help\n"
        "\n"
        "Examples:\n"
        "  %s /dev/pts/3\n"
        "  %s -e ^q /dev/pts/3      # exit with Ctrl-Q\n"
        "  %s -p \"login: root\" -p \"Password: secret\" /dev/pts/3\n",
        prog, prog, prog, prog);
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
static void sync_winsize(int pts_fd)
{
    struct winsize ws;
    char cmd[128];

    if (ioctl(STDIN_FILENO, TIOCGWINSZ, &ws) < 0)
        return;

    snprintf(cmd, sizeof(cmd), "stty rows %u columns %u\r",
             (unsigned)ws.ws_row, (unsigned)ws.ws_col);
    write_all(pts_fd, (const unsigned char *)cmd, strlen(cmd));
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
static void record_and_check(int pts_fd, const unsigned char *buf, size_t n)
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
                sync_winsize(pts_fd);
            }
        } else {
            p->used = 1;
            nr_unused--;
            write_all(pts_fd, (const unsigned char *)p->response, strlen(p->response));
            write_all(pts_fd, (const unsigned char *)"\r", 1);
        }
        outlen = 0;
        return;
    }
}

int main(int argc, char **argv)
{
    const char *escape_str = "^]";
    const char *pts_path = NULL;
    unsigned char escape_char;
    int pts_fd;
    struct termios raw;
    sigset_t winch_set, orig_set;
    int opt;
    char name[4];

    static const struct option long_options[] = {
        { "escape",  required_argument, NULL, 'e' },
        { "pattern", required_argument, NULL, 'p' },
        { "help",    no_argument,       NULL, 'h' },
        { NULL, 0, NULL, 0 },
    };

    /* parse arguments */
    while ((opt = getopt_long(argc, argv, "e:p:h", long_options, NULL)) != -1) {
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
        case 'h':
            usage(stdout);
            return 0;
        default:
            usage(stderr);
            return 2;
        }
    }

    if (optind + 1 != argc) {
        usage(stderr);
        return 2;
    }
    pts_path = argv[optind];

    escape_char = parse_escape(escape_str);

    /* stdin must be the controlling terminal */
    if (!isatty(STDIN_FILENO))
        die("stdin is not a terminal");
    if (tcgetsid(STDIN_FILENO) != getsid(0))
        die("stdin is not the controlling terminal of this process");

    /* open the pty. O_NOCTTY: do not steal the controlling terminal */
    pts_fd = open(pts_path, O_RDWR | O_NOCTTY);
    if (pts_fd < 0)
        die_errno(pts_path);
    if (!isatty(pts_fd)) {
        close(pts_fd);
        die("specified path is not a terminal device");
    }

    escape_name(escape_char, name, sizeof(name));
    printf("Escape character is %s\n", name);
    fflush(stdout);

    /* set local terminal (stdin) to raw, saving the original settings */
    tty_fd = STDIN_FILENO;
    if (tcgetattr(tty_fd, &saved_tio) < 0)
        die_errno("tcgetattr");
    tio_saved = 1;

    raw = saved_tio;
    cfmakeraw(&raw);
    raw.c_cc[VMIN] = 1;
    raw.c_cc[VTIME] = 0;
    if (tcsetattr(tty_fd, TCSANOW, &raw) < 0)
        die_errno("tcsetattr");

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

    /* set the pty to raw too */
    if (tcgetattr(pts_fd, &raw) < 0)
        die_errno("tcgetattr(pts)");
    cfmakeraw(&raw);
    raw.c_cc[VMIN] = 1;
    raw.c_cc[VTIME] = 0;
    if (tcsetattr(pts_fd, TCSANOW, &raw) < 0)
        die_errno("tcsetattr(pts)");

    /* simulate pressing Enter so the shell behind the pty prints a fresh prompt */
    write_all(pts_fd, (const unsigned char *)"\r", 1);

    /* main loop: forward both directions */
    for (;;) {
        struct pollfd pfd[2] = {
            { .fd = STDIN_FILENO, .events = POLLIN },
            { .fd = pts_fd,       .events = POLLIN },
        };
        unsigned char buf[4096];
        ssize_t n;

        /*
         * Window changed while sitting at the command prompt: push the
         * new size right away. Output streaming (not at a prompt)
         * clears sentinel_seen, so the push defers to the next prompt
         * instead of injecting stty into a running program.
         */
        if (sentinel_seen && winch_pending) {
            winch_pending = 0;
            sync_winsize(pts_fd);
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
                        write_all(pts_fd, buf, (size_t)i);
                    goto out;
                }
            }
            write_all(pts_fd, buf, (size_t)n);
        }

        /* pty -> screen */
        if (pfd[1].revents & (POLLIN | POLLERR | POLLHUP)) {
            n = read(pts_fd, buf, sizeof(buf));
            if (n < 0) {
                if (errno == EINTR || errno == EAGAIN)
                    continue;
                break; /* EIO etc.: pty peer is gone */
            }
            if (n == 0)
                break;
            write_all(STDOUT_FILENO, buf, (size_t)n);
            record_and_check(pts_fd, buf, (size_t)n);
        }
    }

out:
    /* move to a fresh line: the remote prompt we were sitting on has no \n */
    write_all(STDOUT_FILENO, (const unsigned char *)"\r\n", 2);
    restore_tty();
    close(pts_fd);
    return 0;
}
