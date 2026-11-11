"""
PayPal Conditional Autopay — Production Flask REST API & Webhook Server
Endpoints:
- /api/state: Live connection metrics, active rules, audit ledger
- /api/settings/paypal: Real OAuth2 credential handshake & settings persistence
- /api/paypal/vault-setup: Generate PayPal Vault Setup Tokens
- /api/parse-rule: AI NLP rule extraction
- /api/rules: Mandate CRUD & pause/resume toggling
- /api/webhooks/paypal: Ingest real PayPal events (PAYMENT.CAPTURE.COMPLETED, etc.)
- /api/webhooks/external: Ingest vendor invoices & milestone triggers
- /api/rules/<id>/override & /reject: Human-in-the-loop dispute & override actions
"""

import os
import re
from flask import Flask, render_template, request, jsonify
import database as db
import paypal_client
import ai_engine

app = Flask(__name__)

# Initialize clean production schema
db.init_db()

VALID_CATEGORIES = {"CHAIN_REACTION", "SPLIT_PAYMENT", "BILL_CAP", "MILESTONE", "PRICE_DROP"}
VALID_OPERATORS = {">=", "<=", "=="}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _validate_rule_payload(data: dict):
    """Returns (clean_payload, errors). Rejects forged statuses / bad numbers."""
    errors = []
    title = str(data.get("title") or "").strip()
    rule_text = str(data.get("natural_language_rule") or "").strip()
    category = str(data.get("rule_category") or "BILL_CAP").strip().upper()
    trigger = str(data.get("trigger_source") or "").strip()
    payee_name = str(data.get("recipient_name") or "").strip()
    payee_email = str(data.get("recipient_paypal_email") or "").strip()
    operator = str(data.get("condition_operator") or "<=").strip()

    try:
        payment_amount = float(data.get("payment_amount", 0))
    except (TypeError, ValueError):
        payment_amount = -1
    try:
        threshold = float(data.get("condition_threshold", 0))
    except (TypeError, ValueError):
        threshold = -1

    if not title:
        errors.append("A mandate title is required.")
    if category not in VALID_CATEGORIES:
        errors.append(f"rule_category must be one of {sorted(VALID_CATEGORIES)}.")
    if operator not in VALID_OPERATORS:
        errors.append("condition_operator must be one of >=, <=, ==.")
    if not trigger:
        errors.append("trigger_source is required.")
    if not payee_name:
        errors.append("recipient_name is required.")
    if not EMAIL_RE.match(payee_email):
        errors.append("recipient_paypal_email must be a valid email address.")
    if payment_amount < 0:
        errors.append("payment_amount must be zero or greater.")
    if threshold < 0:
        errors.append("condition_threshold must be zero or greater.")
    if category in {"CHAIN_REACTION", "SPLIT_PAYMENT", "MILESTONE", "PRICE_DROP"} and payment_amount <= 0:
        errors.append("payment_amount must be greater than zero for this mandate type.")
    if category == "BILL_CAP" and payment_amount <= 0:
        # For a cap mandate the threshold is the spending cap; the amount is optional.
        payment_amount = 0.0

    clean = {
        "title": title,
        "natural_language_rule": rule_text,
        "rule_category": category,
        "trigger_source": trigger,
        "recipient_name": payee_name,
        "recipient_paypal_email": payee_email,
        "payment_amount": payment_amount,
        "currency": str(data.get("currency") or "USD").upper()[:3],
        "condition_operator": operator,
        "condition_threshold": threshold,
        # Never trust a client-provided status.
        "status": "ACTIVE_WAITING",
    }
    return clean, errors


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health", methods=["GET"])
def health():
    """Lightweight liveness probe for containers and uptime checks."""
    return jsonify({
        "success": True,
        "status": "ok",
        "service": "paypal-conditional-autopay",
        "demo_mode": db.get_account().get("connected_status") != "CONNECTED"
    })


