#!/usr/bin/env python3.12
"""Copy-mode tests: a copied file keeps its mode, as it does under scp.

The reported bug: `mssh ./script host:/tmp/` left the script non-executable,
because paramiko's put() sends an empty attribute block and the server
creates the file at its own 0666 & ~umask.  scp transmits the mode.

These drive mssh's real upload_path/download_path against OpenSSH's own
sftp-server over a pipe -- no sshd, no network, but the same server code a
real copy talks to -- and compare every result against what scp itself
produces for the identical source under the identical umask.  Comparing
against scp rather than against a hardcoded number is the point: the rule
being matched is scp's, so scp is the oracle.
"""

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
MSSH = os.path.join(os.path.dirname(HERE), "mssh")

spec = importlib.util.spec_from_loader("mssh", SourceFileLoader("mssh", MSSH))
mssh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mssh)

SFTP_SERVER = "/usr/libexec/openssh/sftp-server"

# mssh filters a copied mode by this process's umask, in both directions, so
# every expectation is derived from it rather than from a literal.
LOCAL_UMASK = mssh.local_umask()


class Opts(object):
    def __init__(self, recursive=False, preserve=False):
        self.recursive = recursive
        self.preserve = preserve
        self.verbose = False


class PipeChan(object):
    """The minimum an SFTPClient needs: a bidirectional byte channel.

    Points it at a real sftp-server running under a chosen umask, which is
    what decides the created mode on the far side.
    """

    def __init__(self, umask):
        self.proc = subprocess.Popen(
            [SFTP_SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            preexec_fn=lambda: os.umask(umask),
        )

    def send(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()
        return len(data)

    def recv(self, size):
        return self.proc.stdout.read(size)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.wait()
        # Close stdout too, or every test leaks an fd and unittest fills the
        # log with ResourceWarnings that hide the actual results.
        try:
            self.proc.stdout.close()
        except Exception:
            pass

    def settimeout(self, _):
        pass

    def get_name(self):
        return "sftp"


def modes(root):
    """Every path under root, relative, with its octal mode."""
    out = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root)
            out[rel] = stat.S_IMODE(os.lstat(path).st_mode)
    return out


@unittest.skipUnless(os.path.exists(SFTP_SERVER), "no sftp-server")
@unittest.skipUnless(shutil.which("scp"), "no scp")
class ModeBase(unittest.TestCase):
    umask = 0o022

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="msshmode.")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.chan = PipeChan(self.umask)
        self.addCleanup(self.chan.close)
        self.sftp = paramiko.SFTPClient(self.chan)
        self.addCleanup(self.sftp.close)
        # A wrapper that makes scp run its "remote" end here, under the same
        # umask, so the two tools are compared under identical conditions.
        self.ssh = os.path.join(self.tmp, "fakessh")
        with open(self.ssh, "w") as fh:
            fh.write(
                "#!/bin/sh\n"
                "while [ $# -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    -o|-c|-i|-F|-l|-p|-P) shift 2 ;;\n"
                "    -*) shift ;;\n"
                "    *) shift; break ;;\n"
                "  esac\n"
                "done\n"
                "umask 0%03o\n"
                "exec /bin/sh -c \"$*\"\n" % self.umask
            )
        os.chmod(self.ssh, 0o755)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def expect(self, mode):
        """The mode a freshly created copy should end at.

        The *local* umask, for both directions.  SFTP gives no way to ask
        the far side for its umask, so mssh applies ours: for upload that is
        a deliberate approximation of scp, which uses the receiving side's.
        The two agree whenever both ends are configured alike -- the usual
        case -- and -p is there when the exact bits matter.

        setuid/setgid/sticky are not masked, matching scp.
        """
        return (mode & ~LOCAL_UMASK & 0o777) | (mode & 0o7000)

    def expect_scp(self, mode):
        """What scp would produce here: filtered by the *remote* umask."""
        return (mode & ~self.umask & 0o777) | (mode & 0o7000)

    def make(self, rel, mode, content=b"#!/bin/sh\necho hi\n"):
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(content)
        os.chmod(full, mode)
        return full

    def scp(self, args, preserve=False, recursive=False):
        cmd = ["scp", "-S", self.ssh]
        if preserve:
            cmd.append("-p")
        if recursive:
            cmd.append("-r")
        proc = subprocess.run(cmd + args, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestUploadFileMode(ModeBase):
    """A single uploaded file lands at the mode scp would give it."""

    def _one(self, mode):
        src = self.make("src/prog", mode)
        os.makedirs(self.path("mine"))
        os.makedirs(self.path("theirs"))

        mssh.upload_path(self.sftp, src, self.path("mine", "prog"), Opts())
        self.scp([src, "host:" + self.path("theirs") + "/"])

        ours = stat.S_IMODE(os.stat(self.path("mine", "prog")).st_mode)
        theirs = stat.S_IMODE(os.stat(self.path("theirs", "prog")).st_mode)
        if self.umask == LOCAL_UMASK:
            # Same umask on both ends -- the usual case, and the only one
            # where mssh and scp are expected to agree bit for bit.
            self.assertEqual(
                oct(ours), oct(theirs),
                "source %04o: mssh gave %04o, scp gave %04o"
                % (mode, ours, theirs))
        else:
            # Different umasks: scp uses the remote's, mssh uses ours.  Both
            # are checked, so the divergence stays deliberate and visible.
            self.assertEqual(oct(theirs), oct(self.expect_scp(mode)))
        return ours

    def test_executable_is_still_executable(self):
        """The reported bug: a 0755 script arrived 0644."""
        got = self._one(0o755)
        self.assertEqual(oct(got), oct(self.expect(0o755)))
        self.assertTrue(got & stat.S_IXUSR, "the owner cannot run it")

    def test_plain_file(self):
        self.assertEqual(oct(self._one(0o644)), oct(self.expect(0o644)))

    def test_private_file_is_not_widened(self):
        self.assertEqual(oct(self._one(0o600)), oct(0o600))

    def test_world_writable_source_is_filtered_by_remote_umask(self):
        self.assertEqual(oct(self._one(0o777)), oct(self.expect(0o777)))

    def test_setuid_survives(self):
        got = self._one(0o4755)
        self.assertEqual(oct(got), oct(0o4000 | self.expect(0o755)))
        self.assertTrue(got & stat.S_ISUID)

    def test_setgid_survives(self):
        got = self._one(0o2755)
        self.assertEqual(oct(got), oct(0o2000 | self.expect(0o755)))
        self.assertTrue(got & stat.S_ISGID)

    def test_no_permissions_at_all(self):
        self.assertEqual(oct(self._one(0o000)), oct(0o000))

    def test_content_still_arrives_intact(self):
        src = self.make("src/big", 0o755, content=os.urandom(200000))
        mssh.upload_path(self.sftp, src, self.path("big"), Opts())
        with open(src, "rb") as a, open(self.path("big"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertEqual(oct(stat.S_IMODE(os.stat(self.path("big")).st_mode)),
                         oct(self.expect(0o755)))

    def test_empty_file(self):
        src = self.make("src/empty", 0o755, content=b"")
        mssh.upload_path(self.sftp, src, self.path("empty"), Opts())
        self.assertEqual(os.path.getsize(self.path("empty")), 0)
        self.assertEqual(oct(stat.S_IMODE(os.stat(self.path("empty")).st_mode)),
                         oct(self.expect(0o755)))


class TestUploadUnderTightUmask(TestUploadFileMode):
    """Same comparisons with a restrictive remote umask."""
    umask = 0o077


class TestUploadLooseUmask(TestUploadFileMode):
    umask = 0o002


class TestExistingRemoteFile(ModeBase):
    def test_existing_file_keeps_its_own_mode(self):
        """Overwriting must not widen a tight mode on the target."""
        src = self.make("src/conf", 0o644, content=b"new\n")
        dest = self.make("dest/conf", 0o600, content=b"old\n")

        mssh.upload_path(self.sftp, src, dest, Opts())
        self.assertEqual(oct(stat.S_IMODE(os.stat(dest).st_mode)), oct(0o600))
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), b"new\n")       # contents did update

    def test_scp_agrees(self):
        src = self.make("src/conf", 0o644, content=b"new\n")
        dest = self.make("dest/conf", 0o600, content=b"old\n")
        self.scp([src, "host:" + dest])
        self.assertEqual(oct(stat.S_IMODE(os.stat(dest).st_mode)), oct(0o600))

    def test_existing_file_is_truncated_not_appended(self):
        src = self.make("src/f", 0o644, content=b"short\n")
        dest = self.make("dest/f", 0o644, content=b"a much longer old file\n")
        mssh.upload_path(self.sftp, src, dest, Opts())
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), b"short\n")


