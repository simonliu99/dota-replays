"""Cases for the shared test helpers."""

import socket
import unittest

from tests.support import closed_port


class ClosedPortTestCase(unittest.TestCase):
    def test_the_closed_port_refuses_a_connection(self):
        # The point at once, not after a timeout: a client the cases point at
        # this port must fail a connection, as a refused one does.
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", closed_port()), timeout=10)


if __name__ == "__main__":
    unittest.main()
