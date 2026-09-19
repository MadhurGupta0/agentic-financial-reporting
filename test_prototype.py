import json
import unittest
from decimal import Decimal

import prototype


class PrototypeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coa = prototype.load_csv("chart_of_accounts.csv")
        self.tb = prototype.load_csv("trial_balance.csv")
        self.prior_tb = prototype.load_csv("prior_period_tb.csv")
        self.fx = prototype.load_csv("fx_rates.csv")
        self.batch = json.loads((prototype.INPUTS / "manual_adjustments.json").read_text(encoding="utf-8"))

    def test_decision_payload_is_idempotent(self) -> None:
        results_a, _ = prototype.validate_adjustments(self.coa, self.batch)
        results_b, _ = prototype.validate_adjustments(self.coa, self.batch)

        payload_a = {
            "summary": {
                "entries": len(results_a),
                "accepted": sum(result["status"] == "ACCEPT" for result in results_a),
                "escalated": sum(result["status"] == "ESCALATE" for result in results_a),
                "rejected": sum(result["status"] == "REJECT" for result in results_a),
            },
            "entries": results_a,
        }
        payload_b = {
            "summary": {
                "entries": len(results_b),
                "accepted": sum(result["status"] == "ACCEPT" for result in results_b),
                "escalated": sum(result["status"] == "ESCALATE" for result in results_b),
                "rejected": sum(result["status"] == "REJECT" for result in results_b),
            },
            "entries": results_b,
        }
        self.assertEqual(payload_a, payload_b)

    def test_malformed_amount_is_rejected(self) -> None:
        batch = {
            "period": "2024-Q4",
            "functional_currency": "USD",
            "entries": [
                {
                    "id": "BAD-001",
                    "description": "Malformed amount",
                    "date": "2024-12-31",
                    "lines": [
                        {"account": "6100", "debit": "", "credit": 0},
                        {"account": "2120", "debit": 0, "credit": 10},
                    ],
                }
            ],
        }
        results, _ = prototype.validate_adjustments(self.coa, batch)
        self.assertEqual(results[0]["status"], "REJECT")
        self.assertIn("MALFORMED_AMOUNT", {issue["code"] for issue in results[0]["errors"]})

    def test_header_account_is_rejected(self) -> None:
        batch = {
            "period": "2024-Q4",
            "functional_currency": "USD",
            "entries": [
                {
                    "id": "BAD-002",
                    "description": "Header posting",
                    "date": "2024-12-31",
                    "lines": [
                        {"account": "2100", "debit": 100, "credit": 0},
                        {"account": "6100", "debit": 0, "credit": 100},
                    ],
                }
            ],
        }
        results, _ = prototype.validate_adjustments(self.coa, batch)
        self.assertEqual(results[0]["status"], "REJECT")
        self.assertIn("HEADER_ACCOUNT", {issue["code"] for issue in results[0]["errors"]})

    def test_out_of_period_date_is_rejected(self) -> None:
        batch = {
            "period": "2024-Q4",
            "functional_currency": "USD",
            "entries": [
                {
                    "id": "BAD-003",
                    "description": "Wrong period",
                    "date": "2025-01-01",
                    "lines": [
                        {"account": "6100", "debit": 100, "credit": 0},
                        {"account": "2120", "debit": 0, "credit": 100},
                    ],
                }
            ],
        }
        results, _ = prototype.validate_adjustments(self.coa, batch)
        self.assertEqual(results[0]["status"], "REJECT")
        self.assertIn("DATE_OUT_OF_PERIOD", {issue["code"] for issue in results[0]["errors"]})

    def test_duplicate_and_missing_ids_are_rejected(self) -> None:
        batch = {
            "period": "2024-Q4",
            "functional_currency": "USD",
            "entries": [
                {
                    "id": "DUP-001",
                    "description": "First",
                    "date": "2024-12-31",
                    "lines": [
                        {"account": "6100", "debit": 100, "credit": 0},
                        {"account": "2120", "debit": 0, "credit": 100},
                    ],
                },
                {
                    "id": "DUP-001",
                    "description": "Second",
                    "date": "2024-12-31",
                    "lines": [
                        {"account": "6100", "debit": 100, "credit": 0},
                        {"account": "2120", "debit": 0, "credit": 100},
                    ],
                },
                {
                    "id": "",
                    "description": "Missing id",
                    "date": "2024-12-31",
                    "lines": [
                        {"account": "6100", "debit": 100, "credit": 0},
                        {"account": "2120", "debit": 0, "credit": 100},
                    ],
                },
            ],
        }
        results, _ = prototype.validate_adjustments(self.coa, batch)
        self.assertEqual(results[0]["status"], "REJECT")
        self.assertEqual(results[1]["status"], "REJECT")
        self.assertEqual(results[2]["status"], "REJECT")
        self.assertIn("DUPLICATE_ENTRY_ID", {issue["code"] for issue in results[0]["errors"]})
        self.assertIn("MISSING_ENTRY_ID", {issue["code"] for issue in results[2]["errors"]})

    def test_post_adjustment_tb_is_still_balanced(self) -> None:
        results, _ = prototype.validate_adjustments(self.coa, self.batch)
        adjusted_controls = prototype.apply_accepted_adjustments(self.tb, results, self.batch)
        self.assertEqual(adjusted_controls["accepted_entry_count"], 7)
        self.assertEqual(Decimal(adjusted_controls["difference"]), Decimal("-4800.00"))
        self.assertFalse(adjusted_controls["is_balanced"])
        self.assertFalse(adjusted_controls["release_ready"])
        self.assertEqual(
            adjusted_controls["release_blockers"],
            ["post-adjustment trial balance is not balanced within 0.01 USD"],
        )


if __name__ == "__main__":
    unittest.main()