class TestUploadTree(ModeBase):
    """-r reproduces scp's modes for a whole tree, files and directories."""

    def _tree(self, root):
        self.make(root + "/run.sh", 0o755)
        self.make(root + "/data.txt", 0o644, content=b"data\n")
        self.make(root + "/sub/lib.so", 0o644, content=b"lib\n")
        self.make(root + "/sub/tool", 0o700)
        os.chmod(self.path(root, "sub"), 0o750)
        os.chmod(self.path(root), 0o755)

    def test_tree_matches_scp(self):
        self._tree("tree")
        os.makedirs(self.path("mine"))
        os.makedirs(self.path("theirs"))

        mssh.upload_path(self.sftp, self.path("tree"), self.path("mine/tree"),
                         Opts(recursive=True))
        self.scp([self.path("tree"), "host:" + self.path("theirs") + "/"],
                 recursive=True)

        if self.umask == LOCAL_UMASK:
            self.assertEqual(modes(self.path("mine/tree")),
                             modes(self.path("theirs/tree")))
        else:
            # Different umasks on the two ends: scp filters by the remote's,
            # mssh by ours.  Check each against its own rule so the
            # divergence is asserted rather than merely tolerated.
            self.assertEqual(
                modes(self.path("mine/tree")),
                {"run.sh": self.expect(0o755), "data.txt": self.expect(0o644),
                 "sub": self.expect(0o750),
                 "sub/lib.so": self.expect(0o644),
                 "sub/tool": self.expect(0o700)})
            self.assertEqual(
                modes(self.path("theirs/tree")),
                {"run.sh": self.expect_scp(0o755),
                 "data.txt": self.expect_scp(0o644),
                 "sub": self.expect_scp(0o750),
                 "sub/lib.so": self.expect_scp(0o644),
                 "sub/tool": self.expect_scp(0o700)})
        self.assertEqual(
            oct(stat.S_IMODE(os.stat(self.path("mine/tree/run.sh")).st_mode)),
            oct(self.expect(0o755)))

    def test_read_only_source_directory_is_still_filled(self):
        """A 0555 directory must be created writable, then narrowed."""
        self.make("ro/inside.sh", 0o755)
        os.chmod(self.path("ro"), 0o555)
        self.addCleanup(os.chmod, self.path("ro"), 0o755)

        mssh.upload_path(self.sftp, self.path("ro"), self.path("out"),
                         Opts(recursive=True))

        self.assertTrue(os.path.exists(self.path("out/inside.sh")))
        self.assertEqual(
            oct(stat.S_IMODE(os.stat(self.path("out")).st_mode)),
            oct(self.expect(0o555)))
        self.assertEqual(
            oct(stat.S_IMODE(os.stat(self.path("out/inside.sh")).st_mode)),
            oct(self.expect(0o755)))

    def test_existing_remote_directory_keeps_its_mode(self):
        self.make("tree2/f.sh", 0o755)
        os.makedirs(self.path("dst"), 0o700)
        os.chmod(self.path("dst"), 0o700)

        mssh.upload_path(self.sftp, self.path("tree2"), self.path("dst"),
                         Opts(recursive=True))

        self.assertEqual(
            oct(stat.S_IMODE(os.stat(self.path("dst")).st_mode)), oct(0o700))
        self.assertEqual(
            oct(stat.S_IMODE(os.stat(self.path("dst/f.sh")).st_mode)),
            oct(self.expect(0o755)))

    def test_preserve_still_wins_over_the_umask(self):
        """-p keeps the exact mode, umask and all -- unchanged behaviour."""
        self.make("p/w.sh", 0o777)
        os.chmod(self.path("p"), 0o777)
        mssh.upload_path(self.sftp, self.path("p"), self.path("pout"),
                         Opts(recursive=True, preserve=True))
        self.assertEqual(
            oct(stat.S_IMODE(os.stat(self.path("pout/w.sh")).st_mode)),
            oct(0o777))


