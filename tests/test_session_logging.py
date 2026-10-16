"""Session stderr logging, compatible with Python 3.6."""

import importlib.util
import io
import logging
import os
import threading
import types
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock


PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mssh")
SPEC = importlib.util.spec_from_loader(
    "mssh_logging", SourceFileLoader("mssh_logging", PATH))
mssh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mssh)


class TestSessionLogging(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.patch = mock.patch.object(mssh.sys, "stderr", self.output)
        self.patch.start()
        self.loggers = {
            name: (list(logging.getLogger(name).handlers),
                   logging.getLogger(name).level,
                   logging.getLogger(name).disabled,
                   logging.getLogger(name).propagate)
            for name in ("paramiko", "mssh.session")}
        mssh._SESSION_LOG_ENABLED = True
        mssh._SESSION_LOG_NAME = "test"
        mssh._setup_logging(types.SimpleNamespace(verbose=3))

    def tearDown(self):
        mssh._SESSION_LOG_ENABLED = False
        mssh._SESSION_LOG_NAME = None
        mssh._SESSION_LOG_CONTEXT.__dict__.clear()
        for name, state in self.loggers.items():
            logger = logging.getLogger(name)
            logger.handlers, logger.level, logger.disabled, logger.propagate = state
        self.patch.stop()

    def test_original_and_saved_exception_stacks(self):
        transport = mock.Mock()
        transport.is_active.return_value = False
        transport.sock.fileno.return_value = -1
        try:
            raise OSError(9, "event pipe closed")
        except OSError as exc:
            transport.saved_exception = exc
        mssh._SESSION_LOG_CONTEXT.request = "abcd"
        mssh._SESSION_LOG_CONTEXT.op = "sftp"
        mssh._session_log("sftp.failed", logging.ERROR,
                          types.SimpleNamespace(transport=transport),
                          exc=RuntimeError("relay failed"))
        text = self.output.getvalue()
        for value in ("session=test", "request=abcd", "op=sftp", "pid=",
                      "thread=", "event pipe closed", "relay failed",
                      "raise OSError", "transport.saved_exception"):
            self.assertIn(value, text)
        transport.get_exception.assert_not_called()

    def test_paramiko_error_captures_current_exception(self):
        try:
            raise OSError(9, "Bad file descriptor")
        except OSError:
            logging.getLogger("paramiko.transport").error("Socket exception")
        self.assertIn("Traceback", self.output.getvalue())
        self.assertIn("raise OSError", self.output.getvalue())

    def test_reconfigure_does_not_duplicate_handlers(self):
        mssh._setup_logging(types.SimpleNamespace(verbose=3))
        mssh._session_log("once")
        self.assertEqual(self.output.getvalue().count("once"), 1)
        self.assertIs(logging.getLogger("paramiko").handlers[0],
                      logging.getLogger("mssh.session").handlers[0])

    def test_quiet_mode_suppresses_child_paramiko_logger(self):
        child = logging.getLogger("paramiko.transport")
        previous = child.level
        try:
            child.setLevel(logging.DEBUG)
            mssh._setup_logging(types.SimpleNamespace(verbose=False))
            child.error("not visible")
            mssh._session_log("not visible")
            self.assertEqual(self.output.getvalue(), "")
        finally:
            child.setLevel(previous)

    def test_context_is_thread_local(self):
        mssh._SESSION_LOG_CONTEXT.request = "main-request"
        def worker():
            mssh._SESSION_LOG_CONTEXT.request = "worker-request"
            mssh._session_log("worker")
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        mssh._session_log("main")
        lines = self.output.getvalue().splitlines()
        self.assertIn("request=worker-request", lines[0])
        self.assertIn("request=main-request", lines[1])

    def test_verbosity_filters_both_loggers_and_hop_output(self):
        for count, minimum in ((0, logging.CRITICAL + 1),
                               (1, logging.WARNING),
                               (2, logging.INFO), (3, logging.DEBUG),
                               (4, logging.DEBUG)):
            with self.subTest(count=count):
                self.output.seek(0)
                self.output.truncate()
                opts = types.SimpleNamespace(verbose=count)
                mssh._setup_logging(opts)
                for level in (logging.DEBUG, logging.INFO, logging.WARNING,
                              logging.ERROR, logging.CRITICAL):
                    mssh._session_log("daemon-level-%d" % level, level)
                    logging.getLogger("paramiko.transport").log(
                        level, "paramiko-level-%d" % level)
                mssh.verbose(opts, "hop-info")
                text = self.output.getvalue()
                for level in (logging.DEBUG, logging.INFO, logging.WARNING,
                              logging.ERROR, logging.CRITICAL):
                    for prefix in ("daemon", "paramiko"):
                        self.assertEqual(
                            "%s-level-%d" % (prefix, level) in text,
                            level >= minimum)
                self.assertEqual("hop-info" in text, count > 0)

    def test_parser_counts_verbose_flags(self):
        parser = mssh.build_parser()
        for flags, count in (([], 0), (["-v"], 1), (["-vv"], 2),
                             (["-vvv"], 3),
                             (["--verbose", "--verbose"], 2)):
            self.assertEqual(parser.parse_args(flags).verbose, count)


if __name__ == "__main__":
    unittest.main()
