"""
Production Test Suite for PayPal Conditional Autopay — AI Mandate Firewall
Verifies:
1. Clean database initialization (zero pre-seeded rules or balances).
2. Natural-language rule parsing (heuristic & regex extraction including emails).
3. Rule lifecycle: creation, listing, pause/resume toggling, and deletion.
4. Real webhook ingestion via /api/webhooks/external:
   - Fair bill ($45 <= $50 cap) -> Approved and settled.
   - Spiked bill ($85 > $50 cap) -> Blocked by AI Firewall, $0 moved, protected metric incremented.
5. Human-in-the-Loop Override & Rejection:
   - Force-capture override of blocked charge.
   - Permanent dispute and rejection.
6. Real PayPal Webhook ingestion via /api/webhooks/paypal:
   - Full paycheck (+$1,200) -> Triggers downstream rent payment ($500).
   - Partial paycheck (+$600) -> Insufficient trigger, held in place to protect against overdraft.
7. PayPal Client & Settings:
   - Environment switching, OAuth credential error handling, vault token generation.
8. New hardening guards:
   - Rule payload validation and forced server-side status.
   - Single-use override (no double charge), lifecycle toggle/re-arm guards.
   - Parser negation handling and /api/state secret redaction.
   - Idempotent demo seed/reset controls.
"""

import unittest
import json
import os
import database as db
import paypal_client
import ai_engine
from app import app