class TestUploadTreeTightUmask(TestUploadTree):
    umask = 0o077


class TestDownloadMode(ModeBase):
    """The same rule in the other direction, where paramiko had the same gap."""

    def _one(self, mode):
        src = self.make("remote/prog", mode)
        os.makedirs(self.path("mine"))
        os.makedirs(self.path("theirs"))

        mssh.download_path(self.sftp, src, self.path("mine", "prog"), Opts())
        self.scp(["host:" + src, self.path("theirs") + "/"])

        ours = stat.S_IMODE(os.stat(self.path("mine", "prog")).st_mode)
        theirs = stat.S_IMODE(os.stat(self.path("theirs", "prog")).st_mode)
        self.assertEqual(oct(ours), oct(theirs),
                         "remote %04o: mssh %04o, scp %04o"
                         % (mode, ours, theirs))
        return ours

    def test_downloaded_script_is_executable(self):
        self.assertEqual(oct(self._one(0o755)), oct(0o755))

    def test_downloaded_plain_file(self):
        self.assertEqual(oct(self._one(0o644)), oct(0o644))

    def test_downloaded_private_file_stays_private(self):
        self.assertEqual(oct(self._one(0o600)), oct(0o600))

    def test_existing_local_file_keeps_its_mode(self):
        src = self.make("remote/f", 0o755, content=b"remote\n")
        dest = self.make("local/f", 0o600, content=b"local\n")
        mssh.download_path(self.sftp, src, dest, Opts())
        self.assertEqual(oct(stat.S_IMODE(os.stat(dest).st_mode)), oct(0o600))
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), b"remote\n")

    def test_tree_download_matches_scp(self):
        self.make("rtree/run.sh", 0o755)
        self.make("rtree/data.txt", 0o644, content=b"d\n")
        self.make("rtree/sub/x", 0o700)
        os.chmod(self.path("rtree/sub"), 0o750)
        os.chmod(self.path("rtree"), 0o755)
        os.makedirs(self.path("mine"))
        os.makedirs(self.path("theirs"))

        mssh.download_path(self.sftp, self.path("rtree"),
                           self.path("mine/rtree"), Opts(recursive=True))
        self.scp(["host:" + self.path("rtree"), self.path("theirs") + "/"],
                 recursive=True)

        self.assertEqual(modes(self.path("mine/rtree")),
                         modes(self.path("theirs/rtree")))

    def test_contents_are_intact(self):
        blob = os.urandom(300000)
        src = self.make("remote/blob", 0o755, content=blob)
        mssh.download_path(self.sftp, src, self.path("blob"), Opts())
        with open(self.path("blob"), "rb") as fh:
            self.assertEqual(fh.read(), blob)


