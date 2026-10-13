/*
 * pty-proxy -- attach the local terminal to a pty, local or remote
 *
 * The terminal a user wants to work on is not always a local
 * /dev/pts/N. Very often it is a REMOTE pty: the shell of an ssh
 * session, the console of a virtual machine, a serial console behind
 * a gateway. Such a pty cannot be opened directly -- it is only
 * reachable through a channel command that connects to it, such as
 * "ssh", "virsh console" or "telnet".
 *
 * pty-proxy stands between the local terminal and that pty:
 *
 *   keyboard (stdin) --> pty      (local terminal: raw input,
 *   screen  (stdout) <-- pty       output flags untouched)
 *
 * so the user operates the remote pty as if it were local. The pty
 * is either a local one (--pty /dev/pts/N) or a remote one reached
 * through a channel command (see usage below).
 *
 * Beyond plain forwarding it also automates the procedure that comes
 * with a remote pty: -p patterns auto-type replies to login prompts,
 * commands -- queued with -s, or a -p pattern sharing the sentinel's
 * match -- run at the prompt, one per prompt, until the queue is empty
 * and the proxy exits by itself, and the remote side is kept in sync
 * with the local terminal -- the window size on every change, TERM
 * once on a serial console (--term). Files travel the same way: --put
 * and --get queue a remote rz or sz, and the proxy speaks ZMODEM to it
 * (zmodem.c) -- all a serial console needs to get a file in or out.
 *
 * Requires stdin to be the controlling terminal of this process,
 * otherwise it exits immediately -- unless commands are queued: such a
 * run ends by itself once they ran, nobody needs to type into it, and
 * it goes ahead unattended without reading stdin at all.
 *
 * Exit by pressing the escape character (default ^] = Ctrl-]), when
 * the pty peer closes, or by itself once every queued command has
 * run.
 *
 * Build: gcc -Wall -O2 -o pty-proxy pty-proxy.c -lutil
 *        (zmodem.c is #included, not compiled on its own)
 * Usage: ./pty-proxy [-e CHAR|--escape CHAR] [-p "login: root"] \
 *                     [-s "uptime"] [--put FILE] [--get FILE] \
 *                     [--term pty|serial|auto] [-v] \
 *                     [-w "Escape character is ^]"] \
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
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static const char *prog = "pty-proxy";

/* -v: narrate what the proxy does, one line per event, on stderr */
static int verbose;

static int tty_fd = -1;          /* local terminal (stdin) */
static struct termios saved_tio; /* original terminal settings */
static int tio_saved = 0;

/*
 * Unattended run: stdin is not the controlling terminal, but commands
 * are queued, so the proxy exits by itself once they ran and nobody
 * has to type anything. stdin is never read (an EOF on it must not end
 * the run early) and never switched to raw; the escape key is gone
 * with it.
 *
 * local_fd is the local terminal the remote side is described by --
 * its termios and window size seed the channel pty, its size is what
 * gets pushed: stdin normally; in an unattended run stdout if that is
 * still a terminal (the output is watched there), else -1 -- there is
 * no terminal to describe, and no size or TERM is pushed at all.
 */
static int unattended;
static int local_fd = STDIN_FILENO;

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
    int xfer;             /* --put/--get: ZM_PUT/ZM_GET, the command is rz/sz */
    const char *path;     /* ... the file to put or get */
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
 * and quit_pending has the proxy leave by itself, the same way the
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
 * ^C in an unattended run: stdin is not read, so the key never reaches
 * the pty as a byte -- the terminal turns it into SIGINT for the proxy
 * instead. Ending the proxy alone would leave the command running on
 * the remote end (on a serial console nothing hangs up on the guest's
 * side at all), so the interrupt is passed on: a ^C byte is typed into
 * the pty -- the remote line discipline turns it into SIGINT for the
 * remote foreground job -- and the proxy leaves at the prompt that
 * comes back, or after INTR_WAIT_SEC if none does. A second ^C leaves
 * at once. Either way the proxy then dies of SIGINT, so its caller
 * sees the run was interrupted.
 */
#define INTR_WAIT_SEC 5
static volatile sig_atomic_t intr_count; /* SIGINTs received */
static int intr_sent;                    /* the ^C byte has been typed */
static struct timespec intr_at;          /* ... at this moment */

