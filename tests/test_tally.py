import io
import tempfile
import json
import unittest
from unittest.mock import patch

import server


class ScoreStateTests(unittest.TestCase):
    def test_events_are_idempotent_and_month_floor_is_respected(self):
        state = {
            "cameron": 6, "paget": 3,
            "period": "2026-10",
            "start": {"cameron": 6, "paget": 3},
            "seen": {}, "months": [],
        }
        event = {"id": "offlineTap_0001", "who": "cameron", "delta": 1}
        accepted = server.apply_events(state, [event, event])
        self.assertEqual(accepted, [event["id"], event["id"]])
        self.assertEqual(state["cameron"], 7)

        undo = {"id": "offlineTap_0002", "who": "cameron", "delta": -1}
        server.apply_events(state, [undo])
        floor = {"id": "offlineTap_0003", "who": "cameron", "delta": -1}
        server.apply_events(state, [floor])
        self.assertEqual(state["cameron"], 6)

    def test_month_roll_archives_points_without_resetting_lifetime(self):
        state = {
            "cameron": 18, "paget": 15,
            "period": "2026-10",
            "start": {"cameron": 12, "paget": 13},
            "seen": {}, "months": [],
        }
        with patch.object(server, "current_period", return_value="2026-11"):
            self.assertTrue(server.roll_if_needed(state))
        self.assertEqual(state["months"], [{"period": "2026-10", "cameron": 6, "paget": 2}])
        self.assertEqual((state["cameron"], state["paget"]), (18, 15))
        self.assertEqual(server.month_points(state), {"cameron": 0, "paget": 0})

    def test_state_save_round_trips_to_the_configured_data_file(self):
        with tempfile.TemporaryDirectory() as d, patch.object(server, "DATA", d + "/tally.json"):
            state = {"cameron": 2, "paget": 1, "period": "2026-10",
                     "start": {"cameron": 0, "paget": 0}, "seen": {}, "months": []}
            server.save(state)
            self.assertEqual(server.load(), state)

    def test_update_request_is_queued_without_touching_score_state(self):
        with tempfile.TemporaryDirectory() as d:
            status = d + "/update-status.json"
            request = d + "/update.req"
            with patch.object(server, "UPDATE_DIR", d), \
                 patch.object(server, "UPDATE_STATUS_FILE", status), \
                 patch.object(server, "UPDATE_REQUEST_FILE", request), \
                 patch.object(server, "update_status", return_value={
                     "current": server.APP_VERSION, "latest": "2.1.0", "available": True
                 }):
                ok, message = server.request_update()
                self.assertTrue(ok)
                self.assertIn("queued", message)
                with open(request) as fh:
                    self.assertEqual(json.load(fh)["version"], "2.1.0")
                with open(status) as fh:
                    self.assertEqual(json.load(fh)["state"], "queued")

    def test_semantic_version_ordering(self):
        self.assertGreater(server._version_tuple("2.1.0"), server._version_tuple("2.0.9"))
        self.assertFalse(server._version_tuple("2.0.0") > server._version_tuple("2.0.0"))

    def test_update_check_fetches_version_from_latest_immutable_commit(self):
        sha = "a" * 40
        responses = [io.BytesIO(json.dumps({"sha": sha}).encode()), io.BytesIO(b"2.1.0\n")]
        with tempfile.TemporaryDirectory() as d, \
             patch.object(server, "UPDATE_DIR", d), \
             patch.object(server, "UPDATE_STATUS_FILE", d + "/update-status.json"), \
             patch.object(server.urllib.request, "urlopen", side_effect=responses) as opened:
            status = server.check_update()
        self.assertTrue(status["available"])
        self.assertEqual(status["latest"], "2.1.0")
        self.assertIn("/" + sha + "/VERSION", opened.call_args_list[1].args[0].full_url)


if __name__ == "__main__":
    unittest.main()
