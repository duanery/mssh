/*
 * zmodem.c -- the ZMODEM end of a pty-proxy file transfer
 *
 * Not a unit of its own: pty-proxy.c includes it, after its helpers
 * (prog, VLOG(), esc_bytes()), and drives one transfer at a time with
 * zm_transfer(). The remote end is lrzsz's rz or sz, started at the
 * prompt like any other queued command; this file plays the other
 * side over the proxied pty:
 *
 *   --put FILE   remote "rz" receives, zm_send() sends FILE
 *   --get FILE   remote "sz FILE" sends, zm_recv() receives it
 *
 * The protocol is Chuck Forsberg's ZMODEM as lrzsz speaks it: hex
 * headers for the control frames, binary headers (CRC-32 when the
 * receiver offers it) in front of data, 1024-byte data subpackets.
 * Only what one file in one direction needs is implemented -- no
 * ZCOMMAND, no compression, no encryption, no resume.
 *
 * The channel is not assumed to be 8-bit transparent towards the
 * remote end: what the proxy sends goes through the channel command
 * first, and "virsh console" leaves at ^], "ssh -tt" at "~." after a
 * newline. Beyond the bytes ZMODEM always escapes (ZDLE, XON, XOFF,
 * DLE, CR after '@'), the escape key of virsh and every CR and LF are
 * ZDLE-escaped too -- any receiver decodes an escaped byte, whether it
 * asked for it or not.
 */

#define ZPAD    '*'
#define ZDLE    0x18    /* ZMODEM's escape, the same byte as CAN */
#define ZBIN    'A'     /* binary header, CRC-16 */
#define ZHEX    'B'     /* hex header, CRC-16 */
#define ZBIN32  'C'     /* binary header, CRC-32 */
#define XON     0x11
#define XOFF    0x13

/* frame types */
#define ZRQINIT     0
#define ZRINIT      1
#define ZSINIT      2
#define ZACK        3
#define ZFILE       4
#define ZSKIP       5
#define ZNAK        6
#define ZABORT      7
#define ZFIN        8
#define ZRPOS       9
#define ZDATA       10
#define ZEOF        11
#define ZFERR       12
#define ZCRC        13
#define ZCHALLENGE  14
#define ZCOMPL      15
#define ZCAN        16
#define ZFREECNT    17
#define ZCOMMAND    18
#define ZSTDERR     19

/* data subpacket ends, sent as ZDLE + the letter */
#define ZCRCE   'h'     /* end of frame, header follows */
#define ZCRCG   'i'     /* frame goes on, no answer wanted */
#define ZCRCQ   'j'     /* frame goes on, ZACK wanted */
#define ZCRCW   'k'     /* end of frame, ZACK wanted */
#define ZRUB0   'l'     /* escaped 0x7f */
#define ZRUB1   'm'     /* escaped 0xff */

/* ZRINIT flags, in ZF0 */
#define CANFDX  0x01    /* full duplex */
#define CANOVIO 0x02    /* can receive during disk I/O */
#define CANFC32 0x20    /* CRC-32 */
#define ESCCTL  0x40    /* every control character must be escaped */

/* ZFILE options: binary file, replace what exists (scp does as much) */
#define ZCBIN   1
#define ZMCLOB  4

/* the header bytes: ZP0..ZP3 carry a position, ZF0 is the last one */
#define ZF0 3
#define ZF1 2

#define ZM_SUBPKT       1024            /* data subpacket we send */
#define ZM_MAXPKT       8192            /* largest one we take (sz -8) */
#define ZM_ACK_EVERY    8192            /* a ZCRCQ this often, for a ZACK */
#define ZM_WINDOW       (512L << 10)    /* unacked data, at most: zm_send() */
#define ZM_WINDOW_MIN   32768           /* ... after a ZRPOS, growing back */
#define ZM_START_SEC    30              /* for rz/sz to come up */
#define ZM_TIMEOUT_SEC  10              /* for each answer once it runs */
#define ZM_RETRIES      10
#define ZM_ESC_WAIT_MS  2000            /* for the prompt after a cancel,
                                           when the escape key leaves */
#define ZM_PROGRESS_MS  500

/* zm_transfer() results */
#define ZM_OK       0
#define ZM_FAILED   1   /* refused, broken off by the remote, timed out */
#define ZM_CANCEL   2   /* ^C, typed (raw) or as SIGINT (unattended) */
#define ZM_ESCAPE   3   /* the escape key: the proxy leaves */

/* directions */
#define ZM_PUT  1
#define ZM_GET  2

/* what the proxy lends the transfer */
struct zm_link {
    int fd;                         /* the pty rz/sz talks on */
    int key_fd;                     /* keyboard; -1: none (unattended) */
    unsigned char escape;           /* the escape key on key_fd */
    const sigset_t *mask;           /* signal mask while waiting */
    volatile sig_atomic_t *intr;    /* SIGINTs received */
    /*
     * Output that is not ZMODEM: what the remote prints before rz/sz
     * speaks ("rz waiting to receive.", "command not found") -- shown
     * -- and what comes in after a cancel -- not shown: it may be a
     * stream of data still in flight. Returns nonzero once the
     * sentinel has seen the prompt in it: rz/sz is over, the remote
     * shell is back.
     */
    int (*text)(int fd, const unsigned char *buf, size_t n, int show);
};

/* internal results, all negative: bytes are 0..255, frame ends 0x100|c */
#define ZE_TIMEOUT  (-1)
#define ZE_GONE     (-2)    /* the pty is closed: the channel is gone */
#define ZE_CANCEL   (-3)    /* ^C */
#define ZE_ESCAPE   (-4)    /* the escape key */
#define ZE_RCAN     (-5)    /* the remote broke off: five CANs */
#define ZE_BAD      (-6)    /* CRC or framing error */
#define ZE_NOSTART  (-7)    /* the prompt came back before rz/sz spoke */
#define ZE_LONG     (-8)    /* subpacket longer than ZM_MAXPKT */
#define ZE_SHELL    (-10)   /* the prompt, amid a put: rz is gone */
#define GOTOR       0x100

static struct zm {
    const struct zm_link *l;
    unsigned char ibuf[16384];
    size_t ipos, ilen;
    unsigned char obuf[16384];
    size_t olen;
    int session;            /* a header came in: rz/sz is speaking */
    unsigned char tbuf[4096]; /* text before the session, not shown yet */
    size_t tlen;
    size_t tkeep;           /* ... its last tkeep bytes may start a header */
    int rx32;               /* the last header read was CRC-32 */
    int tx32;               /* send CRC-32 */
    int escctl;             /* escape every control character */
    int watch;              /* data goes out: see zm_stray() */
    const char *what;       /* "put NAME" / "get NAME", for messages */
    off_t total;            /* file size, -1: unknown */
    off_t done;             /* bytes through so far */
    struct timespec t0, tshown;
    int shown;              /* a progress line is on stderr */
} zm;