/*
 * File transfer (--put/--get): a queued command like any other, typed
 * at the prompt by the sentinel -- "rz" to put a file, "sz FILE" to get
 * one -- after which the pty belongs to the ZMODEM transfer (zmodem.c)
 * until rz/sz is done; the prompt that comes back then is the
 * command's completion, as with every other command. zm_job is the
 * transfer just typed, for the main loop to run. Should the prompt come
 * back before rz/sz even spoke (no rz on the remote end, a file sz
 * cannot open), the sentinel says so in zm_prompt, and types nothing.
 *
 * A failed transfer -- broken off by the user (^C, the escape key), by
 * the remote end, a timeout -- ends the run: the rest of the queue is
 * not typed, and the proxy exits with status 1 (xfer_failed).
 */
static struct pattern *zm_job;
static int zm_prompt;
static int xfer_failed;

/*
 * Terminal type of the channel (--term): what the channel can carry
 * to the remote end on its own. A "pty" channel (ssh -tt) allocates
 * its pty out of band -- it relays window changes and exports TERM
 * itself, so the proxy only pushes TIOCSWINSZ and types nothing. A
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

/*
 * -w/--wait-for: text the serial channel must have printed before the
 * startup Enter is typed, e.g. virsh console's "Escape character is
 * ^]" -- the channel says it is connected, so an Enter now reaches the
 * console rather than the command. Searched anywhere in the output,
 * not only at its tail: it is a banner line, not a prompt. NULL: the
 * raw switch alone decides.
 *
 * Seeing the text is not quite the moment either: the command prints
 * it on its way into the console and still has the last steps of its
 * setup to take. The Enter waits WAIT_SETTLE_MS more after the text
 * shows up -- reading on all the while, so nothing it prints then is
 * left behind to pass for the console's answer.
 */
#define WAIT_MAX 256
#define WAIT_SETTLE_MS 100
static const char *wait_text;
static size_t wait_len;

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

/*
 * SIGINT handler of an unattended run: the first one only records the
 * ^C for the main loop to pass on (blocked outside ppoll, like
 * SIGWINCH); a second one leaves at once, as any other signal does.
 */
static void on_intr(int sig)
{
    if (intr_count++ == 0)
        return;
    on_signal(sig);
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

    if (unattended) {
        sa.sa_handler = on_intr;
        sigaction(SIGINT, &sa, NULL);
    }

    /* no local terminal: no window to follow, SIGWINCH keeps its default */
    if (local_fd < 0)
        return;
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
 * like every other stderr message. A bare "\n" is correct wherever
 * stderr points: raw applies to the local terminal's input side only,
 * so the tty's own output flags render the newline, and a file or
 * pipe takes the "\n" as it is.
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
    fputc('\n', stderr);
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

/* Milliseconds elapsed since *t, on the monotonic clock */
static long ms_since(const struct timespec *t)
{
    struct timespec now;

    clock_gettime(CLOCK_MONOTONIC, &now);
    return (now.tv_sec - t->tv_sec) * 1000 +
           (now.tv_nsec - t->tv_nsec) / 1000000;
}

/* ZMODEM, the protocol behind --put/--get: zm_transfer() */
#include "zmodem.c"

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
        "                      prompt, in the order given; repeatable. The proxy exits\n"
        "                      by itself once every queued command has run. Such a\n"
        "                      run needs no terminal on stdin: without one it goes\n"
        "                      unattended and never reads stdin; ^C then interrupts\n"
        "                      the remote command too and leaves at its prompt.\n"
        "                      Needs a sentinel pattern (a -p with an empty reply)\n"
        "      --put FILE      queue an upload: \"rz\" is typed at the prompt and the\n"
        "                      local FILE is sent to it over ZMODEM, into the remote\n"
        "                      shell's current directory (an existing file there is\n"
        "                      replaced). Repeatable, queued with --send in order\n"
        "      --get FILE      queue a download: \"sz FILE\" is typed at the prompt and\n"
        "                      the file it sends is saved in the local current\n"
        "                      directory, under FILE's last component -- replacing\n"
        "                      it only once all of it arrived. Repeatable. During a\n"
        "                      transfer the keyboard only knows the escape key\n"
        "                      (leave) and ^C (cancel); unattended, ^C cancels.\n"
        "                      A failed transfer ends the run with exit status 1.\n"
        "                      Both need a sentinel pattern and lrzsz on the remote\n"
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
        "  -w, --wait-for TEXT on a serial channel, type the startup Enter only once\n"
        "                      the command has printed TEXT (anywhere in its output),\n"
        "                      100ms have passed since, and the pty is raw, e.g.\n"
        "                      \"Escape character is ^]\" for virsh console\n"
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
        "  %s -p \"]# \" --term serial -- virsh console vm1\n"
        "  %s -p \"]# \" -w \"Escape character is ^]\" -- virsh console vm1\n"
        "  %s -p \"]# \" --put app.tgz --get /var/log/messages -- \\\n"
        "      virsh console vm1                     # file in, file out\n",
        prog, prog, prog, prog, prog, prog, prog, prog);
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
 * Relay output for stdout. On the terminal the bytes go out verbatim:
 * the tty's own output flags -- which the proxy never touches -- do
 * the display processing, and a relayed "\r\n" merely gains a second,
 * invisible "\r" through ONLCR. A pipe or a file has no line
 * discipline out there to hand the carriage returns to: downstream
 * tools split on "\n" and a "\r" left at the end of the line poisons
 * their line ends (grep's $ anchor, awk's last field). Translate
 * "\r\n" into "\n" there.
 *
 * A "\r" is only recognized as the first half of a "\r\n" once the
 * byte after it is known, and a chunk read from the pty may end right
 * after one: hold such a trailing "\r" back and decide on the next
 * write; flush_held_cr() emits it at the end of the stream if no
 * "\n" ever follows.
 */
