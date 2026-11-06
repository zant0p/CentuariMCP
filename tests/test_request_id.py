"""Tests for the RequestID handling in the SDCP client.

The printer does not always echo the RequestID back verbatim: when the
request's "From" field is non-zero it appends a byte equal to that value to
the echoed RequestID. A client that correlates replies on exact equality
drops that reply and the command times out.

Run with: python3 -m unittest discover -s tests
"""

import asyncio
import json
import unittest

from centauri_mcp.printer_client import (
    _new_request_id,
    _strip_trailing_control,
    CentauriClient,
)


class RequestIdTests(unittest.TestCase):
    def test_new_request_id_is_16_hex_chars(self):
        """Reference implementations all send a short hex id."""
        rid = _new_request_id()
        self.assertEqual(len(rid), 16)
        int(rid, 16)  # raises if it is not hex

    def test_new_request_ids_differ(self):
        self.assertNotEqual(_new_request_id(), _new_request_id())

    def test_strip_trailing_control_removes_appended_byte(self):
        rid = _new_request_id()
        self.assertEqual(_strip_trailing_control(rid + chr(1)), rid)
        self.assertEqual(_strip_trailing_control(rid + chr(2)), rid)

    def test_strip_trailing_control_leaves_clean_id_alone(self):
        rid = _new_request_id()
        self.assertEqual(_strip_trailing_control(rid), rid)


class ResponseCorrelationTests(unittest.TestCase):
    """The reply must be matched even when the echo is mangled."""

    def _connection_with_pending(self, request_id):
        conn = CentauriClient.__new__(CentauriClient)
        conn._pending_requests = {}
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        conn._pending_requests[request_id] = future
        return conn, future, loop

    def _handle(self, conn, echoed_request_id):
        """Run the response branch of the message handler."""
        msg = {
            "Topic": "sdcp/response/test",
            "Data": {"RequestID": echoed_request_id, "Data": {"Ack": 0}},
        }
        asyncio.run(conn._handle_message(json.dumps(msg)))

    def test_exact_match_resolves(self):
        rid = _new_request_id()
        conn, future, loop = self._connection_with_pending(rid)
        try:
            self._handle(conn, rid)
            self.assertTrue(future.done())
            self.assertEqual(future.result()["Data"]["Ack"], 0)
        finally:
            loop.close()

    def test_mangled_echo_still_resolves(self):
        """The printer appends the From byte; the reply must still arrive."""
        rid = _new_request_id()
        conn, future, loop = self._connection_with_pending(rid)
        try:
            self._handle(conn, rid + chr(1))
            self.assertTrue(future.done(), "mangled RequestID was dropped")
            self.assertEqual(future.result()["Data"]["Ack"], 0)
        finally:
            loop.close()

    def test_unrelated_request_id_is_not_resolved(self):
        rid = _new_request_id()
        conn, future, loop = self._connection_with_pending(rid)
        try:
            self._handle(conn, "ffffffffffffffff")
            self.assertFalse(future.done())
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