/* table-driven: a fast line takes tens of MB/s through these */
static uint16_t zm_crc16tab[256];
static uint32_t zm_crc32tab[256];

static void zm_crc_init(void)
{
    for (unsigned n = 0; n < 256; n++) {
        uint16_t c16 = (uint16_t)(n << 8);
        uint32_t c32 = n;

        for (int i = 0; i < 8; i++) {
            c16 = c16 & 0x8000 ? (uint16_t)((c16 << 1) ^ 0x1021) : (uint16_t)(c16 << 1);
            c32 = c32 & 1 ? (c32 >> 1) ^ 0xedb88320 : c32 >> 1;
        }
        zm_crc16tab[n] = c16;
        zm_crc32tab[n] = c32;
    }
}

static uint16_t zm_crc16(uint16_t crc, unsigned char c)
{
    return (uint16_t)(crc << 8) ^ zm_crc16tab[(crc >> 8) ^ c];
}

static uint32_t zm_crc32(uint32_t crc, unsigned char c)
{
    return (crc >> 8) ^ zm_crc32tab[(crc ^ c) & 0xff];
}

/* a failure the user has to hear of, -v or not */
static void zm_err(const char *fmt, ...)
{
    va_list ap;

    if (zm.shown) {
        fputc('\n', stderr);
        zm.shown = 0;
    }
    fprintf(stderr, "%s: %s: ", prog, zm.what);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
}

/*
 * The proxy's keyboard during a transfer: nothing typed can go into
 * the ZMODEM stream, so keys are dropped -- except the escape key,
 * which leaves, and ^C, which cancels. Either one sticks: every wait
 * from here on returns it, so it cannot get lost between two reads.
 */
static int zm_keyev;
static int zm_draining;     /* after a cancel: only the escape key counts */

static int zm_keys(void)
{
    unsigned char k[256];
    ssize_t n = read(zm.l->key_fd, k, sizeof(k));

    if (n <= 0) {
        if (n < 0 && (errno == EINTR || errno == EAGAIN))
            return 0;
        return zm_keyev = ZE_ESCAPE; /* the keyboard is gone: leave */
    }
    for (ssize_t i = 0; i < n; i++) {
        if (k[i] == zm.l->escape)
            return zm_keyev = ZE_ESCAPE;
        if (k[i] == 0x03 && !zm_draining)
            zm_keyev = ZE_CANCEL;
    }
    if (!zm_keyev)
        VLOG("zmodem: %zd typed byte(s) dropped", n);
    return zm_keyev;
}

/*
 * Wait up to ms (0: just look) for the pty to be ready for events.
 * Returns 0 when it is, else ZE_TIMEOUT, or what the keyboard or a
 * SIGINT says. ppoll() runs under the proxy's wait mask, so SIGINT
 * and SIGWINCH are delivered in here.
 */
static int zm_wait(short events, long ms)
{
    struct timespec t0;

    clock_gettime(CLOCK_MONOTONIC, &t0);
    for (;;) {
        struct pollfd pfd[2] = {
            { .fd = zm.l->fd,     .events = events },
            { .fd = zm.l->key_fd, .events = POLLIN },
        };
        long left = ms - ms_since(&t0);
        struct timespec ts;

        if (zm_keyev && (zm_keyev == ZE_ESCAPE || !zm_draining))
            return zm_keyev;
        if (*zm.l->intr && !zm_draining)
            return ZE_CANCEL;
        if (left < 0)
            left = 0;
        ts.tv_sec = left / 1000;
        ts.tv_nsec = left % 1000 * 1000000L;
        if (ppoll(pfd, 2, &ts, zm.l->mask) < 0) {
            if (errno == EINTR)
                continue;
            return ZE_GONE;
        }
        if (pfd[1].revents & (POLLIN | POLLHUP | POLLERR))
            zm_keys();
        if (zm_keyev && (zm_keyev == ZE_ESCAPE || !zm_draining))
            return zm_keyev;
        if (pfd[0].revents)
            return 0;   /* POLLHUP/POLLERR too: the read or write tells */
        if (left == 0)
            return ZE_TIMEOUT;
    }
}

/*
 * Write out what is buffered, waiting up to ms for room each time; the
 * pty is non-blocking for the transfer.
 */
static int zm_flush_ms(long ms)
{
    struct timespec t0;
    size_t off = 0;

    clock_gettime(CLOCK_MONOTONIC, &t0);

    while (off < zm.olen) {
        ssize_t w;
        int r = zm_wait(POLLOUT, ms - ms_since(&t0));

        if (r < 0) {
            zm.olen = 0;
            return r;
        }
        w = write(zm.l->fd, zm.obuf + off, zm.olen - off);
        if (w < 0) {
            if (errno == EINTR || errno == EAGAIN)
                continue;
            zm.olen = 0;
            return ZE_GONE;
        }
        off += (size_t)w;
    }
    zm.olen = 0;
    return 0;
}

static int zm_flush(void)
{
    return zm_flush_ms(ZM_TIMEOUT_SEC * 1000L);
}

/*
 * Show the text that came before the session, keeping back its last
 * tkeep bytes: they may be the start of the first header. Returns
 * nonzero once the prompt has been seen in it.
 */
static int zm_text_flush(void)
{
    size_t n = zm.tlen - zm.tkeep;
    int prompt = 0;

    if (n > 0) {
        prompt = zm.l->text(zm.l->fd, zm.tbuf, n, 1);
        memmove(zm.tbuf, zm.tbuf + n, zm.tkeep);
        zm.tlen = zm.tkeep;
    }
    return prompt;
}

/* refill the input buffer, waiting up to ms */
static int zm_fill(long ms)
{
    for (;;) {
        ssize_t n;
        int r = zm_wait(POLLIN, ms);

        if (r < 0)
            return r;
        n = read(zm.l->fd, zm.ibuf, sizeof(zm.ibuf));
        if (n < 0 && (errno == EINTR || errno == EAGAIN))
            continue;
        if (n <= 0)
            return ZE_GONE;
        zm.ipos = 0;
        zm.ilen = (size_t)n;
        return 0;
    }
}

/*
 * The next byte from the pty, waiting up to ms. Before it waits,
 * what was sent goes out -- the answer to it is what is waited for --
 * and before the session, the text read so far is shown.
 */