static int stdout_lf;  /* stdout is not a terminal: "\r\n" leaves as "\n" */
static int cr_held;    /* a "\r" is held back, waiting for the next byte */

static void write_stdout(const unsigned char *buf, size_t len)
{
    size_t i, start;

    if (!stdout_lf) {
        write_all(STDOUT_FILENO, buf, len);
        return;
    }
    if (cr_held && (len == 0 || buf[0] != '\n')) {
        /* the held "\r" was an ordinary carriage return after all */
        write_all(STDOUT_FILENO, (const unsigned char *)"\r", 1);
    }
    cr_held = 0;
    start = 0;
    for (i = 0; i < len; i++) {
        if (buf[i] != '\r')
            continue;
        if (i + 1 == len) {
            /* the chunk ends on the "\r": hold it for the next write */
            write_all(STDOUT_FILENO, buf + start, i - start);
            cr_held = 1;
            return;
        }
        if (buf[i + 1] == '\n') {
            /* the "\r" of a "\r\n": emit what precedes it, skip it */
            write_all(STDOUT_FILENO, buf + start, i - start);
            start = i + 1;
        }
        /* else: a "\r" the channel means literally -- keep it */
    }
    write_all(STDOUT_FILENO, buf + start, len - start);
}

/*
 * The stream is over while a "\r" is still held: nothing will follow
 * that could make it the first half of a "\r\n", so it was a literal
 * carriage return -- write it, or the byte would be lost.
 */