@app.route("/api/state", methods=["GET"])
def get_state():
    """Returns real-time account status, metrics, active rules, and audit ledger."""
    try:
        account = db.get_account()
        # Mask sensitive client secret in client state
        safe_account = dict(account)
        if safe_account.get("client_secret"):
            secret = safe_account["client_secret"]
            safe_account["client_secret_masked"] = f"{secret[:4]}••••••••{secret[-4:]}" if len(secret) > 8 else "••••••••"
        else:
            safe_account["client_secret_masked"] = ""
        # Do not expose raw client_secret to client
        safe_account.pop("client_secret", None)

        rules = db.get_rules()
        active_rules = [r for r in rules if r["status"] == "ACTIVE_WAITING"]
        blocked_rules = [r for r in rules if r["status"] == "BLOCKED_BY_AI"]
        audit_logs = db.get_audit_logs(limit=100)

        gemini_configured = bool(db.get_config("gemini_api_key", ""))
        webhook_id_configured = bool(db.get_config("webhook_secret_key", ""))

        return jsonify({
            "success": True,
            "account": safe_account,
            "metrics": {
                "active_mandates": len(active_rules),
                "blocked_mandates": len(blocked_rules),
                "total_mandates": len(rules),
                "total_saved_by_ai": account["total_saved_by_ai"],
                "total_auto_settled": account["total_auto_settled"]
            },
            "rules": rules,
            "audit_logs": audit_logs,
            "config": {
                "gemini_configured": gemini_configured,
                # Never expose the signing secret to the browser.
                "webhook_configured": webhook_id_configured
            },
            "demo_mode": safe_account.get("connected_status") != "CONNECTED"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/settings/paypal", methods=["POST"])
def update_paypal_settings():
    """Saves and validates PayPal credentials via live OAuth2 token handshake.

    Blank credential fields keep the currently stored values, so a user can
    update the Gemini key or environment without accidentally disconnecting.
    """
    data = request.get_json() or {}
    existing = db.get_account()
    client_id = data.get("client_id", "").strip() or existing.get("client_id", "")
    client_secret = data.get("client_secret", "").strip() or existing.get("client_secret", "")
    environment = data.get("environment", existing.get("environment", "sandbox")).strip().lower()
    merchant_email = data.get("merchant_email", existing.get("merchant_email", "")).strip()
    gemini_key = data.get("gemini_api_key", "").strip()

    if environment not in ("sandbox", "live"):
        return jsonify({"success": False, "error": "environment must be 'sandbox' or 'live'."}), 400

    if gemini_key:
        db.set_config("gemini_api_key", gemini_key)

    # Explicit disconnect: clears stored credentials so the app returns to the
    # offline demo mode (Gemini key and other config are preserved).
    if data.get("disconnect"):
        db.update_account_credentials("", "", environment, merchant_email, status="DISCONNECTED")
        db.update_vault_tokens(vault_setup_token="", vault_payment_token="")
        return jsonify({
            "success": True,
            "connected_status": "DISCONNECTED",
            "demo_mode": True,
            "message": "PayPal credentials cleared. Back to offline demo mode."
        })

    if not client_id or not client_secret:
        # Save credentials as disconnected
        db.update_account_credentials(client_id, client_secret, environment, merchant_email, status="DISCONNECTED")
        return jsonify({
            "success": True,
            "connected_status": "DISCONNECTED",
            "demo_mode": True,
            "message": "Settings saved (Demo / Offline Mode — add PayPal credentials to go live)."
        })

    # Test live handshake with PayPal API
    test_res = paypal_client.test_connection(client_id, client_secret, environment)
    if test_res.get("success"):
        db.update_account_credentials(client_id, client_secret, environment, merchant_email, status="CONNECTED")
        # Ensure a vault setup token is provisioned
        account = db.get_account()
        if not account.get("vault_setup_token"):
            vault_res = paypal_client.create_vault_setup_token(client_id, client_secret, environment)
            if vault_res.get("success"):
                db.update_vault_tokens(vault_setup_token=vault_res["vault_setup_token"])

        return jsonify({
            "success": True,
            "connected_status": "CONNECTED",
            "environment": environment,
            "demo_mode": False,
            "message": f"Successfully authenticated with PayPal {environment.upper()} REST API!"
        })
    else:
        db.update_account_credentials(client_id, client_secret, environment, merchant_email, status="ERROR")
        return jsonify({
            "success": False,
            "connected_status": "ERROR",
            "error": test_res.get("error", "OAuth2 handshake failed with PayPal.")
        }), 400


@app.route("/api/paypal/vault-setup", methods=["POST"])
def generate_vault_token():
    """Provisions a new PayPal Payment Method Vault Setup Token."""
    account = db.get_account()
    res = paypal_client.create_vault_setup_token(
        account.get("client_id"),
        account.get("client_secret"),
        account.get("environment", "sandbox")
    )
    if res.get("success"):
        token = res["vault_setup_token"]
        db.update_vault_tokens(vault_setup_token=token)
        return jsonify({"success": True, "simulated": bool(res.get("simulated")), "vault_setup_token": token, "details": res})
    return jsonify({"success": False, "error": res.get("error", "Failed to create vault setup token")}), 400


@app.route("/api/parse-rule", methods=["POST"])
def parse_rule():
    """Extracts structured mandate fields from plain English text."""
    data = request.get_json() or {}
    text = data.get("text", "")
    gemini_key = db.get_config("gemini_api_key", "")
    try:
        parsed = ai_engine.parse_natural_language_rule(text, api_key=gemini_key)
        return jsonify({"success": True, "parsed": parsed})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/rules", methods=["GET", "POST"])
def handle_rules():
    """Lists active rules or locks a new rule into SQLite."""
    if request.method == "GET":
        rules = db.get_rules()
        return jsonify({"success": True, "rules": rules})

    data = request.get_json() or {}
    clean, errors = _validate_rule_payload(data)
    if errors:
        return jsonify({"success": False, "error": " ".join(errors), "errors": errors}), 400
    try:
        rule = db.create_rule(clean)
        db.log_audit(
            rule_id=rule["id"],
            event_source="User Rule Activation",
            event_title=f"Mandate Registered: {rule['title']}",
            raw_webhook_json={"action": "MANDATE_ACTIVATION", "rule": rule},
            ai_reasoning_steps=[
                f"[✓] Validated Mandate Guard: {rule['condition_operator']} ${rule['condition_threshold']:,.2f}",
                f"[✓] Linked PayPal Vault Token ID: {rule['vault_token_id']}",
                f"[✓] Firewall Armed: Listening for {rule['trigger_source']} to settle {rule['recipient_name']} ({rule['recipient_paypal_email']})"
            ],
            paypal_api_trace={
                "tool": "paypal_vault.register_mandate",
                "mandate_id": rule["vault_token_id"],
                "payee": rule["recipient_paypal_email"],
                "status": "ARMED_AND_LISTENING"
            },
            verdict="CAPTURED",
            amount_impact=0.0
        )
        return jsonify({"success": True, "rule": rule})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/rules/<int:rule_id>", methods=["DELETE"])
def delete_rule_endpoint(rule_id):
    """Deletes an active rule from SQLite."""
    try:
        db.delete_rule(rule_id)
        return jsonify({"success": True, "deleted_id": rule_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/rules/<int:rule_id>/toggle", methods=["PATCH"])
def toggle_rule(rule_id):
    """Pauses an ACTIVE_WAITING mandate or resumes a PAUSED one."""
    try:
        rule = db.toggle_rule_pause(rule_id)
        if rule:
            return jsonify({"success": True, "rule": rule})
        return jsonify({"success": False, "error": "Rule not found"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/rules/<int:rule_id>/rearm", methods=["POST"])
def rearm_rule(rule_id):
    """Re-activates a finished mandate (EXECUTED / REJECTED / OVERRIDDEN / PAUSED)."""
    try:
        rule = db.rearm_rule(rule_id)
        if rule:
            return jsonify({"success": True, "rule": rule})
        return jsonify({"success": False, "error": "Rule not found"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/demo/seed", methods=["POST"])
def seed_demo():
    """One-click judge demo: arms a bill-cap mandate and a paycheck chain reaction."""
    existing_titles = {r["title"] for r in db.get_rules()}
    created = []

    demo_rules = [
        {
            "title": "SpeedNet Wi-Fi Cap (Demo)",
            "natural_language_rule": "Auto-pay Wi-Fi invoices from billing@speednet.com only if total is $50 or less",
            "rule_category": "BILL_CAP",
            "trigger_source": "SpeedNet Wi-Fi",
            "recipient_name": "SpeedNet Broadband",
            "recipient_paypal_email": "billing@speednet.com",
            "payment_amount": 0.0,
            "currency": "USD",
            "condition_operator": "<=",
            "condition_threshold": 50.0,
            "status": "ACTIVE_WAITING",
        },
        {
            "title": "Payday First, Rent Second (Demo)",
            "natural_language_rule": "Pay landlord@metroprop.com $500 for rent only after an incoming paycheck of at least $1,000 arrives",
            "rule_category": "CHAIN_REACTION",
            "trigger_source": "Employer Paycheck",
            "recipient_name": "Metro Properties (Landlord)",
            "recipient_paypal_email": "landlord@metroprop.com",
            "payment_amount": 500.0,
            "currency": "USD",
            "condition_operator": ">=",
            "condition_threshold": 1000.0,
            "status": "ACTIVE_WAITING",
        },
    ]

    for payload in demo_rules:
        if payload["title"] in existing_titles:
            continue
        clean, errors = _validate_rule_payload(payload)
        if errors:
            return jsonify({"success": False, "error": " ".join(errors)}), 400
        created.append(db.create_rule(clean))

    return jsonify({"success": True, "created": len(created), "rules": db.get_rules()})


@app.route("/api/demo/reset", methods=["POST"])
def reset_demo():
    """Clears mandates, audit logs and running totals (keeps credentials/config)."""
    db.clear_rules_and_logs()
    return jsonify({"success": True, "message": "Demo data cleared."})


@app.route("/api/webhooks/paypal", methods=["POST"])
def paypal_webhook():
    """
    Real PayPal Webhook receiver.
    Triggered when incoming payment captures (PAYMENT.CAPTURE.COMPLETED)
    or invoices arrive from PayPal.
    """
    raw_body = request.get_data(as_text=True)
    headers = dict(request.headers)
    account = db.get_account()

    is_verified = paypal_client.verify_webhook_signature(
        headers=headers,
        body_str=raw_body,
        webhook_id=db.get_config("webhook_secret_key"),
        client_id=account.get("client_id"),
        client_secret=account.get("client_secret"),
        env=account.get("environment", "sandbox")
    )

    # In live mode, refuse unverified events: an unsigned wallet trigger is a
    # money-movement request, not a demo toy.
    if account.get("connected_status") == "CONNECTED" and not is_verified:
        return jsonify({
            "success": False,
            "error": "Webhook signature verification failed.",
            "signature_verified": False
        }), 401

    event_data = request.get_json(silent=True) or {}
    event_type = event_data.get("event_type", "PAYMENT.CAPTURE.COMPLETED")
    event_source = event_data.get("summary") or event_data.get("resource_type") or "PayPal Webhook"

    # Idempotency: PayPal redelivers webhooks on timeout/retry. The same event
    # id must never move money twice.
    event_id = event_data.get("id") or event_data.get("event_id")
    if event_id and db.was_event_processed(event_id):
        return jsonify({
            "success": True,
            "webhook_status": "DUPLICATE_SKIPPED",
            "event_id": event_id,
            "signature_verified": is_verified,
            "evaluation": {"processed_count": 0, "results": []}
        })

    # Evaluate against active rules
    eval_result = ai_engine.evaluate_and_process_event(
        event_source=f"PayPal: {event_source}",
        event_type=event_type,
        event_data=event_data
    )

    if event_id:
        db.mark_event_processed(event_id, f"PayPal: {event_source}")

    return jsonify({
        "success": True,
        "webhook_status": "PROCESSED",
        "signature_verified": is_verified,
        "evaluation": eval_result
    })


@app.route("/api/webhooks/external", methods=["POST"])
def external_webhook():
    """
    Production endpoint for vendor billing invoices, milestone deliveries, or price alerts.
    Can be called via curl, Zapier, GitHub Webhooks, or the production UI Modal.

    Set AUTOPAY_REQUIRE_WEBHOOK_SECRET=1 to require the X-Webhook-Secret header
    (value: the server's configured webhook secret) on every call.
    """
    data = request.get_json() or {}

    if os.environ.get("AUTOPAY_REQUIRE_WEBHOOK_SECRET") == "1":
        expected = db.get_config("webhook_secret_key", "")
        provided = request.headers.get("X-Webhook-Secret", "")
        if not expected or provided != expected:
            return jsonify({"success": False, "error": "Invalid or missing X-Webhook-Secret header."}), 401

    source = data.get("trigger_source", "External Billing Service")
    event_type = data.get("event_type", "INVOICE_BILLING")

    # Idempotency for integrations that redeliver (Zapier retries, queue
    # replays): an explicit event_id is only ever processed once.
    event_id = data.get("event_id") or data.get("id")
    if event_id and db.was_event_processed(event_id):
        return jsonify({
            "success": True,
            "webhook_status": "DUPLICATE_SKIPPED",
            "event_id": event_id,
            "evaluation": {"processed_count": 0, "results": []}
        })

    eval_result = ai_engine.evaluate_and_process_event(
        event_source=source,
        event_type=event_type,
        event_data=data
    )

    if event_id:
        db.mark_event_processed(event_id, source)

    return jsonify({"success": True, "evaluation": eval_result})


@app.route("/api/rules/<int:rule_id>/override", methods=["POST"])
def override_rule(rule_id):
    """Human-in-the-loop action: Authorizes force-capture of a blocked spiked bill."""
    try:
        result = ai_engine.execute_override(rule_id)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/rules/<int:rule_id>/reject", methods=["POST"])
def reject_rule(rule_id):
    """Human-in-the-loop action: Permanently rejects and disputes an unauthorized charge."""
    try:
        result = ai_engine.execute_reject(rule_id)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "0") == "1"
    )