static int zm_rawc(long ms)
{
    int r;

    if (zm.ipos < zm.ilen)
        return zm.ibuf[zm.ipos++];
    if ((r = zm_flush()) < 0)
        return r;
    if (!zm.session && zm_text_flush())
        return ZE_NOSTART;
    if ((r = zm_fill(ms)) < 0)
        return r;
    return zm.ibuf[zm.ipos++];
}

/*
 * A byte that is not part of a frame, while a put sends its data:
 * nothing rz says outside of its headers, but the shell's prompt -- rz
 * gone, killed or given up, and what is sent going to the shell. The
 * sentinel looks at it, a byte at a time: the echo of the data follows
 * the prompt at once. Not while getting a file: there the bytes passed
 * over in a resync are sz's data -- which might hold a prompt -- and
 * nothing goes to the shell but a few headers.
 */
static int zm_stray(unsigned char c)
{
    return zm.watch && zm.l->text(zm.l->fd, &c, 1, 0);
}

/*
 * Has the receiver said something? Looked at between two subpackets,
 * without waiting: bytes that cannot start a header (the XON after a
 * hex header) are skipped, so only a header -- or CANs -- count.
 */
static int zm_pending(void)
{
    for (;;) {
        int r;

        for (; zm.ipos < zm.ilen; zm.ipos++) {
            unsigned char c = zm.ibuf[zm.ipos];

            if ((c & 0x7f) == ZPAD || c == ZDLE)
                return 1;
            if (zm_stray(c))
                return ZE_SHELL;
        }
        r = zm_fill(0);
        if (r == ZE_TIMEOUT)
            return 0;
        if (r < 0)
            return r;
    }
}

static void zm_out(unsigned char c)
{
    if (zm.olen == sizeof(zm.obuf))
        zm_flush(); /* a failure shows at the next wait for an answer */
    zm.obuf[zm.olen++] = c;
}

/*
 * One byte, ZDLE-escaped if it must be: what ZMODEM always escapes,
 * plus CR and LF ("~." after one leaves ssh -tt; CR also covers the
 * CR-after-@ rule) and ^] (virsh console's escape key), with or
 * without the high bit -- and every control character if the
 * receiver asked for ESCCTL.
 */
static void zm_putz(unsigned char c)
{
    switch (c & 0x7f) {
    case ZDLE: case 0x10: case XON: case XOFF:
    case '\r': case '\n': case 0x1d:
        break;
    default:
        if (!zm.escctl || (c & 0x60))
            goto plain;
    }
    zm_out(ZDLE);
    c ^= 0x40;
plain:
    zm_out(c);
}

static void zm_setpos(unsigned char h[4], uint32_t pos)
{
    h[0] = (unsigned char)pos;
    h[1] = (unsigned char)(pos >> 8);
    h[2] = (unsigned char)(pos >> 16);
    h[3] = (unsigned char)(pos >> 24);
}

static uint32_t zm_getpos(const unsigned char h[4])
{
    return h[0] | (uint32_t)h[1] << 8 | (uint32_t)h[2] << 16 |
           (uint32_t)h[3] << 24;
}

static const char *zm_name(int type)
{
    static const char *names[] = {
        "ZRQINIT", "ZRINIT", "ZSINIT", "ZACK", "ZFILE", "ZSKIP", "ZNAK",
        "ZABORT", "ZFIN", "ZRPOS", "ZDATA", "ZEOF", "ZFERR", "ZCRC",
        "ZCHALLENGE", "ZCOMPL", "ZCAN", "ZFREECNT", "ZCOMMAND", "ZSTDERR",
    };

    return type >= 0 && type <= ZSTDERR ? names[type] : "?";
}

/*
 * -v: a line per header, but for the stream of ZACKs. Positions go in
 * decimal, the flags of ZRINIT/ZSINIT/ZFILE as ZF0..ZF3.
 */
#define ZM_TRACE(dir, type, h) do {                                     \
    if (type == ZRINIT || type == ZSINIT || type == ZFILE)              \
        VLOG("zmodem: %s %s flags %02x %02x %02x %02x", dir,            \
             zm_name(type), h[ZF0], h[ZF1], h[1], h[0]);                \
    else if (type != ZACK)                                              \
        VLOG("zmodem: %s %s %u", dir, zm_name(type),                    \
             (unsigned)zm_getpos(h));                                   \
} while (0)

/* control frames go as hex headers: plain ASCII, any line carries them */
static void zm_hexhdr(int type, const unsigned char h[4])
{
    static const char hex[] = "0123456789abcdef";
    unsigned char b[5] = { (unsigned char)type, h[0], h[1], h[2], h[3] };
    uint16_t crc = 0;

    ZM_TRACE("->", type, h);
    zm_out(ZPAD);
    zm_out(ZPAD);
    zm_out(ZDLE);
    zm_out(ZHEX);
    for (int i = 0; i < 7; i++) {
        unsigned char c = i < 5 ? b[i] : i == 5 ? crc >> 8 : crc & 0xff;

        if (i < 5)
            crc = zm_crc16(crc, c);
        zm_out(hex[c >> 4]);
        zm_out(hex[c & 15]);
    }
    zm_out('\r');
    zm_out('\n' | 0x80);
    if (type != ZFIN && type != ZACK)
        zm_out(XON);
}

/* a binary header, the kind data follows: CRC-32 if the receiver can */
static void zm_binhdr(int type, const unsigned char h[4])
{
    unsigned char b[5] = { (unsigned char)type, h[0], h[1], h[2], h[3] };

    ZM_TRACE("->", type, h);
    zm_out(ZPAD);
    zm_out(ZDLE);
    if (zm.tx32) {
        uint32_t crc = 0xffffffff;

        zm_out(ZBIN32);
        for (int i = 0; i < 5; i++) {
            zm_putz(b[i]);
            crc = zm_crc32(crc, b[i]);
        }
        crc = ~crc;
        for (int i = 0; i < 4; i++, crc >>= 8)
            zm_putz(crc & 0xff);
    } else {
        uint16_t crc = 0;

        zm_out(ZBIN);
        for (int i = 0; i < 5; i++) {
            zm_putz(b[i]);
            crc = zm_crc16(crc, b[i]);
        }
        zm_putz(crc >> 8);
        zm_putz(crc & 0xff);
    }
}