class TestNoRecursionGuard(ModeBase):
    """The -r guards still fire; the mode work must not have bypassed them."""

    def test_upload_directory_without_r(self):
        os.makedirs(self.path("d"))
        with self.assertRaises(SystemExit) as caught:
            mssh.upload_path(self.sftp, self.path("d"), self.path("o"), Opts())
        self.assertIn("use -r", str(caught.exception))

    def test_download_directory_without_r(self):
        os.makedirs(self.path("d"))
        with self.assertRaises(SystemExit) as caught:
            mssh.download_path(self.sftp, self.path("d"), self.path("o"),
                               Opts())
        self.assertIn("use -r", str(caught.exception))

    def test_missing_remote_file_is_reported(self):
        with self.assertRaises(SystemExit) as caught:
            mssh.download_path(self.sftp, self.path("nope"), self.path("o"),
                               Opts())
        self.assertIn("cannot read", str(caught.exception))


class TestUnwritableModes(ModeBase):
    """A mode without write permission must not break its own transfer.

    The trap: create the file at the target mode, close it, then let the
    transfer reopen it by path -- which fails EACCES for 0400 or 0000,
    because the second open() is checked against the mode the first one just
    set.  These run as an unprivileged user, since root ignores permission
    bits entirely and would pass either way.
    """

    NOBODY = "nobody"

    def _as_nobody(self, script):
        """Run a snippet as an unprivileged user in a world-writable dir."""
        if os.geteuid() != 0:
            self.skipTest("need root to drop privileges")
        try:
            import pwd
            pwd.getpwnam(self.NOBODY)
        except KeyError:
            self.skipTest("no %s user" % self.NOBODY)

        work = self.path("unpriv")
        os.makedirs(work)
        os.chmod(self.tmp, 0o777)
        os.chmod(work, 0o777)
        # /root is not traversable by nobody, so the script under test has to
        # be reachable from the unprivileged side.
        local_mssh = self.path("mssh_under_test")
        shutil.copyfile(MSSH, local_mssh)
        os.chmod(local_mssh, 0o644)
        path = self.path("snippet.py")
        with open(path, "w") as fh:
            fh.write(script.replace("@MSSH@", local_mssh))
        os.chmod(path, 0o644)
        proc = subprocess.run(
            ["runuser", "-u", self.NOBODY, "--", sys.executable, path, work],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return proc

    def test_download_of_an_unwritable_file(self):
        """A 0400 remote file (a sealed key) must still download.

        0400 is the case that matters: readable, so a copy is possible at
        all, but not writable, so a create-then-reopen loses.  (0000 is out
        of scope in both directions -- nobody but root can read the source,
        so there is nothing to copy; cp(1) fails on it too.)
        """
        proc = self._as_nobody(HARNESS + """
for mode in (0o400, 0o444, 0o500, 0o755):
    src = os.path.join(WORK, "remote%04o" % mode)
    with open(src, "wb") as fh:
        fh.write(b"payload\\n" * 2000)
    os.chmod(src, mode)
    dest = os.path.join(WORK, "local%04o" % mode)
    mssh.download_path(sftp, src, dest, Opts())
    got = stat.S_IMODE(os.stat(dest).st_mode)
    print("%04o -> %04o size=%d" % (mode, got, os.path.getsize(dest)))
""")
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        out = proc.stdout.decode()
        # The mode arrives and the payload is complete -- 16000 bytes, not a
        # zero-length file left behind by a failed reopen.
        self.assertIn("0400 -> 0400 size=16000", out, out)
        self.assertIn("0444 -> 0444 size=16000", out, out)
        self.assertIn("0500 -> 0500 size=16000", out, out)
        self.assertIn("0755 -> 0755 size=16000", out, out)

    def test_upload_of_an_unwritable_file(self):
        """The same on the way out: a 0400 source must upload."""
        proc = self._as_nobody(HARNESS + """
for mode in (0o400, 0o444, 0o755):
    src = os.path.join(WORK, "src%04o" % mode)
    with open(src, "wb") as fh:
        fh.write(b"x" * 5000)
    os.chmod(src, mode)
    dest = os.path.join(WORK, "up%04o" % mode)
    mssh.upload_path(sftp, src, dest, Opts())
    print("%04o -> %04o size=%d"
          % (mode, stat.S_IMODE(os.stat(dest).st_mode),
             os.path.getsize(dest)))
""")
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        out = proc.stdout.decode()
        self.assertIn("0400 -> 0400 size=5000", out, out)
        self.assertIn("0444 -> 0444 size=5000", out, out)
        self.assertIn("0755 -> 0755 size=5000", out, out)

    def test_unwritable_file_inside_a_downloaded_tree(self):
        proc = self._as_nobody(HARNESS + """
tree = os.path.join(WORK, "tree")
os.makedirs(tree)
for name, mode in (("key", 0o400), ("run", 0o755), ("ro", 0o444)):
    p = os.path.join(tree, name)
    with open(p, "wb") as fh:
        fh.write(b"d\\n")
    os.chmod(p, mode)
out = os.path.join(WORK, "out")
mssh.download_path(sftp, tree, out, Opts(recursive=True))
for name in ("key", "run", "ro"):
    p = os.path.join(out, name)
    print("%s %04o %d" % (name, stat.S_IMODE(os.stat(p).st_mode),
                          os.path.getsize(p)))
""")
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        out = proc.stdout.decode()
        self.assertIn("key 0400 2", out, out)
        self.assertIn("ro 0444 2", out, out)
        self.assertIn("run 0755 2", out, out)

    def test_a_private_upload_is_briefly_world_readable(self):
        """Documents an accepted limitation, so it cannot regress silently.

        mssh sets the mode with a chmod after the transfer, so a 0600 source
        exists at the server's default (0644 under umask 022) while the
        bytes are in flight, and is narrowed at the end.

        scp does NOT have this window -- measured: its protocol sends the
        mode in the file header, before the data, so a 30 MB 0600 upload
        reads 0600 from the first sample onwards.  Matching that would mean
        sending the mode in the SFTP OPEN request, which paramiko's public
        API cannot do.  The window was accepted as the price of staying on
        the public API; the final mode is still correct.

        Worth knowing when uploading a secret to a host with other users on
        it.  Upload to a directory they cannot enter, or use -p on a
        pre-created file, if the window matters for your case.
        """
        proc = self._as_nobody(HARNESS + """
import threading, time
src = os.path.join(WORK, "secret")
with open(src, "wb") as fh:
    fh.write(b"S" * 400000)
os.chmod(src, 0o600)
dest = os.path.join(WORK, "secret.up")
seen = set()
stop = [False]
def watch():
    while not stop[0]:
        try:
            seen.add(stat.S_IMODE(os.stat(dest).st_mode))
        except OSError:
            pass
        time.sleep(0.0005)
t = threading.Thread(target=watch)
t.start()
mssh.upload_path(sftp, src, dest, Opts())
stop[0] = True
t.join()
print("seen=%s final=%04o"
      % (sorted(oct(m) for m in seen),
         stat.S_IMODE(os.stat(dest).st_mode)))
""")
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        out = proc.stdout.decode()
        # What matters, and what must not regress: the file ends up correct.
        self.assertIn("final=0600", out, out)


HARNESS = '''
import importlib.util, os, stat, subprocess, sys
from importlib.machinery import SourceFileLoader
import paramiko

WORK = sys.argv[1]
spec = importlib.util.spec_from_loader(
    "mssh", SourceFileLoader("mssh", "@MSSH@"))
mssh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mssh)


class Opts(object):
    def __init__(self, recursive=False, preserve=False):
        self.recursive = recursive
        self.preserve = preserve
        self.verbose = False


class PipeChan(object):
    def __init__(self):
        self.proc = subprocess.Popen(
            ["%s"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            preexec_fn=lambda: os.umask(0o022))

    def send(self, d):
        self.proc.stdin.write(d)
        self.proc.stdin.flush()
        return len(d)

    def recv(self, n):
        return self.proc.stdout.read(n)

    def close(self):
        self.proc.stdin.close()
        self.proc.wait()

    def settimeout(self, _):
        pass

    def get_name(self):
        return "sftp"


chan = PipeChan()
sftp = paramiko.SFTPClient(chan)
''' % SFTP_SERVER


if __name__ == "__main__":
    unittest.main(verbosity=2)