static void flush_held_cr(void)
{
    if (!cr_held)
        return;
    cr_held = 0;
    write_all(STDOUT_FILENO, (const unsigned char *)"\r", 1);
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
    if (term_serial && local_fd >= 0 && t != NULL && *t != '\0') {
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
     * happen. With no local terminal there is nothing to push, now or
     * later (and a pending push typing nothing would cost the prompt
     * its one line).
     */
    winch_pending = term_serial && local_fd >= 0;
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

    if (ioctl(local_fd, TIOCGWINSZ, &ws) < 0)
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

    if (ioctl(local_fd, TIOCGWINSZ, &ws) == 0) {
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
            if (zm_job) {
                VLOG("  back at the prompt: no %s to talk to",
                     zm_job->xfer == ZM_PUT ? "rz" : "sz");
                zm_prompt = 1;
            } else if (intr_sent) {
                VLOG("  back at the prompt after ^C; exiting");
                quit_pending = 1;
            } else if (winch_pending) {
                winch_pending = 0;
                sync_winsize(pty_fd);
            } else if (cmd) {
                cmd->used = 1;
                nr_unused--;
                VLOG("  typing command '%s' + Enter", esc_str(cmd->response));
                write_all(pty_fd, (const unsigned char *)cmd->response,
                          strlen(cmd->response));
                write_all(pty_fd, (const unsigned char *)"\r", 1);
                if (cmd->xfer)
                    zm_job = cmd; /* the main loop runs the transfer */
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
 * What the remote end prints around a transfer that is not ZMODEM: the
 * echoed command, "rz waiting to receive.", an error, before rz/sz
 * speaks -- shown as usual -- and what comes in after a cancel, kept
 * off the screen (show 0). Either way it is checked for the prompt,
 * which tells the transfer rz/sz is not running (any more).
 */
static int zm_text(int fd, const unsigned char *buf, size_t n, int show)
{
    if (show)
        write_stdout(buf, n);
    record_and_check(fd, buf, n);
    return zm_prompt;
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
 * window size and the same termios settings -- except OPOST, cleared
 * on the output side (see below) -- so the command -- and the remote
 * pty it reaches -- sees the terminal the user is sitting at from the
 * very first byte. "ssh -tt" for instance relays that window size to
 * the remote pty right away, and the local line settings are what the
 * command would have seen without a proxy.
 */
static int open_command_pty(char **cmd, pid_t *pidp)
{
    int master, slave;
    struct termios tio, *tiop = NULL;
    struct winsize ws, *wsp = NULL;
    pid_t pid;

    if (tcgetattr(local_fd, &tio) == 0) {
        tiop = &tio;
        /*
         * The copy keeps the input side: the line settings of the
         * terminal the user types on, so a cooked command gets line
         * editing and echo like on a real terminal. The output side
         * loses OPOST: the channel carries bytes that are already
         * display-ready (a serial console sends "\r\n"; a cooked
         * command's "\n" is completed by the user's own terminal
         * below), and the one line discipline that should touch them
         * is that terminal -- not this in-between pty, which would
         * only add a second "\r" to every newline.
         */
        tio.c_oflag &= ~OPOST;
    }
    if (ioctl(local_fd, TIOCGWINSZ, &ws) == 0)
        wsp = &ws;

    if (openpty(&master, &slave, NULL, tiop, wsp) < 0)
        die_errno("openpty");

    /*
     * No local terminal to copy (an unattended run, stdout not a
     * terminal either): the pty keeps the driver defaults, but loses
     * OPOST all the same -- the bytes are display-ready already.
     */
    if (!tiop && tcgetattr(slave, &tio) == 0) {
        tio.c_oflag &= ~OPOST;
        tcsetattr(slave, TCSANOW, &tio);
    }

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
 * Feed a chunk of startup output to the -w search; returns 1 once the
 * text has been seen. The text may be split across reads, so the last
 * wait_len - 1 bytes of the output so far are kept and searched
 * together with the next chunk.
 */
static int wait_text_check(const unsigned char *buf, size_t n)
{
    static unsigned char carry[WAIT_MAX];
    static size_t carry_len;
    unsigned char win[WAIT_MAX + 4096];

    while (n > 0) {
        size_t take = sizeof(win) - carry_len;
        size_t len;

        if (take > n)
            take = n;
        memcpy(win, carry, carry_len);
        memcpy(win + carry_len, buf, take);
        len = carry_len + take;
        buf += take;
        n -= take;
        if (memmem(win, len, wait_text, wait_len))
            return 1;
        carry_len = len < wait_len - 1 ? len : wait_len - 1;
        memcpy(carry, win + len - carry_len, carry_len);
    }
    return 0;
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
 *    connected, and must wait.) With -w the switch alone is not
 *    enough: the Enter also waits until the -w text (e.g. virsh's
 *    "Escape character is ^]") has shown up in the output -- before
 *    or after the switch, whichever order the command prints in --
 *    and then WAIT_SETTLE_MS more, counted from the text. The
 *    output is read, printed and recorded the whole time, raw or not,
 *    exactly as in 1. After the Enter the pty is polled:
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
    int raw = 0;
    int seen = wait_text == NULL; /* no -w: the raw switch alone decides */
    struct timespec seen_at;

    for (;;) {
        long settle = 0; /* ms still to wait after the -w text */

        raw = pty_is_raw(pty_fd);
        if (seen && wait_text)
            settle = WAIT_SETTLE_MS - ms_since(&seen_at);
        if (raw && seen && settle <= 0)
            break;
        /* wake every 100ms to notice the switch, sooner to end the settle */
        ts.tv_nsec = (settle > 0 && settle < 100 ? settle : 100) * 1000000L;
        if (ppoll(&pfd, 1, &ts, NULL) > 0 &&
            (pfd.revents & (POLLIN | POLLERR | POLLHUP))) {
            n = read(pty_fd, buf, sizeof(buf));
            if (n <= 0) {
                VLOG("startup: command gone");
                return; /* command gone: let the main loop see it too */
            }
            VLOG("startup: %zu bytes, pty %s", (size_t)n,
                 raw ? "raw, waiting for the -w text" : "not raw yet");
            write_stdout(buf, (size_t)n);
            record_and_check(pty_fd, buf, (size_t)n);
            if (quit_pending)
                return; /* every command ran during the startup wait */
            if (zm_job)
                return; /* a transfer was typed: the main loop runs it */
            if (!seen && wait_text_check(buf, (size_t)n)) {
                seen = 1;
                clock_gettime(CLOCK_MONOTONIC, &seen_at);
                VLOG("startup: saw '%s'; Enter in %d ms at the earliest",
                     esc_str(wait_text), WAIT_SETTLE_MS);
            }
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

/*
 * The line --get types: sz with the remote path in single quotes, so
 * the remote shell takes it verbatim -- a quote in it closes the
 * quoting, adds an escaped one, and opens it again.
 */
static char *sz_command(const char *path)
{
    char *cmd = malloc(strlen(path) * 4 + 16), *q;

    if (!cmd)
        die("out of memory");
    q = cmd + sprintf(cmd, "sz -- '");
    for (; *path; path++) {
        if (*path == '\'') {
            memcpy(q, "'\\''", 4);
            q += 4;
        } else {
            *q++ = *path;
        }
    }
    strcpy(q, "'");
    return cmd;
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
    const char *first_send = NULL;  /* --send/--put/--get, for the refusal */
    char name[4];

    static const struct option long_options[] = {
        { "escape",  required_argument, NULL, 'e' },
        { "pattern", required_argument, NULL, 'p' },
        { "send",    required_argument, NULL, 's' },
        { "term",    required_argument, NULL, 't' },
        { "wait-for", required_argument, NULL, 'w' },
        { "verbose", no_argument,       NULL, 'v' },
        { "pty",     required_argument, NULL, 1 },
        { "put",     required_argument, NULL, 2 },
        { "get",     required_argument, NULL, 3 },
        { "help",    no_argument,       NULL, 'h' },
        { NULL, 0, NULL, 0 },
    };

    /*
     * The leading '+' stops at the first non-option argument: the
     * channel command's own options (e.g. "ssh -tt host") belong to
     * the command, not to us.
     */
    while ((opt = getopt_long(argc, argv, "+e:p:s:t:w:vh", long_options, NULL)) != -1) {
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
            if (!first_send)
                first_send = "send";
            break;
        }
        case 2:   /* --put */
        case 3: { /* --get */
            struct pattern *p;
            const char *base = strrchr(optarg, '/');

            /*
             * A command in the queue like --send, typed at its turn:
             * rz to receive what is put, sz to send what is got. The
             * file goes by its last component -- into the remote
             * shell's current directory, or the local one.
             */
            base = base ? base + 1 : optarg;
            if (!*base || !strcmp(base, ".") || !strcmp(base, "..")) {
                fprintf(stderr, "%s: --%s needs a file, not '%s'\n",
                        prog, opt == 2 ? "put" : "get", optarg);
                return 2;
            }
            if (opt == 2 && access(optarg, R_OK) < 0) {
                fprintf(stderr, "%s: %s: %s\n", prog, optarg, strerror(errno));
                return 2;
            }
            if (nr_patterns >= MAX_PATTERNS)
                die("too many patterns");
            p = &patterns[nr_patterns++];
            p->match = NULL;
            p->mlen = 0;
            p->response = opt == 2 ? "rz" : sz_command(optarg);
            p->sentinel = 0;
            p->command = 1;
            p->xfer = opt == 2 ? ZM_PUT : ZM_GET;
            p->path = optarg;
            p->used = 0;
            nr_unused++;
            has_commands = 1;
            nr_sends++;
            if (!first_send)
                first_send = opt == 2 ? "put" : "get";
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
        case 'w':
            wait_len = strlen(optarg);
            if (wait_len == 0 || wait_len > WAIT_MAX) {
                fprintf(stderr,
                        "%s: --wait-for text must be 1 to %d bytes\n",
                        prog, WAIT_MAX);
                return 2;
            }
            wait_text = optarg;
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
     * --send types at the sentinel's prompt, and so do --put/--get:
     * without a sentinel there is no prompt boundary to wait for, and
     * the command would be typed blind. (The serial method refuses the
     * same way, for the same reason.)
     */
    if (nr_sends && !has_sentinel) {
        fprintf(stderr,
                "%s: --%s needs a sentinel pattern (e.g. -p \"]# \")\n",
                prog, first_send);
        return 2;
    }

    /*
     * Classify the commands: a non-sentinel pattern whose match equals
     * the sentinel's. Its -p argument starts with the sentinel's
     * ("ls" after "]# "), so the split at the first space leaves both
     * with the same match -- the prompt -- and the reply is what to
     * run there. The sentinel types them one per prompt, in -p order,
     * and exits the proxy once they all ran (see the sentinel comment
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
            else if (p->xfer)
                VLOG("pattern[%d] -> %s '%s', command '%s'", i,
                     p->xfer == ZM_PUT ? "put" : "get", esc_str(p->path),
                     esc_str(p->response));
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

    /*
     * stdin must be the controlling terminal: the proxy is an
     * interactive attach console. A run with commands queued is the
     * exception -- it exits by itself once they ran, nobody needs to
     * type into it, so it goes ahead unattended (see local_fd). Decided
     * before the terminal type: what gets pushed depends on it.
     */
    if (!isatty(STDIN_FILENO) || tcgetsid(STDIN_FILENO) != getsid(0)) {
        if (!has_commands) {
            if (!isatty(STDIN_FILENO))
                die("stdin is not a terminal");
            die("stdin is not the controlling terminal of this process");
        }
        unattended = 1;
        local_fd = isatty(STDOUT_FILENO) ? STDOUT_FILENO : -1;
        VLOG("stdin is not the controlling terminal: running unattended%s",
             local_fd < 0 ? ", no local terminal" : ", stdout is the terminal");
    }

    /* resolve the terminal type now that the command (if any) is known */
    resolve_term_mode(pty_path, argv + optind, argc - optind);

    /*
     * -w only gates the serial startup Enter: anywhere else there is no
     * startup phase for it to hold back, so say it goes unused.
     */
    if (wait_text && (pty_path || !term_serial))
        fprintf(stderr,
                "%s: --wait-for ignored: not a serial channel COMMAND\n",
                prog);
    else if (wait_text)
        VLOG("startup Enter waits for '%s'", esc_str(wait_text));

    escape_char = parse_escape(escape_str);

    /*
     * A piped or redirected stdout has no line discipline to hand a
     * "\r\n" to: translate it to "\n" on the way out (write_stdout),
     * so line-oriented tools downstream get clean line ends. On the
     * terminal itself the bytes go out verbatim -- its own output
     * flags, which the proxy never touches, render them.
     */
    stdout_lf = !isatty(STDOUT_FILENO);

    /*
     * Our own message first -- the command output follows below. In
     * COMMAND mode announce the command being run, so the escape hint
     * the command itself may print later (e.g. virsh console prints
     * "Escape character is ^]" for the virtual machine console) is
     * not mistaken for ours. Unattended there is no escape key to
     * announce.
     */
    escape_name(escape_char, name, sizeof(name));
    if (pty_path) {
        if (!unattended)
            printf("Escape character is %s\n", name);
    } else {
        printf("Running: ");
        for (int i = optind; i < argc; i++)
            printf("%s%s", i > optind ? " " : "", argv[i]);
        printf("\n");
        if (!unattended)
            printf("Escape character is %s (exits %s)\n", name, prog);
    }
    fflush(stdout);

    /*
     * Acquire the pty to proxy. With --pty it is an existing
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

    /*
     * save the original local terminal settings, for restore on exit
     * -- unattended, stdin is left alone altogether: nothing to save
     */
    if (!unattended) {
        tty_fd = STDIN_FILENO;
        if (tcgetattr(tty_fd, &saved_tio) < 0)
            die_errno("tcgetattr");
        tio_saved = 1;
    }

    /*
     * Block SIGWINCH for the whole session and let ppoll unblock it
     * only while waiting: the signal is then delivered inside ppoll,
     * which returns EINTR -- no missed-wakeup race around the wait.
     * Unattended, SIGINT goes the same way: its ^C is passed on by the
     * loop (see intr_count).
     */
    sigemptyset(&winch_set);
    sigaddset(&winch_set, SIGWINCH);
    if (unattended)
        sigaddset(&winch_set, SIGINT);
    sigprocmask(SIG_BLOCK, &winch_set, &orig_set);

    /* restore the terminal when killed by a signal */
    setup_signals();

    /*
     * now switch the local terminal to raw -- on the input side only:
     * cfmakeraw would clear OPOST too, and the output flags stay
     * exactly as the user had them. The relayed bytes are already
     * display-ready (the channel pty is created with OPOST off, and a
     * serial console sends "\r\n" by itself), so ONLCR can only add a
     * "\r" that repeats a return the line already made. And with the
     * flags untouched, every other writer on this terminal -- od at
     * the end of a pipe, the shell after us -- keeps rendering its
     * own "\n" the way the user configured it. Unattended there is no
     * keyboard to take raw.
     */
    if (!unattended) {
        raw = saved_tio;
        cfmakeraw(&raw);
        raw.c_oflag = saved_tio.c_oflag;
        raw.c_cc[VMIN] = 1;
        raw.c_cc[VTIME] = 0;
        if (tcsetattr(tty_fd, TCSANOW, &raw) < 0)
            die_errno("tcsetattr");
    }

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

    /* main loop: forward both directions (unattended: the pty only) */
    for (;;) {
        struct pollfd pfd[2] = {
            { .fd = unattended ? -1 : STDIN_FILENO, .events = POLLIN },
            { .fd = pty_fd,       .events = POLLIN },
        };
        unsigned char buf[4096];
        ssize_t n;
        struct timespec intr_left, *wait = NULL;

        /*
         * A transfer was just typed at the prompt: the pty is the
         * transfer's until it is over (zmodem.c), the keyboard only
         * good for the escape key and ^C. What came in behind a good
         * session is the remote shell's again, its prompt included.
         * A failed one ends the run here: the remote rz/sz has been
         * cancelled already (zm_abort), so a SIGINT has nothing left
         * to pass on -- the proxy leaves at once, and dies of it.
         */
        if (zm_job) {
            struct zm_link link = {
                .fd = pty_fd,
                .key_fd = unattended ? -1 : STDIN_FILENO,
                .escape = escape_char,
                .mask = &orig_set,
                .intr = &intr_count,
                .text = zm_text,
            };
            const unsigned char *rest;
            size_t nrest;
            int r = zm_transfer(&link, zm_job->xfer, zm_job->path);

            zm_job = NULL;
            zm_prompt = 0;
            if (r != ZM_OK) {
                xfer_failed = 1;
                goto out;
            }
            nrest = zm_leftover(&rest);
            if (nrest) {
                write_stdout(rest, nrest);
                record_and_check(pty_fd, rest, nrest);
                if (quit_pending)
                    goto out;
            }
            continue;
        }

        /*
         * ^C in an unattended run: type it into the pty for the remote
         * job, then give the prompt INTR_WAIT_SEC to come back -- the
         * sentinel leaves when it does (record_and_check) -- and leave
         * without it once the time is up.
         */
        if (intr_count && !intr_sent) {
            intr_sent = 1;
            clock_gettime(CLOCK_MONOTONIC, &intr_at);
            VLOG("^C: typing it into the pty, leaving at the next prompt");
            write_all(pty_fd, (const unsigned char *)"\x03", 1);
        }
        if (intr_sent) {
            long left = INTR_WAIT_SEC * 1000L - ms_since(&intr_at);

            if (left <= 0) {
                VLOG("no prompt %d s after ^C; exiting", INTR_WAIT_SEC);
                goto out;
            }
            intr_left.tv_sec = left / 1000;
            intr_left.tv_nsec = left % 1000 * 1000000L;
            wait = &intr_left;
        }
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
         * it, and the blocked mask is back on return. The same holds
         * for an unattended run's SIGINT.
         */
        if (ppoll(pfd, 2, wait, &orig_set) < 0) {
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
            write_stdout(buf, (size_t)n);
            record_and_check(pty_fd, buf, (size_t)n);
            if (quit_pending)
                goto out; /* every command ran: leave like the escape key */
        }
    }

out:
    /*
     * A "\r" the stream ended on is still held: write it, then move to
     * a fresh line -- the remote prompt we were sitting on has no \n.
     * Piped, the detach newline is translated with everything else.
     */
    flush_held_cr();
    write_stdout((const unsigned char *)"\n", 1);
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

    /* interrupted (^C, unattended): die of it, for the caller to see */
    if (intr_count) {
        signal(SIGINT, SIG_DFL);
        sigprocmask(SIG_SETMASK, &orig_set, NULL);
        raise(SIGINT);
    }
    return xfer_failed ? 1 : 0;
}
