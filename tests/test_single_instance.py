"""6 Nov: a second copy of DCS George AI left running made every UDP listener fail ("Only one usage of each
socket address"), so the new one had no position. Start-up now stops with a plain message instead."""
import os
import socket
import unittest

from tests import fakes  # noqa: F401
from core.telemetry_listener import port_in_use

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PortInUse(unittest.TestCase):
    def test_free_then_taken(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        self.assertTrue(port_in_use(port))
        s.close()
        self.assertFalse(port_in_use(port))

    def test_main_checks_before_starting(self):
        with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
            src = f.read()
        i, j = src.index("if port_in_use(_port):"), src.index('print("[1/5] Starting Export.lua Telemetry Listener...")')
        self.assertLess(i, j)
        self.assertIn("_sys.exit(2)", src[i:j])


if __name__ == "__main__":
    unittest.main()