/* a data subpacket, ended by end (ZCRCE/G/Q/W) */
static void zm_sdata(const unsigned char *buf, size_t n, int end)
{
    if (zm.tx32) {
        uint32_t crc = 0xffffffff;

        for (size_t i = 0; i < n; i++) {
            zm_putz(buf[i]);
            crc = zm_crc32(crc, buf[i]);
        }
        zm_out(ZDLE);
        zm_out((unsigned char)end);
        crc = ~zm_crc32(crc, (unsigned char)end);
        for (int i = 0; i < 4; i++, crc >>= 8)
            zm_putz(crc & 0xff);
    } else {
        uint16_t crc = 0;

        for (size_t i = 0; i < n; i++) {
            zm_putz(buf[i]);
            crc = zm_crc16(crc, buf[i]);
        }
        zm_out(ZDLE);
        zm_out((unsigned char)end);
        crc = zm_crc16(crc, (unsigned char)end);
        zm_putz(crc >> 8);
        zm_putz(crc & 0xff);
    }
    if (end == ZCRCW)
        zm_out(XON);
}

/*
 * One ZDLE-decoded byte: 0..255, or GOTOR|c for a subpacket end. A
 * raw XON/XOFF is line noise -- a sender escapes its own -- and is
 * skipped; five CANs in a row are the remote breaking off.
 */
static int zm_zdl(long ms)
{
    int c, ncan = 1;

    for (;;) {
        if ((c = zm_rawc(ms)) < 0)
            return c;
        if (c == ZDLE)
            break;
        if ((c & 0x7f) != XON && (c & 0x7f) != XOFF)
            return c;
    }
    for (;;) {
        if ((c = zm_rawc(ms)) < 0)
            return c;
        switch (c) {
        case ZDLE:
            if (++ncan >= 5)
                return ZE_RCAN;
            continue;
        case ZCRCE: case ZCRCG: case ZCRCQ: case ZCRCW:
            return GOTOR | c;
        case ZRUB0:
            return 0x7f;
        case ZRUB1:
            return 0xff;
        case XON: case XON | 0x80: case XOFF: case XOFF | 0x80:
            continue;
        }
        if ((c & 0x60) == 0x40)
            return c ^ 0x40;
        return ZE_BAD;
    }
}

static int zm_hexval(int c)
{
    c &= 0x7f;
    if (c >= '0' && c <= '9')
        return c - '0';
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    return -1;
}

/* the rest of a header, after ZPAD ZDLE and its format letter */
static int zm_hdrbody(int fmt, unsigned char h[4], long ms)
{
    unsigned char b[9];
    int n = fmt == ZBIN32 ? 9 : 7;
    int c;

    for (int i = 0; i < n; i++) {
        if (fmt == ZHEX) {
            int hi, lo;

            if ((c = zm_rawc(ms)) < 0 || (hi = zm_hexval(c)) < 0)
                return c < 0 ? c : ZE_BAD;
            if ((c = zm_rawc(ms)) < 0 || (lo = zm_hexval(c)) < 0)
                return c < 0 ? c : ZE_BAD;
            b[i] = (unsigned char)(hi << 4 | lo);
        } else {
            if ((c = zm_zdl(ms)) < 0)
                return c;
            if (c & GOTOR)
                return ZE_BAD;
            b[i] = (unsigned char)c;
        }
    }
    if (fmt == ZBIN32) {
        uint32_t crc = 0xffffffff;

        for (int i = 0; i < 5; i++)
            crc = zm_crc32(crc, b[i]);
        if (~crc != zm_getpos(b + 5))
            return ZE_BAD;
    } else {
        uint16_t crc = 0;

        for (int i = 0; i < 5; i++)
            crc = zm_crc16(crc, b[i]);
        if (crc != (b[5] << 8 | b[6]))
            return ZE_BAD;
    }
    if (fmt == ZHEX) {
        /* the CR LF a hex header ends with; the XON after it is noise */
        if (zm.ipos < zm.ilen && (zm.ibuf[zm.ipos] & 0x7f) == '\r')
            zm.ipos++;
        if (zm.ipos < zm.ilen && (zm.ibuf[zm.ipos] & 0x7f) == '\n')
            zm.ipos++;
    }
    zm.rx32 = fmt == ZBIN32;
    memcpy(h, b + 1, 4);
    ZM_TRACE("<-", b[0], h);
    return b[0];
}

/*
 * The next header: its frame type, the four header bytes in h. Up to
 * ms per byte. Whatever comes before it is skipped -- or, before the
 * session, shown: that is the remote shell talking, and its prompt
 * coming back there means rz/sz is not going to (ZE_NOSTART).
 */
static int zm_gethdr(unsigned char h[4], long ms)
{
    int st = 0, ncan = 0;

    for (;;) {
        int c, r;

        if (!zm.session && zm.tlen == sizeof(zm.tbuf) && zm_text_flush())
            return ZE_NOSTART;
        if ((c = zm_rawc(ms)) < 0)
            return c;
        if (zm_stray((unsigned char)c))
            return ZE_SHELL;
        if (!zm.session)
            zm.tbuf[zm.tlen++] = (unsigned char)c;
        ncan = c == ZDLE ? ncan + 1 : 0;
        if (ncan >= 5)
            return ZE_RCAN;
        if ((c & 0x7f) == ZPAD) {
            if (st != 1)
                zm.tkeep = 0;
            st = 1;
            if (zm.tkeep < 4)   /* a row of '*' further back is text */
                zm.tkeep++;
            continue;
        }
        if (st == 1 && c == ZDLE) {
            st = 2;
            zm.tkeep++;
            continue;
        }
        if (st == 2 && (c == ZBIN || c == ZHEX || c == ZBIN32)) {
            if (!zm.session) {
                /* the header is not text: show what precedes it */
                zm.tlen -= zm.tkeep + 1;
                zm.tkeep = 0;
                zm_text_flush();
            }
            zm.tkeep = 0;
            r = zm_hdrbody(c, h, ms);
            if (r >= 0) {
                zm.session = 1;
                return r;
            }
            if (r != ZE_BAD)
                return r;
            VLOG("zmodem: garbled header");
            if (zm.session)
                return ZE_BAD;
        }
        st = 0;
        zm.tkeep = 0;
    }
}

/*
 * A data subpacket into buf: returns how it ended (ZCRCE/G/Q/W), with
 * the length in *np, or ZE_BAD on a CRC error. Its CRC is of the kind
 * of the header in front of the frame.
 */
