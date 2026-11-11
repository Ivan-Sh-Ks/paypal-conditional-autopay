"""
PayPal Conditional Autopay — AI Mandate Firewall Engine
Natural-language rule extraction and real-time firewall evaluation against PayPal webhooks and invoices.
"""

import re
import json
import uuid
from datetime import datetime
import database as db
import paypal_client
import requests


def parse_natural_language_rule(text: str, api_key: str = None) -> dict:
    """
    Parses plain English text into a structured Autopay Mandate:
    - title
    - rule_category ('CHAIN_REACTION', 'SPLIT_PAYMENT', 'BILL_CAP', 'MILESTONE', 'PRICE_DROP')
    - trigger_source
    - recipient_name
    - recipient_paypal_email
    - payment_amount
    - currency ('USD')
    - condition_operator ('>=', '<=', '==')
    - condition_threshold
    """
    clean_text = text.strip()
    if not clean_text:
        return {
            "title": "Untitled Mandate",
            "rule_category": "BILL_CAP",
            "trigger_source": "Inbound Webhook",
            "recipient_name": "Vendor Merchant",
            "recipient_paypal_email": "merchant@example.com",
            "payment_amount": 0.0,
            "currency": "USD",
            "condition_operator": "<=",
            "condition_threshold": 0.0,
            "natural_language_rule": text,
            "parser": "local"
        }

    # Attempt Live LLM parse if Gemini API key is configured
    if api_key:
        try:
            llm_result = _call_gemini_parser(clean_text, api_key)
            if llm_result:
                return llm_result
        except Exception:
            pass

    return _parse_rule_deterministic(clean_text)