class TestPayPalConditionalAutopayProduction(unittest.TestCase):

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        # Reset DB by removing file and re-initializing clean schema
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        db.init_db()

    def tearDown(self):
        pass

    def test_01_clean_production_initial_state(self):
        """Verifies clean initial state: 0 rules, 0 fake balance, disconnected initial account."""
        state_res = self.client.get("/api/state")
        self.assertEqual(state_res.status_code, 200)
        data = state_res.get_json()

        self.assertTrue(data["success"])
        self.assertEqual(len(data["rules"]), 0)
        self.assertEqual(data["metrics"]["active_mandates"], 0)
        self.assertEqual(data["metrics"]["total_saved_by_ai"], 0.0)
        self.assertEqual(data["metrics"]["total_auto_settled"], 0.0)
        self.assertEqual(data["account"]["connected_status"], "DISCONNECTED")

    def test_02_natural_language_rule_parser(self):
        """Verifies rule parsing and entity extraction from plain English."""
        # Case A: Paycheck chain reaction with email
        res_a = ai_engine.parse_natural_language_rule(
            "Pay landlord@metroprop.com $500 for rent ONLY AFTER incoming paycheck of at least $1,000 arrives"
        )
        self.assertEqual(res_a["rule_category"], "CHAIN_REACTION")
        self.assertEqual(res_a["recipient_paypal_email"], "landlord@metroprop.com")
        self.assertEqual(res_a["payment_amount"], 500.0)
        self.assertEqual(res_a["condition_operator"], ">=")
        self.assertEqual(res_a["condition_threshold"], 1000.0)

        # Case B: Bill cap with email
        res_b = ai_engine.parse_natural_language_rule(
            "Auto-pay invoices from billing@speednet.com only if total is $50 or less"
        )
        self.assertEqual(res_b["rule_category"], "BILL_CAP")
        self.assertEqual(res_b["recipient_paypal_email"], "billing@speednet.com")
        self.assertEqual(res_b["payment_amount"], 50.0)
        self.assertEqual(res_b["condition_operator"], "<=")
        self.assertEqual(res_b["condition_threshold"], 50.0)

    def test_03_rule_crud_and_pause_toggle(self):
        """Verifies rule creation, pause/resume toggling, and deletion."""
        rule_payload = {
            "title": "Wi-Fi Monthly Cap",
            "natural_language_rule": "Auto-pay invoices from billing@speednet.com only if total is $50 or less",
            "rule_category": "BILL_CAP",
            "trigger_source": "SpeedNet Wi-Fi",
            "recipient_paypal_email": "billing@speednet.com",
            "recipient_name": "SpeedNet Broadband",
            "payment_amount": 45.0,
            "currency": "USD",
            "condition_operator": "<=",
            "condition_threshold": 50.0
        }
        create_res = self.client.post("/api/rules", json=rule_payload)
        self.assertEqual(create_res.status_code, 200)
        rule = create_res.get_json()["rule"]
        self.assertEqual(rule["status"], "ACTIVE_WAITING")

        # Toggle pause
        toggle_res = self.client.patch(f"/api/rules/{rule['id']}/toggle")
        self.assertEqual(toggle_res.status_code, 200)
        self.assertEqual(toggle_res.get_json()["rule"]["status"], "PAUSED")

        # Toggle resume
        toggle_res2 = self.client.patch(f"/api/rules/{rule['id']}/toggle")
        self.assertEqual(toggle_res2.status_code, 200)
        self.assertEqual(toggle_res2.get_json()["rule"]["status"], "ACTIVE_WAITING")

        # Delete rule
        del_res = self.client.delete(f"/api/rules/{rule['id']}")
        self.assertEqual(del_res.status_code, 200)
        self.assertIsNone(db.get_rule_by_id(rule["id"]))

    def test_04_external_webhook_bill_cap_evaluation(self):
        """Verifies fair invoice capture vs. price-spike blocking."""
        # Create rule with cap $50.00
        rule = db.create_rule({
            "title": "SpeedNet Wi-Fi Cap",
            "natural_language_rule": "Auto-pay Wi-Fi bill only if $50 or less",
            "rule_category": "BILL_CAP",
            "trigger_source": "SpeedNet Wi-Fi",
            "recipient_paypal_email": "billing@speednet.com",
            "recipient_name": "SpeedNet Broadband",
            "payment_amount": 45.0,
            "currency": "USD",
            "condition_operator": "<=",
            "condition_threshold": 50.0
        })

        # 1. Ingest fair invoice ($45.00)
        fair_res = self.client.post("/api/webhooks/external", json={
            "trigger_source": "SpeedNet Wi-Fi",
            "amount": 45.0,
            "event_type": "INVOICE_BILLING"
        })
        self.assertEqual(fair_res.status_code, 200)
        fair_eval = fair_res.get_json()["evaluation"]["results"][0]
        self.assertEqual(fair_eval["verdict"], "CAPTURED")
        # Bills recur: the mandate stays armed after a successful payment.
        self.assertEqual(db.get_rule_by_id(rule["id"])["status"], "ACTIVE_WAITING")
        self.assertEqual(db.get_rule_by_id(rule["id"])["execution_count"], 1)

        # 2. Ingest spiked invoice ($85.00) without manually re-arming.
        spike_res = self.client.post("/api/webhooks/external", json={
            "trigger_source": "SpeedNet Wi-Fi",
            "amount": 85.0,
            "event_type": "INVOICE_BILLING"
        })
        self.assertEqual(spike_res.status_code, 200)
        spike_eval = spike_res.get_json()["evaluation"]["results"][0]
        self.assertEqual(spike_eval["verdict"], "BLOCKED")

        # Verify rule is BLOCKED_BY_AI and metric saved incremented
        updated_rule = db.get_rule_by_id(rule["id"])
        self.assertEqual(updated_rule["status"], "BLOCKED_BY_AI")
        self.assertEqual(updated_rule["pending_invoice_amount"], 85.0)
        account = db.get_account()
        self.assertEqual(account["total_saved_by_ai"], 85.0)

    def test_05_human_in_the_loop_override_and_dispute(self):
        """Verifies manual override authorization and dispute actions on blocked bills."""
        rule = db.create_rule({
            "title": "Spiked Wi-Fi",
            "natural_language_rule": "Cap $50",
            "rule_category": "BILL_CAP",
            "trigger_source": "SpeedNet Wi-Fi",
            "recipient_paypal_email": "billing@speednet.com",
            "recipient_name": "SpeedNet Broadband",
            "payment_amount": 45.0,
            "condition_operator": "<=",
            "condition_threshold": 50.0,
            "status": "BLOCKED_BY_AI",
            "pending_invoice_amount": 85.0
        })

        # Test Override
        override_res = self.client.post(f"/api/rules/{rule['id']}/override")
        self.assertEqual(override_res.status_code, 200)
        self.assertEqual(override_res.get_json()["result"]["rule"]["status"], "OVERRIDDEN")
        self.assertEqual(db.get_account()["total_auto_settled"], 85.0)

        # Test Reject / Dispute on another rule
        rule2 = db.create_rule({
            "title": "Unauthorized Charge",
            "natural_language_rule": "Cap $20",
            "rule_category": "BILL_CAP",
            "trigger_source": "Unknown Vendor",
            "recipient_paypal_email": "fraud@merchant.com",
            "recipient_name": "Unknown",
            "payment_amount": 10.0,
            "condition_operator": "<=",
            "condition_threshold": 20.0,
            "status": "BLOCKED_BY_AI",
            "pending_invoice_amount": 99.0
        })
        reject_res = self.client.post(f"/api/rules/{rule2['id']}/reject")
        self.assertEqual(reject_res.status_code, 200)
        self.assertEqual(reject_res.get_json()["result"]["rule"]["status"], "REJECTED")

    def test_06_paypal_webhook_chain_reaction(self):
        """Verifies incoming PayPal PAYMENT.CAPTURE.COMPLETED chain reaction execution vs. hold."""
        rule = db.create_rule({
            "title": "Payday First, Rent Second",
            "natural_language_rule": "Pay landlord@metroprop.com $500 once paycheck of at least $1,000 arrives",
            "rule_category": "CHAIN_REACTION",
            "trigger_source": "Employer Paycheck",
            "recipient_paypal_email": "landlord@metroprop.com",
            "recipient_name": "Metro Properties",
            "payment_amount": 500.0,
            "condition_operator": ">=",
            "condition_threshold": 1000.0
        })

        # 1. Partial Paycheck ($600 < $1,000 threshold)
        partial_webhook = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "summary": "Employer Paycheck ACH Deposit",
            "resource": {
                "amount": {"value": "600.00", "currency_code": "USD"}
            }
        }
        res_partial = self.client.post("/api/webhooks/paypal", json=partial_webhook)
        self.assertEqual(res_partial.status_code, 200)
        partial_eval = res_partial.get_json()["evaluation"]["results"][0]
        self.assertEqual(partial_eval["verdict"], "HOLD_INSUFFICIENT_TRIGGER")
        self.assertEqual(db.get_rule_by_id(rule["id"])["status"], "ACTIVE_WAITING")

        # 2. Full Paycheck ($1,200 >= $1,000 threshold)
        full_webhook = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "summary": "Employer Paycheck ACH Deposit",
            "resource": {
                "amount": {"value": "1200.00", "currency_code": "USD"}
            }
        }
        res_full = self.client.post("/api/webhooks/paypal", json=full_webhook)
        self.assertEqual(res_full.status_code, 200)
        full_eval = res_full.get_json()["evaluation"]["results"][0]
        self.assertEqual(full_eval["verdict"], "CAPTURED")
        self.assertEqual(db.get_rule_by_id(rule["id"])["status"], "EXECUTED")

    def test_07_paypal_client_and_settings(self):
        """Verifies PayPal OAuth token error handling and vault setup token generation."""
        # Invalid credentials should return clean error without crashing
        res = paypal_client.test_connection("invalid_client_id", "invalid_secret", "sandbox")
        self.assertFalse(res.get("success"))

        # Vault token generation
        vault_res = paypal_client.create_vault_setup_token("", "", "sandbox")
        self.assertTrue(vault_res.get("success"))
        self.assertTrue(vault_res["vault_setup_token"].startswith("vt_setup_"))
        self.assertTrue(vault_res.get("simulated"))

        # Save settings via API
        settings_res = self.client.post("/api/settings/paypal", json={
            "environment": "sandbox",
            "client_id": "",
            "client_secret": "",
            "merchant_email": "test@merchant.com",
            "gemini_api_key": ""
        })
        self.assertEqual(settings_res.status_code, 200)
        self.assertEqual(settings_res.get_json()["connected_status"], "DISCONNECTED")

    def test_08_rule_validation_and_forged_status(self):
        """Rejects malformed mandates and never lets a client forge a status."""
        bad_res = self.client.post("/api/rules", json={
            "title": "",
            "payment_amount": -500,
            "condition_threshold": -1,
            "condition_operator": "hax",
            "recipient_paypal_email": "not-an-email"
        })
        self.assertEqual(bad_res.status_code, 400)

        forged_res = self.client.post("/api/rules", json={
            "title": "Forged Settlement",
            "rule_category": "BILL_CAP",
            "trigger_source": "Vendor",
            "recipient_name": "Vendor",
            "recipient_paypal_email": "vendor@example.com",
            "payment_amount": 0,
            "condition_operator": "<=",
            "condition_threshold": 10,
            "status": "EXECUTED"
        })
        self.assertEqual(forged_res.status_code, 200)
        self.assertEqual(forged_res.get_json()["rule"]["status"], "ACTIVE_WAITING")

    def test_09_double_override_guard(self):
        """A blocked mandate can only be force-captured once (no double charge)."""
        rule = db.create_rule({
            "title": "Blocked Once",
            "natural_language_rule": "Cap $50",
            "rule_category": "BILL_CAP",
            "trigger_source": "SpeedNet Wi-Fi",
            "recipient_paypal_email": "billing@speednet.com",
            "recipient_name": "SpeedNet Broadband",
            "payment_amount": 45.0,
            "condition_operator": "<=",
            "condition_threshold": 50.0,
            "status": "BLOCKED_BY_AI",
            "pending_invoice_amount": 85.0
        })

        first = self.client.post(f"/api/rules/{rule['id']}/override")
        self.assertEqual(first.status_code, 200)

        second = self.client.post(f"/api/rules/{rule['id']}/override")
        self.assertEqual(second.status_code, 400)
        self.assertEqual(db.get_account()["total_auto_settled"], 85.0)
        self.assertGreaterEqual(db.get_account()["total_saved_by_ai"], 0.0)

    def test_10_toggle_and_rearm_state_guards(self):
        """Pause/resume only applies to ACTIVE/PAUSED; finished rules need re-arm."""
        rule = db.create_rule({
            "title": "Lifecycle Guard",
            "natural_language_rule": "Cap $50",
            "rule_category": "BILL_CAP",
            "trigger_source": "SpeedNet Wi-Fi",
            "recipient_paypal_email": "billing@speednet.com",
            "recipient_name": "SpeedNet Broadband",
            "payment_amount": 45.0,
            "condition_operator": "<=",
            "condition_threshold": 50.0
        })

        self.assertEqual(self.client.patch(f"/api/rules/{rule['id']}/toggle").status_code, 200)
        self.assertEqual(db.get_rule_by_id(rule["id"])["status"], "PAUSED")
        self.assertEqual(self.client.patch(f"/api/rules/{rule['id']}/toggle").status_code, 200)

        db.update_rule_status(rule["id"], "EXECUTED")
        blocked_toggle = self.client.patch(f"/api/rules/{rule['id']}/toggle")
        self.assertEqual(blocked_toggle.status_code, 400)
        self.assertEqual(db.get_rule_by_id(rule["id"])["status"], "EXECUTED")

        rearm = self.client.post(f"/api/rules/{rule['id']}/rearm")
        self.assertEqual(rearm.status_code, 200)
        self.assertEqual(rearm.get_json()["rule"]["status"], "ACTIVE_WAITING")

    def test_11_parser_negation_and_no_secret_leak(self):
        """'No more than' must mean <= and /api/state must not expose the webhook secret."""
        parsed = ai_engine.parse_natural_language_rule("Pay my Wi-Fi bill no more than $50")
        self.assertEqual(parsed["condition_operator"], "<=")
        self.assertEqual(parsed["rule_category"], "BILL_CAP")
        self.assertEqual(parsed["trigger_source"], "SpeedNet Wi-Fi")

        state = self.client.get("/api/state").get_json()
        self.assertNotIn("webhook_secret", state["config"])
        self.assertTrue(state["config"]["webhook_configured"])
        self.assertTrue(state["demo_mode"])

    def test_12_demo_seed_and_reset(self):
        """Demo controls are idempotent and reset clears rules, logs and totals."""
        first = self.client.post("/api/demo/seed").get_json()
        self.assertEqual(first["created"], 2)

        second = self.client.post("/api/demo/seed").get_json()
        self.assertEqual(second["created"], 0)

        self.client.post("/api/demo/reset")
        state = self.client.get("/api/state").get_json()
        self.assertEqual(len(state["rules"]), 0)
        self.assertEqual(state["metrics"]["total_saved_by_ai"], 0.0)
        self.assertEqual(state["metrics"]["total_auto_settled"], 0.0)


if __name__ == "__main__":
    unittest.main()