static int zm_rdata(unsigned char *buf, size_t max, size_t *np, long ms)
{
    uint32_t crc32 = 0xffffffff;
    uint16_t crc16 = 0;
    unsigned char t[4];
    size_t n = 0;
    int c, end;

    for (;;) {
        if ((c = zm_zdl(ms)) < 0)
            return c;
        if (c & GOTOR)
            break;
        if (n == max)
            return ZE_LONG;
        buf[n++] = (unsigned char)c;
        if (zm.rx32)
            crc32 = zm_crc32(crc32, (unsigned char)c);
        else
            crc16 = zm_crc16(crc16, (unsigned char)c);
    }
    end = c & 0xff;
    for (int i = 0; i < (zm.rx32 ? 4 : 2); i++) {
        if ((c = zm_zdl(ms)) < 0)
            return c;
        if (c & GOTOR)
            return ZE_BAD;
        t[i] = (unsigned char)c;
    }
    if (zm.rx32 ? ~zm_crc32(crc32, (unsigned char)end) != zm_getpos(t)
                : zm_crc16(crc16, (unsigned char)end) != (t[0] << 8 | t[1]))
        return ZE_BAD;
    *np = n;
    return end;
}

/*
 * Progress on stderr, if that is a terminal: one line, rewritten at
 * most every ZM_PROGRESS_MS. The summary at the end is printed
 * wherever stderr goes.
 */
static void zm_progress(off_t done, int final)
{
    double sec;

    zm.done = done;
    if (!final) {
        if (!isatty(STDERR_FILENO) || (zm.shown &&
            ms_since(&zm.tshown) < ZM_PROGRESS_MS))
            return;
        clock_gettime(CLOCK_MONOTONIC, &zm.tshown);
        fprintf(stderr, "%s%s: %s: %lld", zm.shown ? "\r" : "\r\n", prog,
                zm.what, (long long)done);
        if (zm.total > 0)
            fprintf(stderr, " of %lld bytes (%d%%)\033[K",
                    (long long)zm.total, (int)(done * 100 / zm.total));
        else
            fprintf(stderr, " bytes\033[K");
        zm.shown = 1;
        return;
    }
    sec = ms_since(&zm.t0) / 1000.0;
    fprintf(stderr, "%s%s: %s: %lld bytes in %.1f s (%.1f KiB/s)%s\n",
            zm.shown ? "\r" : isatty(STDERR_FILENO) ? "\r\n" : "",
            prog, zm.what, (long long)done, sec,
            sec > 0 ? done / 1024.0 / sec : 0.0,
            zm.shown ? "\033[K" : "");
    zm.shown = 0;
}

/*
 * Break the session off, and wait for it to be over. Unless the other
 * end broke off itself (cancel 0), the CANs every ZMODEM program stops
 * at go out first, whatever was still buffered dropped (the backspaces
 * after them wipe them off a line editor, should one be reading
 * instead, and a ^U the rest of the line: the data that reached the
 * shell if rz was gone).
 *
 * Then the wait for rz/sz to be gone -- the remote shell's prompt,
 * which the sentinel recognizes. Merely writing the CANs is not
 * enough: they have only reached the local pty, the channel command
 * still has to read them and pass them on, and if the proxy left now
 * -- closing the pty, hanging up the command -- they might never get
 * there. Over ssh that does no harm, the hangup ends rz/sz all the
 * same; over "virsh console" it does: nothing hangs up the guest's
 * serial line, and rz/sz would sit on it until its own timeouts gave
 * up, the console's shell out of reach meanwhile. Nor is anything that
 * comes back proof of the cancel having arrived -- it may have been on
 * its way long before -- the prompt is.
 *
 * What comes in meanwhile is swallowed -- data still in flight would
 * be binary garbage on the screen -- but checked for the prompt. The
 * wait is max_ms at most, the CANs' write included; only the escape
 * key cuts it short -- for the escape key's own cancel, a second
 * press: it returns ZE_ESCAPE, everything else 0.
 */
static int zm_abort(int cancel, long max_ms)
{
    static const unsigned char canit[] = {
        ZDLE, ZDLE, ZDLE, ZDLE, ZDLE, ZDLE, ZDLE, ZDLE,
        8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 0x15,
    };
    struct timespec t0;
    size_t total = 0;
    int r = 0, prompt = 0;

    clock_gettime(CLOCK_MONOTONIC, &t0);
    zm_keyev = 0;       /* the escape or ^C that got us here: acted on */
    zm_draining = 1;
    zm.olen = 0;
    if (cancel) {
        VLOG("zmodem: -> CAN x8");
        memcpy(zm.obuf, canit, sizeof(canit));
        zm.olen = sizeof(canit);
        r = zm_flush_ms(max_ms);
    }
    /* what is still buffered first: the prompt may be in there */
    if (zm.ipos < zm.ilen) {
        total += zm.ilen - zm.ipos;
        prompt = zm.l->text(zm.l->fd, zm.ibuf + zm.ipos, zm.ilen - zm.ipos, 0);
        zm.ipos = zm.ilen;
    }
    while (!prompt && r != ZE_ESCAPE && r != ZE_GONE) {
        if ((r = zm_fill(max_ms - ms_since(&t0))) < 0)
            break;
        total += zm.ilen;
        prompt = zm.l->text(zm.l->fd, zm.ibuf, zm.ilen, 0);
        zm.ipos = zm.ilen;
    }
    zm_draining = 0;
    VLOG("zmodem: %zu byte(s) drained after the cancel, %s", total,
         prompt ? "the prompt is back" :
         r == ZE_ESCAPE ? "escape pressed again" :
         r == ZE_GONE ? "the channel is gone" : "no prompt in time");
    return r == ZE_ESCAPE && !prompt ? r : 0;
}

/* failure already reported with zm_err() */
#define ZE_FAIL     (-9)

/* the header data subpackets and replies are read with, once running */
#define ZM_TMO      (ZM_TIMEOUT_SEC * 1000L)

/* CRC-32 of the first len bytes of the file (0: all of it), for ZCRC */
static uint32_t zm_filecrc(int fd, uint32_t len)
{
    unsigned char buf[8192];
    uint32_t crc = 0xffffffff;
    off_t pos = 0;
    ssize_t n;

    while ((n = pread(fd, buf, sizeof(buf), pos)) > 0) {
        if (len && pos + n > (off_t)len)
            n = (ssize_t)(len - pos);
        for (ssize_t i = 0; i < n; i++)
            crc = zm_crc32(crc, buf[i]);
        pos += n;
        if (len && pos >= (off_t)len)
            break;
    }
    return ~crc;
}