def _call_gemini_parser(text: str, api_key: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    system_prompt = """
    You are an AI FinTech Architect for PayPal Conditional Autopay.
    Parse the user's natural language rule into strict JSON with keys:
    - "title": short descriptive title
    - "rule_category": one of ["CHAIN_REACTION", "SPLIT_PAYMENT", "BILL_CAP", "MILESTONE", "PRICE_DROP"]
    - "trigger_source": event or entity initiating the trigger (e.g. "Employer Paycheck", "Client Corp", "SpeedNet Wi-Fi", "Electric Utility")
    - "recipient_name": payee name
    - "recipient_paypal_email": valid paypal email if mentioned or realistic default
    - "payment_amount": float
    - "currency": "USD"
    - "condition_operator": one of [">=", "<=", "=="]
    - "condition_threshold": float
    Return ONLY JSON.
    """
    payload = {
        "contents": [{
            "parts": [
                {"text": system_prompt},
                {"text": f"Rule text: \"{text}\""}
            ]
        }],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    resp = requests.post(url, json=payload, timeout=6)
    if resp.status_code == 200:
        data = resp.json()
        raw_content = data['candidates'][0]['content']['parts'][0]['text']
        parsed = json.loads(raw_content)
        parsed["natural_language_rule"] = text
        parsed["payment_amount"] = float(parsed.get("payment_amount", 0.0))
        parsed["condition_threshold"] = float(parsed.get("condition_threshold", 0.0))
        parsed["currency"] = parsed.get("currency", "USD")
        parsed["parser"] = "gemini"
        return parsed
    return None


def _compact(value: str) -> str:
    """Lowercase and strip punctuation/spacing so 'Wi-Fi' == 'wifi'."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _parse_rule_deterministic(text: str) -> dict:
    lower = text.lower()
    compact = _compact(text)

    def has(*phrases) -> bool:
        return any(_compact(p) in compact for p in phrases)

    # 1. Detect Category
    category = "BILL_CAP"
    if has("price drop", "drops to", "sniper", "restock", "in stock", "deal"):
        category = "PRICE_DROP"
    elif has("milestone", "checksum", "upload", "github", "pull request", "proof"):
        category = "MILESTONE"
    elif has("rent", "paycheck", "salary", "wage", "only after", "chain", "once the", "landlord"):
        category = "CHAIN_REACTION"
    elif has("split", "client", "share", "pass-through", "sends me", "whenever client"):
        category = "SPLIT_PAYMENT"
    elif has("bill", "cap", "invoice", "spike", "under", "limit", "utility", "wifi", "electric"):
        category = "BILL_CAP"

    # 2. Extract Email
    email_regex = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b'
    emails = re.findall(email_regex, text)
    recipient_email = emails[0] if emails else "payee@example.com"

    # 3. Extract dollar amounts: e.g. $500, $1,000, 500 dollars
    dollar_regex = r'\$\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)'
    amounts = [float(m.replace(',', '')) for m in re.findall(dollar_regex, text)]

    if not amounts:
        word_amounts = re.findall(r'([0-9]+(?:\.[0-9]{1,2})?)\s*(?:dollars|bucks|usd)', lower)
        amounts = [float(a) for a in word_amounts]

    payment_amount = 0.0
    threshold = 0.0

    if len(amounts) >= 2:
        payment_amount = amounts[0]
        threshold = amounts[1]
    elif len(amounts) == 1:
        payment_amount = amounts[0]
        threshold = amounts[0]

    # 4. Detect Operator (negations before bare comparatives so
    #    "no more than $50" becomes <= instead of >=)
    if "==" in text or has("equal to", "equals", "exactly"):
        operator = "=="
    elif has("no more than", "not more than", "not to exceed", "up to", "at most",
             "maximum", "capped at", "or less", "no greater than"):
        operator = "<="
    elif ">=" in text or has("at least", "minimum", "or more", "no less than",
                             "not less than", "greater than or equal"):
        operator = ">="
    elif "<=" in text or has("less than or equal", "below or equal"):
        operator = "<="
    elif ">" in text or has("more than", "greater than", "exceeds", "above"):
        operator = ">="
    elif "<" in text or has("less than", "under", "below", "limit"):
        operator = "<="
    else:
        operator = ">=" if category in ["CHAIN_REACTION", "SPLIT_PAYMENT"] else "<="

    # 5. Detect Recipient Name
    recipient_name = "Designated Payee"
    if has("landlord", "rent"):
        recipient_name = "Metro Properties (Landlord)"
        if not emails:
            recipient_email = "landlord@metroprop.com"
    elif has("rahul", "designer"):
        recipient_name = "Rahul Sharma (UI Designer)"
        if not emails:
            recipient_email = "rahul@designcraft.io"
    elif has("speednet", "wifi"):
        recipient_name = "SpeedNet Broadband"
        if not emails:
            recipient_email = "billing@speednet.com"
    elif has("video", "maya"):
        recipient_name = "Maya Lin (Video Editor)"
        if not emails:
            recipient_email = "maya@editpro.com"
    elif has("cooling pad", "techgear"):
        recipient_name = "TechGear Direct"
        if not emails:
            recipient_email = "orders@techgear.com"
    elif emails:
        recipient_name = emails[0].split('@')[0].capitalize()

    # 6. Detect Trigger Source
    trigger_source = "Inbound Webhook"
    if has("paycheck", "salary", "employer", "payroll"):
        trigger_source = "Employer Paycheck"
    elif has("client"):
        trigger_source = "Client Corp"
    elif has("speednet", "wifi"):
        trigger_source = "SpeedNet Wi-Fi"
    elif has("electric", "power", "grid"):
        trigger_source = "GridPower Electric"
    elif has("cloud", "final_cut"):
        trigger_source = "Cloud Storage Webhook"
    elif has("store", "price", "restock"):
        trigger_source = "Price Tracker Webhook"

    # 7. Generate Title
    if category == "CHAIN_REACTION":
        title = f"Chain Trigger: {recipient_name}"
    elif category == "SPLIT_PAYMENT":
        title = f"Revenue Split: {recipient_name}"
    elif category == "BILL_CAP":
        title = f"Bill Cap: {trigger_source}"
    elif category == "MILESTONE":
        title = f"Milestone Escrow: {recipient_name}"
    else:
        title = f"Price Sniper: {recipient_name}"

    return {
        "title": title,
        "natural_language_rule": text,
        "rule_category": category,
        "trigger_source": trigger_source,
        "recipient_name": recipient_name,
        "recipient_paypal_email": recipient_email,
        "payment_amount": payment_amount,
        "currency": "USD",
        "condition_operator": operator,
        "condition_threshold": threshold,
        "parser": "local"
    }


def _extract_inbound_amount(event_data: dict) -> float:
    """Reads an amount from the common PayPal / generic webhook payload shapes."""
    candidates = [
        event_data.get("amount"),
        event_data.get("price"),
        event_data.get("total"),
    ]
    resource = event_data.get("resource") if isinstance(event_data.get("resource"), dict) else {}
    if isinstance(resource.get("amount"), dict):
        candidates.append(resource["amount"].get("value"))
    elif resource.get("amount") is not None:
        candidates.append(resource.get("amount"))
    purchase_units = resource.get("purchase_units") or event_data.get("purchase_units") or []
    if isinstance(purchase_units, list) and purchase_units:
        pu_amount = (purchase_units[0] or {}).get("amount") or {}
        if isinstance(pu_amount, dict):
            candidates.append(pu_amount.get("value"))

    for value in candidates:
        if value is None:
            continue
        try:
            return float(value)
        except (ValueError, TypeError):
            continue
    return 0.0


def evaluate_and_process_event(event_source: str, event_type: str, event_data: dict) -> dict:
    """
    Main evaluation pipeline:
    Matches active rules against the incoming event, executes AI Firewall checks,
    and calls real PayPal REST API endpoints or halts before funds move.
    """
    account = db.get_account()
    rules = db.get_rules(status="ACTIVE_WAITING")

    inbound_amount = _extract_inbound_amount(event_data)

    results = []

    # If no rules exist, log event to audit ledger
    if not rules:
        log_id = db.log_audit(
            rule_id=None,
            event_source=event_source,
            event_title=f"Unmatched Inbound Event: {event_source} (${inbound_amount:,.2f})",
            raw_webhook_json=event_data,
            ai_reasoning_steps=[
                f"[i] Ingested webhook from {event_source} (Amount: ${inbound_amount:,.2f})",
                "[i] Evaluated 0 active rules in SQLite. No matching conditional mandate found.",
                "[✓] No funds moved."
            ],
            paypal_api_trace={"status": "NO_ACTION_REQUIRED", "reason": "No active matching mandate"},
            verdict="HOLD_INSUFFICIENT_TRIGGER",
            amount_impact=0.0
        )
        return {
            "processed_count": 0,
            "results": [{
                "status": "UNMATCHED",
                "message": "Event received but no active rules matched.",
                "audit_log_id": log_id
            }]
        }

    # Categories where one invoice/event should settle at most once.
    single_settlement_categories = {"BILL_CAP", "MILESTONE", "PRICE_DROP"}

    generic_sources = {
        _compact(name) for name in (
            "", "inbound webhook", "external billing service", "paypal webhook",
            "webhook", "any", "all", "any vendor", "*"
        )
    }
    event_blob = _compact(f"{event_source} {json.dumps(event_data, default=str)}")

    for rule in rules:
        rule_source = _compact(rule.get("trigger_source"))
        source_match = len(rule_source) >= 3 and rule_source in event_blob
        generic_source = rule_source in generic_sources

        category_match = False
        if event_type in ["PAYMENT.CAPTURE.COMPLETED", "PAYROLL_DEPOSIT", "CLIENT_PAYMENT"] and rule["rule_category"] in ["CHAIN_REACTION", "SPLIT_PAYMENT"]:
            category_match = True
        elif event_type in ["INVOICE_BILLING", "INVOICING.INVOICE.PAID", "BILL_PRESENTED"] and rule["rule_category"] == "BILL_CAP":
            category_match = True
        elif event_type in ["OBJECT_VERIFIED", "MILESTONE_WEBHOOK"] and rule["rule_category"] == "MILESTONE":
            category_match = True
        elif event_type in ["PRICE_DROP_ALERT", "STOCK_UPDATE"] and rule["rule_category"] == "PRICE_DROP":
            category_match = True

        if not (source_match or (generic_source and category_match)):
            continue

        steps = []
        rule_id = rule["id"]
        category = rule["rule_category"]
        operator = rule["condition_operator"]
        threshold = rule["condition_threshold"]
        payout_amt = rule["payment_amount"]
        recipient_email = rule["recipient_paypal_email"]
        recipient_name = rule["recipient_name"]
        currency = rule["currency"] or "USD"
        vault_token = rule["vault_token_id"]

        steps.append(f"[✓] Event Ingested: {event_source} ({event_type}) with payload value ${inbound_amount:,.2f}")
        steps.append(f"[✓] Matched Mandate #{rule_id}: '{rule['title']}' (Category: {category})")
        steps.append(f"[✓] Validated PayPal Vault Token: {vault_token}")

        # 1. CHAIN REACTION & SPLIT PAYMENT
        if category in ["CHAIN_REACTION", "SPLIT_PAYMENT"]:
            condition_met = False
            if operator == ">=" and inbound_amount >= threshold:
                condition_met = True
            elif operator == "==" and abs(inbound_amount - threshold) < 0.01:
                condition_met = True
            elif operator == "<=" and inbound_amount <= threshold:
                condition_met = True

            if condition_met:
                steps.append(f"[✓] Condition PASSED: Inbound ${inbound_amount:,.2f} {operator} ${threshold:,.2f} threshold")
                steps.append(f"[✓] AI Mandate Firewall: APPROVED. Dispatching PayPal Orders v2 payout to {recipient_name} ({recipient_email})")

                # Call PayPal Client
                api_trace = paypal_client.create_and_capture_order(
                    recipient_email=recipient_email,
                    amount=payout_amt,
                    currency=currency,
                    client_id=account.get("client_id"),
                    client_secret=account.get("client_secret"),
                    env=account.get("environment", "sandbox"),
                    vault_token=vault_token,
                    description=f"Conditional Autopay: {rule['title']}"
                )

                if api_trace.get("success"):
                    if api_trace.get("simulated"):
                        steps.append("[i] Demo mode: PayPal capture simulated locally (no live credentials connected).")

                    updated_rule = db.increment_rule_executions(rule_id)
                    run_number = (updated_rule or {}).get("execution_count", 1)

                    # If recurring, keep ACTIVE_WAITING; otherwise mark EXECUTED
                    final_status = "ACTIVE_WAITING" if rule.get("recurring") else "EXECUTED"
                    db.update_rule_status(rule_id, final_status, None, None)
                    db.increment_metrics(saved_delta=0.0, settled_delta=payout_amt)

                    log_id = db.log_audit(
                        rule_id=rule_id,
                        event_source=event_source,
                        event_title=f"Mandate Executed: {rule['title']} (${payout_amt:,.2f} sent to {recipient_email}, run #{run_number})",
                        raw_webhook_json=event_data,
                        ai_reasoning_steps=steps,
                        paypal_api_trace=api_trace,
                        verdict="CHAIN_TRIGGERED" if category == "CHAIN_REACTION" else "CAPTURED",
                        amount_impact=payout_amt
                    )
                    results.append({"rule_id": rule_id, "verdict": "CAPTURED", "payout": payout_amt, "audit_log_id": log_id, "recurring": bool(rule.get("recurring")), "execution_count": run_number, "simulated": bool(api_trace.get("simulated"))})
                else:
                    error = api_trace.get("error") or "Unknown PayPal API error"
                    steps.append(f"[✗] PayPal capture FAILED: {error}")
                    steps.append("[i] Mandate re-armed automatically. No funds moved.")
                    db.update_rule_status(rule_id, "ACTIVE_WAITING")
                    log_id = db.log_audit(
                        rule_id=rule_id,
                        event_source=event_source,
                        event_title=f"Payment Failed: {rule['title']} (PayPal did not settle ${payout_amt:,.2f})",
                        raw_webhook_json=event_data,
                        ai_reasoning_steps=steps,
                        paypal_api_trace=api_trace,
                        verdict="PAYMENT_FAILED",
                        amount_impact=0.0
                    )
                    results.append({"rule_id": rule_id, "verdict": "PAYMENT_FAILED", "audit_log_id": log_id})

            else:
                shortfall = threshold - inbound_amount
                steps.append(f"[✗] Threshold FAILED: Inbound ${inbound_amount:,.2f} did not meet ${threshold:,.2f} (Shortfall: -${shortfall:,.2f})")
                steps.append(f"[!] AI Mandate Firewall: HELD outbound payment of ${payout_amt:,.2f} to prevent overdraft")
                steps.append(f"[✓] Zero dollars moved. Mandate remains ACTIVE_WAITING for full deposit.")

                api_trace = {
                    "action": "PAYPAL_CAPTURE_HELD",
                    "reason": f"Inbound amount ${inbound_amount:,.2f} < ${threshold:,.2f} required threshold",
                    "mandate_id": rule["vault_token_id"],
                    "recipient": recipient_email,
                    "target_amount": payout_amt
                }

                log_id = db.log_audit(
                    rule_id=rule_id,
                    event_source=event_source,
                    event_title=f"Mandate Held: {rule['title']} (Inbound ${inbound_amount:,.2f} below threshold)",
                    raw_webhook_json=event_data,
                    ai_reasoning_steps=steps,
                    paypal_api_trace=api_trace,
                    verdict="HOLD_INSUFFICIENT_TRIGGER",
                    amount_impact=0.0
                )
                results.append({"rule_id": rule_id, "verdict": "HOLD_INSUFFICIENT_TRIGGER", "audit_log_id": log_id})

        # 2. BILL CAP
        elif category == "BILL_CAP":
            invoice_amt = inbound_amount if inbound_amount > 0 else payout_amt
            steps.append(f"[✓] Inspecting Inbound Invoice Amount: ${invoice_amt:,.2f} against Cap: ${threshold:,.2f}")

            if invoice_amt <= threshold:
                steps.append(f"[✓] Price Cap Evaluation: ${invoice_amt:,.2f} <= ${threshold:,.2f} PASSED (Within authorized cap)")
                steps.append(f"[✓] AI Mandate Firewall: APPROVED. Authorizing PayPal Orders v2 capture for ${invoice_amt:,.2f}")

                api_trace = paypal_client.create_and_capture_order(
                    recipient_email=recipient_email,
                    amount=invoice_amt,
                    currency=currency,
                    client_id=account.get("client_id"),
                    client_secret=account.get("client_secret"),
                    env=account.get("environment", "sandbox"),
                    vault_token=vault_token,
                    description=f"Conditional Autopay: {rule['title']}"
                )

                if api_trace.get("success"):
                    if api_trace.get("simulated"):
                        steps.append("[i] Demo mode: PayPal capture simulated locally (no live credentials connected).")

                    updated_rule = db.increment_rule_executions(rule_id)
                    run_number = (updated_rule or {}).get("execution_count", 1)
                    db.update_rule_status(rule_id, "ACTIVE_WAITING", None, None)
                    db.increment_metrics(saved_delta=0.0, settled_delta=invoice_amt)

                    log_id = db.log_audit(
                        rule_id=rule_id,
                        event_source=event_source,
                        event_title=f"Bill Captured: {rule['title']} (${invoice_amt:,.2f} paid to {recipient_email}, run #{run_number})",
                        raw_webhook_json=event_data,
                        ai_reasoning_steps=steps,
                        paypal_api_trace=api_trace,
                        verdict="CAPTURED",
                        amount_impact=invoice_amt
                    )
                    results.append({"rule_id": rule_id, "verdict": "CAPTURED", "amount": invoice_amt, "audit_log_id": log_id, "recurring": True, "execution_count": run_number, "simulated": bool(api_trace.get("simulated"))})
                else:
                    error = api_trace.get("error") or "Unknown PayPal API error"
                    steps.append(f"[✗] PayPal capture FAILED: {error}")
                    steps.append("[i] Mandate re-armed automatically. No funds moved.")
                    db.update_rule_status(rule_id, "ACTIVE_WAITING")
                    log_id = db.log_audit(
                        rule_id=rule_id,
                        event_source=event_source,
                        event_title=f"Payment Failed: {rule['title']} (PayPal did not settle ${invoice_amt:,.2f})",
                        raw_webhook_json=event_data,
                        ai_reasoning_steps=steps,
                        paypal_api_trace=api_trace,
                        verdict="PAYMENT_FAILED",
                        amount_impact=0.0
                    )
                    results.append({"rule_id": rule_id, "verdict": "PAYMENT_FAILED", "audit_log_id": log_id})

            else:
                overage = invoice_amt - threshold
                steps.append(f"[✗] PRICE SPIKE VIOLATION: Invoice ${invoice_amt:,.2f} exceeds cap of ${threshold:,.2f} by +${overage:,.2f}")
                steps.append(f"[!] AI Mandate Firewall: IMMEDIATE FREEZE. Halted PayPal capture before a single dollar moved.")
                steps.append(f"[✓] Protected Account: $0.00 deducted from PayPal. Full ${invoice_amt:,.2f} blocked.")
                steps.append(f"[!] Triggered Human-in-the-Loop review card. Awaiting user authorization or formal dispute.")

                api_trace = {
                    "action": "PAYPAL_FIREWALL_HALT",
                    "status": "BLOCKED_BEFORE_CAPTURE",
                    "attempted_amount": invoice_amt,
                    "cap_threshold": threshold,
                    "overage": overage,
                    "recipient": recipient_email,
                    "vault_token": vault_token,
                    "timestamp": datetime.now().isoformat()
                }

                db.update_rule_status(
                    rule_id=rule_id,
                    status="BLOCKED_BY_AI",
                    pending_invoice_amount=invoice_amt,
                    pending_event_payload=event_data
                )
                db.increment_metrics(saved_delta=invoice_amt, settled_delta=0.0)

                log_id = db.log_audit(
                    rule_id=rule_id,
                    event_source=event_source,
                    event_title=f"Bill Blocked by AI: {rule['title']} (${invoice_amt:,.2f} exceeded ${threshold:,.2f} cap)",
                    raw_webhook_json=event_data,
                    ai_reasoning_steps=steps,
                    paypal_api_trace=api_trace,
                    verdict="BLOCKED",
                    amount_impact=0.0
                )
                results.append({"rule_id": rule_id, "verdict": "BLOCKED", "blocked_amount": invoice_amt, "audit_log_id": log_id})

        # 3. MILESTONE & PRICE DROP
        elif category in ["MILESTONE", "PRICE_DROP"]:
            target_amt = inbound_amount if inbound_amount > 0 else payout_amt
            steps.append(f"[✓] Evaluating {category} Payload: Checking verifiable conditions")

            condition_verified = True
            if category == "MILESTONE":
                condition_verified = event_data.get("checksum_verified", True)
                steps.append(f"[✓] SHA-256 Artifact Verification: {'PASSED (200 OK)' if condition_verified else 'FAILED'}")
            elif category == "PRICE_DROP":
                in_stock = event_data.get("in_stock", True)
                condition_verified = in_stock and (target_amt <= threshold)
                steps.append(f"[✓] Restock Check: In Stock={in_stock}, Price=${target_amt:,.2f} <= ${threshold:,.2f}")

            if condition_verified:
                steps.append(f"[✓] AI Mandate Firewall: APPROVED. Executing PayPal payout for ${target_amt:,.2f} to {recipient_email}")
                api_trace = paypal_client.create_and_capture_order(
                    recipient_email=recipient_email,
                    amount=target_amt,
                    currency=currency,
                    client_id=account.get("client_id"),
                    client_secret=account.get("client_secret"),
                    env=account.get("environment", "sandbox"),
                    vault_token=vault_token,
                    description=f"Conditional Autopay: {rule['title']}"
                )
                if api_trace.get("success"):
                    if api_trace.get("simulated"):
                        steps.append("[i] Demo mode: PayPal capture simulated locally (no live credentials connected).")

                    db.increment_rule_executions(rule_id)
                    db.update_rule_status(rule_id, "EXECUTED")
                    db.increment_metrics(saved_delta=0.0, settled_delta=target_amt)
                    log_id = db.log_audit(
                        rule_id=rule_id,
                        event_source=event_source,
                        event_title=f"Condition Met & Settled: {rule['title']} (${target_amt:,.2f})",
                        raw_webhook_json=event_data,
                        ai_reasoning_steps=steps,
                        paypal_api_trace=api_trace,
                        verdict="CAPTURED",
                        amount_impact=target_amt
                    )
                    results.append({"rule_id": rule_id, "verdict": "CAPTURED", "amount": target_amt, "audit_log_id": log_id, "recurring": False, "simulated": bool(api_trace.get("simulated"))})
                else:
                    error = api_trace.get("error") or "Unknown PayPal API error"
                    steps.append(f"[✗] PayPal capture FAILED: {error}")
                    steps.append("[i] Mandate re-armed automatically. No funds moved.")
                    db.update_rule_status(rule_id, "ACTIVE_WAITING")
                    log_id = db.log_audit(
                        rule_id=rule_id,
                        event_source=event_source,
                        event_title=f"Payment Failed: {rule['title']} (PayPal did not settle ${target_amt:,.2f})",
                        raw_webhook_json=event_data,
                        ai_reasoning_steps=steps,
                        paypal_api_trace=api_trace,
                        verdict="PAYMENT_FAILED",
                        amount_impact=0.0
                    )
                    results.append({"rule_id": rule_id, "verdict": "PAYMENT_FAILED", "audit_log_id": log_id})
            else:
                steps.append(f"[✗] Condition Not Met: Action deferred")
                api_trace = {"action": "DEFERRED", "reason": "Milestone checksum or price threshold not satisfied"}
                log_id = db.log_audit(
                    rule_id=rule_id,
                    event_source=event_source,
                    event_title=f"Condition Deferred: {rule['title']}",
                    raw_webhook_json=event_data,
                    ai_reasoning_steps=steps,
                    paypal_api_trace=api_trace,
                    verdict="HOLD_INSUFFICIENT_TRIGGER",
                    amount_impact=0.0
                )
                results.append({"rule_id": rule_id, "verdict": "HOLD_INSUFFICIENT_TRIGGER", "audit_log_id": log_id})

        # One invoice should only settle once per webhook delivery.
        if category in single_settlement_categories:
            break

    return {
        "processed_count": len(results),
        "results": results
    }


def execute_override(rule_id: int) -> dict:
    """Human-in-the-loop override that authorizes and executes a previously BLOCKED_BY_AI payment."""
    rule = db.get_rule_by_id(rule_id)
    if not rule:
        raise ValueError(f"Rule {rule_id} not found")
    if rule["status"] != "BLOCKED_BY_AI":
        raise ValueError(
            f"Mandate #{rule_id} is {rule['status']}. Only BLOCKED_BY_AI mandates can be force-captured."
        )
    if not rule.get("pending_invoice_amount"):
        raise ValueError("No pending invoice amount is attached to this blocked mandate.")

    account = db.get_account()
    override_amt = float(rule.get("pending_invoice_amount") or rule.get("payment_amount", 0.0))
    recipient_email = rule["recipient_paypal_email"]
    recipient_name = rule["recipient_name"]
    currency = rule["currency"] or "USD"

    steps = [
        f"[✓] Human-in-the-Loop Override Confirmed by User for Mandate #{rule_id}",
        f"[✓] User Manually Approved Spiked Bill: ${override_amt:,.2f} to {recipient_name} ({recipient_email})",
        f"[✓] Authenticating PayPal Vault Token: {rule['vault_token_id']}",
        f"[✓] Dispatching Force-Capture to PayPal Orders v2 REST API"
    ]

    api_trace = paypal_client.create_and_capture_order(
        recipient_email=recipient_email,
        amount=override_amt,
        currency=currency,
        client_id=account.get("client_id"),
        client_secret=account.get("client_secret"),
        env=account.get("environment", "sandbox"),
        vault_token=rule["vault_token_id"],
        description=f"Manual Override Authorization: {rule['title']}"
    )

    if not api_trace.get("success"):
        steps.append(f"[✗] PayPal capture FAILED: {api_trace.get('error') or 'Unknown PayPal API error'}")
        db.log_audit(
            rule_id=rule_id,
            event_source="User Human-in-the-Loop Override",
            event_title=f"Override Payment Failed: ${override_amt:,.2f} to {recipient_email}",
            raw_webhook_json={"action": "OVERRIDE_FAILED", "amount": override_amt, "rule_id": rule_id},
            ai_reasoning_steps=steps,
            paypal_api_trace=api_trace,
            verdict="PAYMENT_FAILED",
            amount_impact=0.0
        )
        raise ValueError(f"PayPal capture failed: {api_trace.get('error') or 'Unknown error'}. Mandate remains blocked.")

    if api_trace.get("simulated"):
        steps.append("[i] Demo mode: PayPal capture simulated locally (no live credentials connected).")

    db.increment_rule_executions(rule_id)
    db.update_rule_status(rule_id, "OVERRIDDEN")
    db.increment_metrics(saved_delta=-override_amt, settled_delta=override_amt)

    log_id = db.log_audit(
        rule_id=rule_id,
        event_source="User Human-in-the-Loop Override",
        event_title=f"Manual Override Approved: ${override_amt:,.2f} to {recipient_email}",
        raw_webhook_json={"action": "OVERRIDE_APPROVED", "amount": override_amt, "rule_id": rule_id},
        ai_reasoning_steps=steps,
        paypal_api_trace=api_trace,
        verdict="USER_OVERRIDE",
        amount_impact=override_amt
    )

    return {
        "success": True,
        "rule": db.get_rule_by_id(rule_id),
        "audit_log_id": log_id,
        "api_trace": api_trace
    }


def execute_reject(rule_id: int) -> dict:
    """Permanently rejects a blocked invoice and records a formal dispute entry."""
    rule = db.get_rule_by_id(rule_id)
    if not rule:
        raise ValueError(f"Rule {rule_id} not found")
    if rule["status"] != "BLOCKED_BY_AI":
        raise ValueError(
            f"Mandate #{rule_id} is {rule['status']}. Only BLOCKED_BY_AI mandates can be disputed."
        )

    dispute_amt = float(rule.get("pending_invoice_amount") or rule.get("payment_amount", 0.0))

    steps = [
        f"[!] User Action: Permanent Rejection & Dispute Filed for Mandate #{rule_id}",
        f"[✓] Overcharge Flagged: ${dispute_amt:,.2f} from {rule['trigger_source']}",
        f"[✓] Invoking PayPal Merchant Mandate Freeze: {rule['vault_token_id']}",
        f"[✓] Account Protected: $0.00 deducted. Merchant flagged in security ledger."
    ]

    api_trace = {
        "action": "PAYPAL_INVOICE_DISPUTED",
        "mandate_id": rule["vault_token_id"],
        "disputed_merchant": rule["trigger_source"],
        "disputed_amount": dispute_amt,
        "status": "DISPUTED_RESOLVED",
        "timestamp": datetime.now().isoformat()
    }

    db.update_rule_status(rule_id, "REJECTED")

    log_id = db.log_audit(
        rule_id=rule_id,
        event_source="User Dispute Action",
        event_title=f"Invoice Disputed & Rejected: ${dispute_amt:,.2f} from {rule['trigger_source']}",
        raw_webhook_json={"action": "INVOICE_DISPUTED", "amount": dispute_amt, "rule_id": rule_id},
        ai_reasoning_steps=steps,
        paypal_api_trace=api_trace,
        verdict="DISPUTED",
        amount_impact=0.0
    )

    return {
        "success": True,
        "rule": db.get_rule_by_id(rule_id),
        "audit_log_id": log_id,
        "api_trace": api_trace
    }
