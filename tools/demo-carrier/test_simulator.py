"""Demo Carrier simulator tests — run from this directory:

    python3 -m unittest test_simulator -v
"""
import time
import unittest

from simulator import due_events, tick


class DueEventsTests(unittest.TestCase):
    def test_nothing_before_first_step(self):
        self.assertEqual(due_events("delivered", 0, set()), [])

    def test_catch_up_returns_passed_steps_in_order(self):
        statuses = [e["status"] for e in due_events("delivered", 16, set())]
        self.assertEqual(statuses, ["ACCEPTED", "PICKED_UP"])

    def test_posted_statuses_are_not_reposted(self):
        statuses = [e["status"] for e in due_events("delivered", 16, {"ACCEPTED"})]
        self.assertEqual(statuses, ["PICKED_UP"])

    def test_happy_run_delivers_at_60s(self):
        statuses = [e["status"] for e in due_events("delivered", 61, set())]
        self.assertEqual(len(statuses), 5)
        self.assertEqual(statuses[-1], "DELIVERED")

    def test_failed_scenario_ends_failed(self):
        events = due_events("failed", 61, set())
        self.assertEqual(events[-1]["status"], "DELIVERY_FAILED")

    def test_lost_scenario_stops_mid_transit(self):
        statuses = [e["status"] for e in due_events("lost", 120, set())]
        self.assertEqual(statuses, ["ACCEPTED", "PICKED_UP", "LOST"])
        self.assertNotIn("DELIVERED", statuses)

    def test_unknown_scenario_falls_back_to_happy_path(self):
        self.assertEqual(
            [e["status"] for e in due_events("what-even-is-this", 61, set())],
            [e["status"] for e in due_events("delivered", 61, set())],
        )


class StubApi:
    def __init__(self):
        self.posted = []

    def post_event(self, shipment_id, status, note):
        self.posted.append((shipment_id, status, note))
        return {"ok": True}


def shipment(**overrides):
    base = {
        "id": 7,
        "tracking_number": "DEMO-TEST",
        "status": "CREATED",
        "simulation_scenario": "delivered",
        "escrow_state": "Shipped",
        "events": [{"status": "CREATED"}],
    }
    base.update(overrides)
    return base


class TickTests(unittest.TestCase):
    def test_waits_until_dispatched(self):
        api, started = StubApi(), {}
        tick(api, shipment(escrow_state="Funded"), started)
        self.assertEqual(api.posted, [])
        self.assertEqual(started, {})

    def test_posts_only_due_events(self):
        api, started = StubApi(), {"DEMO-TEST": time.monotonic() - 16}
        tick(api, shipment(), started)
        self.assertEqual([s for _, s, _ in api.posted], ["ACCEPTED", "PICKED_UP"])

    def test_no_duplicate_when_event_already_posted(self):
        api, started = StubApi(), {"DEMO-TEST": time.monotonic() - 16}
        tick(api, shipment(events=[{"status": "CREATED"}, {"status": "ACCEPTED"}]), started)
        self.assertEqual([s for _, s, _ in api.posted], ["PICKED_UP"])

    def test_terminal_shipment_is_forgotten(self):
        api, started = StubApi(), {"DEMO-TEST": time.monotonic()}
        tick(api, shipment(status="DELIVERED"), started)
        self.assertEqual(api.posted, [])
        self.assertNotIn("DEMO-TEST", started)

    def test_run_starts_on_first_shipped_sighting(self):
        api, started = StubApi(), {}
        tick(api, shipment(), started)
        self.assertEqual(api.posted, [])  # t0 set just now — nothing due yet
        self.assertIn("DEMO-TEST", started)


if __name__ == "__main__":
    unittest.main()