/*
 * Send the open file fd to the remote rz, as name. rz speaks first --
 * ZRINIT, its capabilities -- and asks for the data from where it
 * wants it (ZRPOS). The data streams as ZCRCG subpackets, with a ZCRCQ
 * every ZM_ACK_EVERY bytes for rz to ZACK; up to ZM_WINDOW goes out
 * before an ack is waited for -- a window that has to cover a round trip
 * of the line: a far one (ssh, a console server) holds a lot. If rz
 * names a buffer size, the frame ends with a ZCRCW there instead,
 * waiting for the ZACK.
 *
 * Between subpackets anything rz says is read without waiting: a ZRPOS
 * -- rz saw a bad subpacket -- rewinds the stream to where it says. rz
 * then has to read through all that was on its way before the new ZDATA
 * comes, and that bounds the window: rz counts it as garbage, and gives
 * up after some 20 * (1400 + baud rate) bytes of it -- 800 KB at 38400.
 * Meanwhile it repeats its ZRPOS at every 40 KB or so: a ZRPOS for the
 * position just rewound to, with nothing acked since, is one of those,
 * and ignored -- every rewind more would send rz another load of data it
 * has to skip. Should the ZDATA have been lost all the same, the wait
 * for an ack times out and the data goes again. A ZRPOS also narrows
 * the window -- the line may be losing data -- to half, ZM_WINDOW_MIN
 * at least, from where it grows back with every ack.
 *
 * Should rz be gone -- killed, given up -- what is sent goes to the
 * shell instead, so the prompt coming back ends the transfer
 * (ZE_SHELL); the window bounds what was on its way.
 */
static int zm_send(int fd, const char *name, const struct stat *st)
{
    static unsigned char buf[ZM_SUBPKT];
    unsigned char h[4], info[1024];
    uint32_t size = (uint32_t)st->st_size, pos, acked, window, resync;
    uint32_t limit = ZM_WINDOW;
    size_t ilen, sinceq;
    int t, flags, tries = 0, dup;

    for (;;) {
        if ((t = zm_gethdr(h, ZM_START_SEC * 1000L)) < 0)
            return t;
        if (t == ZRINIT)
            break;
        if (t == ZCHALLENGE)
            zm_hexhdr(ZACK, h);
    }
    flags = h[ZF0];
    window = h[0] | h[1] << 8;
    zm.tx32 = !!(flags & CANFC32);
    zm.escctl = !!(flags & ESCCTL);
    if (!window && !((flags & CANFDX) && (flags & CANOVIO)))
        window = ZM_ACK_EVERY;  /* cannot take data while answering */
    VLOG("zmodem: rz flags 0x%02x, buffer %u", flags, window);

    /*
     * name NUL "size mtime mode serial files-left bytes-left" NUL; the
     * mode as all of st_mode, as lsz sends it: rz only applies the
     * permissions of a mode that says it is a regular file
     */
    ilen = (size_t)snprintf((char *)info, sizeof(info), "%s", name) + 1;
    ilen += (size_t)snprintf((char *)info + ilen, sizeof(info) - ilen,
                             "%lu %lo %o 0 1 %lu", (unsigned long)size,
                             (unsigned long)st->st_mtime,
                             (unsigned)st->st_mode,
                             (unsigned long)size) + 1;
    for (;;) {
        zm_setpos(h, 0);
        h[ZF0] = ZCBIN;
        h[ZF1] = ZMCLOB;
        zm_binhdr(ZFILE, h);
        zm_sdata(info, ilen, ZCRCW);
    again:
        t = zm_gethdr(h, ZM_TMO);
        if (t == ZRPOS)
            break;
        switch (t) {
        case ZSKIP:
            zm_err("the remote rz skipped the file");
            return ZE_FAIL;
        case ZCRC:
            zm_setpos(h, zm_filecrc(fd, zm_getpos(h)));
            zm_hexhdr(ZCRC, h);
            goto again;
        case ZRINIT: case ZNAK: case ZE_BAD: case ZE_TIMEOUT:
            if (++tries >= ZM_RETRIES)
                return t == ZE_TIMEOUT ? t : ZE_BAD;
            continue;
        case ZCAN: case ZABORT: case ZFERR:
            return ZE_RCAN;
        default:
            if (t < 0)
                return t;
            goto again;
        }
    }

    resync = pos = zm_getpos(h);
    tries = 0;
    zm.watch = 1;
    for (;;) {                          /* a frame of data from pos */
        if (pos > size)
            pos = size;
        acked = pos;
        sinceq = 0;
        if (pos < size) {
            zm_setpos(h, pos);
            zm_binhdr(ZDATA, h);
        }
        while (pos < size) {
            ssize_t n = pread(fd, buf, ZM_SUBPKT, pos);
            int end, must;

            if (n <= 0) {
                zm_err("read: %s", n < 0 ? strerror(errno) : "file shrank");
                return ZE_FAIL;
            }
            if (pos + n >= size)
                end = ZCRCE;
            else if (window && pos + n - acked >= window)
                end = ZCRCW;
            else if (sinceq + n >= ZM_ACK_EVERY)
                end = ZCRCQ;
            else
                end = ZCRCG;
            zm_sdata(buf, (size_t)n, end);
            pos += (uint32_t)n;
            sinceq = end == ZCRCQ ? 0 : sinceq + (size_t)n;
            zm_progress(pos, 0);
            if (end == ZCRCE)
                break;

            /* what rz says: waited for at a ZCRCW or the limit */
            for (;;) {
                must = end == ZCRCW || pos - acked >= limit;
                if (!must) {
                    int r = zm_pending();

                    if (r < 0)
                        return r;
                    if (!r)
                        break;
                }
                t = zm_gethdr(h, ZM_TMO);
                if (t == ZACK) {
                    uint32_t a = zm_getpos(h);

                    if (a > acked && a <= pos) {
                        limit += a - acked;
                        if (limit > ZM_WINDOW)
                            limit = ZM_WINDOW;
                        acked = a;
                        tries = 0;
                    }
                    if (end == ZCRCW && acked == pos)
                        goto frame;     /* the frame is over: a new one */
                    continue;
                }
                if (t == ZRPOS) {
                    if (zm_getpos(h) == resync && acked == resync)
                        continue;       /* sent before the ZDATA got there */
                    if (++tries >= ZM_RETRIES)
                        return ZE_BAD;
                    resync = pos = zm_getpos(h);
                    zm.olen = 0;        /* stale data: rz skips it anyway */
                    limit = limit / 2 > ZM_WINDOW_MIN ? limit / 2 : ZM_WINDOW_MIN;
                    VLOG("zmodem: rz wants %lu again; %lu unacked at most",
                         (unsigned long)pos, (unsigned long)limit);
                    goto frame;
                }
                if (t == ZE_TIMEOUT && must) {
                    /* the ack -- or the ZDATA -- got lost: send again
                       from what is acked */
                    if (++tries >= ZM_RETRIES)
                        return t;
                    resync = pos = acked;
                    goto frame;
                }
                if (t == ZSKIP || t == ZCAN || t == ZABORT || t == ZFERR)
                    return ZE_RCAN;
                if (t < 0 && t != ZE_BAD && t != ZE_TIMEOUT)
                    return t;
            }
        }

        /*
         * all of it is out: ZEOF, which rz answers with ZRINIT. A
         * repeated ZRPOS is ignored here too -- but if nothing else
         * comes, rz is still waiting for that position: it gets the
         * data again
         */
        for (;;) {
            zm_setpos(h, size);
            zm_hexhdr(ZEOF, h);
            dup = 0;
        eof:
            t = zm_gethdr(h, ZM_TMO);
            if (t == ZRINIT)
                goto fin;
            if (t == ZRPOS && zm_getpos(h) == resync && acked == resync) {
                dup = 1;
                goto eof;
            }
            if (t == ZRPOS || (t == ZE_TIMEOUT && dup)) {
                if (++tries >= ZM_RETRIES)
                    return t == ZRPOS ? ZE_BAD : t;
                if (t == ZRPOS) {
                    resync = zm_getpos(h);
                    limit = limit / 2 > ZM_WINDOW_MIN ? limit / 2 : ZM_WINDOW_MIN;
                }
                pos = resync;
                goto frame;
            }
            if (t == ZACK) {
                uint32_t a = zm_getpos(h);

                if (a > acked && a <= size)
                    acked = a;
                goto eof;
            }
            if (t == ZE_BAD)
                goto eof;
            if (t == ZE_TIMEOUT) {
                if (++tries >= ZM_RETRIES)
                    return t;
                continue;
            }
            if (t == ZSKIP || t == ZCAN || t == ZABORT || t == ZFERR)
                return ZE_RCAN;
            if (t < 0)
                return t;
            goto eof;
        }
    frame:
        continue;
    }

fin:
    /* the file is there; ZFIN ends the session, "OO" is the last word */
    for (tries = 0; tries < 3; tries++) {
        zm_setpos(h, 0);
        zm_hexhdr(ZFIN, h);
        do
            t = zm_gethdr(h, ZM_TMO);
        while (t == ZRINIT || t == ZACK || t == ZE_BAD);
        if (t == ZFIN) {
            zm_out('O');
            zm_out('O');
            return zm_flush();
        }
        if (t != ZE_TIMEOUT)
            break;
    }
    VLOG("zmodem: no ZFIN back; the file got there all the same");
    return t == ZE_ESCAPE || t == ZE_CANCEL ? t : 0;
}

