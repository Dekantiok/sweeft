"""Governance module tests (chain reads mocked, no network)."""
import base64
import json
from unittest import mock

from django.test import TestCase, override_settings

from . import gov

POLICY = {
    "roles": [
        {"name": "all", "kind": "Everyone", "permissions": ["*:AddProposal"], "vote_policy": {}},
        {
            "name": "council",
            "kind": {"Group": ["a.testnet", "b.testnet", "c.testnet"]},
            "permissions": ["*:*"],
            "vote_policy": {},
        },
    ],
    "default_vote_policy": {"weight_kind": "RoleWeight", "quorum": "0", "threshold": [1, 2]},
    "proposal_bond": "100000000000000000000000",
    "proposal_period": "604800000000000",
    "bounty_bond": "100000000000000000000000",
    "bounty_forgiveness_period": "86400000000000",
}


def _proposal(pid, escrow_id, pct, status="InProgress"):
    payload = base64.b64encode(json.dumps({"escrow_id": escrow_id, "seller_pct": pct}).encode()).decode()
    return {
        "id": pid,
        "proposer": "steepdata3968.testnet",
        "description": f"Resolve {escrow_id}",
        "kind": {
            "FunctionCall": {
                "receiver_id": "sweeft-final2.testnet",
                "actions": [
                    {"method_name": "resolve_review", "args": payload, "deposit": "0", "gas": "100000000000000"}
                ],
            }
        },
        "status": status,
        "votes": {"steepdata3968.testnet": "Approve"},
        "vote_counts": {},
        "submission_time": "1",
    }


class GovHelperTests(TestCase):
    def test_council_info_threshold(self):
        info = gov.council_info(POLICY)
        self.assertEqual(info["members"], ["a.testnet", "b.testnet", "c.testnet"])
        self.assertEqual(info["votes_needed"], 2)  # ceil(3 * 1/2)

    def test_council_info_empty(self):
        info = gov.council_info(None)
        self.assertEqual(info["members"], [])
        self.assertEqual(info["votes_needed"], 0)

    @override_settings(SWEEFT_CONTRACT="sweeft-final2.testnet")
    def test_proposal_target_decoding(self):
        target = gov.proposal_review_target(_proposal(3, "SWFT-000012", 60))
        self.assertEqual(target, {"escrow_id": "SWFT-000012", "seller_pct": 60})

    @override_settings(SWEEFT_CONTRACT="sweeft-final2.testnet")
    def test_proposals_for_escrow_filters(self):
        proposals = [_proposal(1, "SWFT-000012", 60), _proposal(2, "SWFT-000013", 10)]
        out = gov.proposals_for_escrow("SWFT-000012", proposals)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["review"]["seller_pct"], 60)

    @override_settings(SWEEFT_CONTRACT="sweeft-final2.testnet")
    def test_ignores_other_receivers(self):
        p = _proposal(4, "SWFT-000012", 50)
        p["kind"]["FunctionCall"]["receiver_id"] = "other.testnet"
        self.assertIsNone(gov.proposal_review_target(p))

    @override_settings(SWEEFT_CONTRACT="sweeft-final2.testnet")
    def test_review_proposal_args_shape(self):
        args = gov.review_proposal_args("SWFT-000012", 65)
        action = args["proposal"]["kind"]["FunctionCall"]["actions"][0]
        self.assertEqual(action["method_name"], "resolve_review")
        self.assertEqual(args["proposal"]["kind"]["FunctionCall"]["receiver_id"], "sweeft-final2.testnet")
        decoded = json.loads(base64.b64decode(action["args"]))
        self.assertEqual(decoded, {"escrow_id": "SWFT-000012", "seller_pct": 65})

    def test_bond_parse(self):
        self.assertEqual(gov.proposal_bond_yocto(POLICY), 10**23)
        self.assertEqual(gov.proposal_bond_yocto(None), 0)


class GovConsoleTests(TestCase):
    def _snapshot(self):
        return {
            "dao": "sweeft-board.testnet",
            "council": {"members": ["a.testnet", "b.testnet", "c.testnet"], "threshold": [1, 2], "votes_needed": 2},
            "policy": {},
            "proposal_bond_display": "0.1",
            "proposal_bond_yocto": "100000000000000000000000",
            "proposals": [],
            "active_count": 0,
            "treasury_near_display": "10",
            "gov": {"arbiter": "sweeft-board.testnet", "treasury": "sweeft-board.testnet"},
        }

    def test_console_renders(self):
        with mock.patch("apps.escrow.gov.summary", return_value=self._snapshot()):
            res = self.client.get("/governance/")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn("Review board", body)
        self.assertIn("sweeft-board.testnet", body)

    def test_console_marks_council_member(self):
        session = self.client.session
        session["wallet"] = "b.testnet"
        session.save()
        with mock.patch("apps.escrow.gov.summary", return_value=self._snapshot()):
            res = self.client.get("/governance/")
        self.assertTrue(res.context["is_council"])