/* --get: the file being received, under a temporary name until done */
static char zm_tmp[4096];
static mode_t zm_umask;

/*
 * Receive the file the remote sz sends into dst. sz speaks first --
 * ZRQINIT -- and is told what this end can take (ZRINIT: full duplex,
 * CRC-32). The file arrives into a temporary file next to dst, renamed
 * over it only once all of it is there: a broken transfer leaves dst
 * as it was. A subpacket that comes in damaged is answered with a
 * ZRPOS for the position the good data reaches, and sz rewinds there.
 * One file per --get: a second ZFILE is skipped.
 */
static int zm_recv(const char *dst)
{
    static unsigned char buf[ZM_MAXPKT + 1];
    unsigned char h[4], z[4] = { 0 };
    long long size = -1;
    unsigned long mtime = 0;
    unsigned mode = 0;
    uint32_t rxpos = 0;
    int t, fd = -1, got = 0, tries = 0;
    size_t n;

    if ((t = zm_gethdr(h, ZM_START_SEC * 1000L)) < 0)
        return t;
    for (;; t = zm_gethdr(h, ZM_TMO)) {
        int end;

        switch (t) {
        case ZRQINIT:
        rinit:
            zm_setpos(h, 0);
            h[ZF0] = CANFDX | CANOVIO | CANFC32;
            zm_hexhdr(ZRINIT, h);
            break;

        case ZSINIT:    /* sz's attention string: noted, not used */
            end = zm_rdata(buf, ZM_MAXPKT, &n, ZM_TMO);
            if (end < 0 && end != ZE_BAD && end != ZE_LONG)
                return end;
            zm_setpos(h, 1);
            zm_hexhdr(end < 0 ? ZNAK : ZACK, end < 0 ? z : h);
            break;

        case ZFILE:
            end = zm_rdata(buf, ZM_MAXPKT, &n, ZM_TMO);
            if (end < 0 && end != ZE_BAD && end != ZE_LONG)
                return end;
            if (end < 0) {
                zm_hexhdr(ZNAK, z);
                break;
            }
            if (fd >= 0 || got) {
                zm_hexhdr(ZSKIP, z);
                break;
            }
            buf[n] = '\0';
            if (strlen((char *)buf) + 1 < n)
                sscanf((char *)buf + strlen((char *)buf) + 1, "%lld %lo %o",
                       &size, &mtime, &mode);
            VLOG("zmodem: file '%s', %lld bytes", esc_str((char *)buf), size);
            zm.total = size;
            snprintf(zm_tmp, sizeof(zm_tmp), "%s.XXXXXX", dst);
            if ((fd = mkstemp(zm_tmp)) < 0) {
                zm_err("%s: %s", zm_tmp, strerror(errno));
                zm_tmp[0] = '\0';
                return ZE_FAIL;
            }
            rxpos = 0;
            zm_setpos(h, rxpos);
            zm_hexhdr(ZRPOS, h);
            break;

        case ZDATA:
            if (fd < 0)
                goto rinit;     /* no ZFILE yet: sz has to start over */
            if (zm_getpos(h) != rxpos) {
                zm_setpos(h, rxpos);
                zm_hexhdr(ZRPOS, h);
                break;
            }
            for (;;) {
                end = zm_rdata(buf, ZM_MAXPKT, &n, ZM_TMO);
                if (end == ZE_BAD || end == ZE_LONG || end == ZE_TIMEOUT) {
                    VLOG("zmodem: bad subpacket at %u", (unsigned)rxpos);
                    if (++tries >= ZM_RETRIES)
                        return end == ZE_TIMEOUT ? end : ZE_BAD;
                    zm_setpos(h, rxpos);
                    zm_hexhdr(ZRPOS, h);
                    break;
                }
                if (end < 0)
                    return end;
                if (pwrite(fd, buf, n, rxpos) != (ssize_t)n) {
                    zm_err("%s: %s", zm_tmp, strerror(errno));
                    return ZE_FAIL;
                }
                rxpos += (uint32_t)n;
                tries = 0;
                zm_progress(rxpos, 0);
                if (end == ZCRCW || end == ZCRCQ) {
                    zm_setpos(h, rxpos);
                    zm_hexhdr(ZACK, h);
                }
                if (end == ZCRCW || end == ZCRCE)
                    break;
            }
            break;

        case ZEOF:
            if (fd < 0 || zm_getpos(h) != rxpos)
                break;  /* behind: the ZRPOS asked for is on its way */
            fchmod(fd, (mode & 0777 ? mode & 0777 : 0666) & ~zm_umask);
            if (mtime) {
                struct timespec ts[2] = {
                    { .tv_nsec = UTIME_OMIT },
                    { .tv_sec = (time_t)mtime },
                };
                futimens(fd, ts);
            }
            if (close(fd) < 0 || rename(zm_tmp, dst) < 0) {
                fd = -1;
                zm_err("%s: %s", dst, strerror(errno));
                return ZE_FAIL;
            }
            fd = -1;
            zm_tmp[0] = '\0';
            got = 1;
            goto rinit;         /* ready for the next file, or ZFIN */

        case ZFIN:
            zm_hexhdr(ZFIN, z);
            if (zm_flush() < 0 || !got) {
                if (!got)
                    zm_err("the remote sz sent no file");
                return ZE_FAIL;
            }
            /* "OO", over and out: not for the screen */
            for (int i = 0; i < 2; i++) {
                if (zm.ipos == zm.ilen && zm_fill(1000) < 0)
                    break;
                if (zm.ibuf[zm.ipos] != 'O')
                    break;
                zm.ipos++;
            }
            return 0;

        case ZFREECNT:
            zm_setpos(h, 0xffffffff);
            zm_hexhdr(ZACK, h);
            break;

        case ZCOMMAND:
            zm_err("the remote sz sent a command; refused");
            return ZE_FAIL;

        case ZCAN: case ZABORT: case ZFERR:
            return ZE_RCAN;

        case ZE_TIMEOUT: case ZE_BAD:
            if (++tries >= ZM_RETRIES)
                return t;
            if (fd < 0)
                goto rinit;
            zm_setpos(h, rxpos);
            zm_hexhdr(ZRPOS, h);
            break;

        default:
            if (t < 0)
                return t;
            break;
        }
    }
}

/*
 * Run one transfer over the pty, the remote rz/sz just typed at the
 * prompt: --put sends the local file path, --get receives the remote
 * file path into the current directory, under its last component. The
 * pty is non-blocking meanwhile, so neither a stalled write nor a
 * silent read holds off the escape key or ^C.
 *
 * Whatever breaks the transfer -- the user, the remote end, a timeout
 * -- is reported on stderr; then, if rz/sz may still be running, the
 * session is cancelled and the rest of its output swallowed, until the
 * other end has answered the cancel (zm_abort) -- for the escape key
 * too, if only for ZM_ESC_WAIT_MS: a proxy that leaves at once may
 * leave rz/sz running on the remote end, behind a cancel that never
 * got there. A second escape press leaves without waiting. Bytes
 * read past the end of a good session are the remote shell's again:
 * zm_leftover() hands them back.
 */
static int zm_transfer(const struct zm_link *l, int dir, const char *path)
{
    static char what[4200];
    const char *base = strrchr(path, '/') ? strrchr(path, '/') + 1 : path;
    struct stat st;
    int r, fd = -1, flags;

    memset(&zm, 0, sizeof(zm));
    if (!zm_crc32tab[1])
        zm_crc_init();
    zm.l = l;
    zm.total = -1;
    zm_keyev = 0;
    zm_tmp[0] = '\0';
    snprintf(what, sizeof(what), "%s %s", dir == ZM_PUT ? "put" : "get", path);
    zm.what = what;
    clock_gettime(CLOCK_MONOTONIC, &zm.t0);
    zm_umask = umask(0);
    umask(zm_umask);

    flags = fcntl(l->fd, F_GETFL);
    fcntl(l->fd, F_SETFL, flags | O_NONBLOCK);
    if (dir == ZM_GET) {
        r = zm_recv(base);
    } else if ((fd = open(path, O_RDONLY)) < 0 || fstat(fd, &st) < 0) {
        zm_err("%s", strerror(errno));
        r = ZE_FAIL;
    } else if (!S_ISREG(st.st_mode) || st.st_size > 0xffffffffLL) {
        zm_err("%s", S_ISREG(st.st_mode) ? "larger than 4 GiB" :
                     "not a regular file");
        r = ZE_FAIL;
    } else {
        zm.total = st.st_size;
        r = zm_send(fd, base, &st);
    }
    if (r == 0)
        r = zm_flush();
    if (fd >= 0)
        close(fd);

    if (r == 0) {
        zm_progress(zm.done, 1);
    } else {
        switch (r) {
        case ZE_FAIL:    break;
        case ZE_ESCAPE:  zm_err("cancelled, leaving"); break;
        case ZE_CANCEL:  zm_err("interrupted"); break;
        case ZE_RCAN:
            /* sz breaks off at once when it cannot open the file */
            if (dir == ZM_GET && zm.total < 0)
                zm_err("the remote sz sent no file -- is it there?");
            else
                zm_err("cancelled by the remote end");
            break;
        case ZE_GONE:    zm_err("the channel is gone"); break;
        case ZE_TIMEOUT: zm_err("no answer from the remote end"); break;
        case ZE_NOSTART: zm_err("the remote %s did not start",
                                dir == ZM_PUT ? "rz" : "sz"); break;
        case ZE_SHELL:   zm_err("the remote rz is gone: back at the prompt");
                         break;
        default:         zm_err("too many errors"); break;
        }
        if (zm_tmp[0])
            unlink(zm_tmp);
        /*
         * Nothing to wait for if rz/sz never came, or the channel is
         * gone; nothing to cancel if the remote broke off itself. With
         * rz gone the prompt is back already: the CANs are for the
         * shell's line, and anything coming back -- its echo -- says
         * they got there
         */
        if (r != ZE_GONE && r != ZE_NOSTART &&
            zm_abort(r != ZE_RCAN, r == ZE_ESCAPE || r == ZE_SHELL
                                   ? ZM_ESC_WAIT_MS
                                   : INTR_WAIT_SEC * 1000L) == ZE_ESCAPE)
            r = ZE_ESCAPE;
        zm.ipos = zm.ilen;      /* nothing in there is the shell's */
    }
    fcntl(l->fd, F_SETFL, flags);
    return r == 0 ? ZM_OK : r == ZE_ESCAPE ? ZM_ESCAPE :
           r == ZE_CANCEL ? ZM_CANCEL : ZM_FAILED;
}

/* the remote shell's bytes that came in behind the session */
static size_t zm_leftover(const unsigned char **p)
{
    *p = zm.ibuf + zm.ipos;
    return zm.ilen - zm.ipos;
}
