# PayPal Conditional Autopay — AI Mandate Firewall

> **An Agentic Pre-Capture Firewall for Programmable Payments**  
> Built with Python Flask, Persistent Relational SQLite (`autopay.db`), and official **PayPal REST API v2/v3** endpoints (`/v1/oauth2/token`, `/v3/vault/setup-tokens`, `/v2/checkout/orders`).

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0.3-black?logo=flask)](https://palletsprojects.com/p/flask/)
[![PayPal REST API](https://img.shields.io/badge/PayPal-REST%20v2%20%2F%20v3-00457C?logo=paypal&logoColor=white)](https://developer.paypal.com/)
[![Gemini AI](https://img.shields.io/badge/Google-Gemini%202.5%20Flash-8E75B2?logo=google&logoColor=white)](https://ai.google.dev/)

---

## 1. Problem & Core Differentiator

Traditional Autopay is **"blind"**: it automatically transfers funds on fixed calendar dates regardless of price spikes, unauthorized service additions, or delayed incoming cashflow.

**PayPal Conditional Autopay** introduces an **AI Mandate Firewall**:
Users write financial boundaries in natural language. When an external trigger (PayPal webhook, vendor invoice, or project milestone) arrives, the AI Agent evaluates the condition **before a single dollar moves**:

* **Condition Met ($\text{TRUE}$):** Instant settlement via **PayPal Payment Method Vaulting** (`/v3/vault/setup-tokens`) and **PayPal Orders v2 Capture** (`/v2/checkout/orders`).
* **Condition Violated ($\text{FALSE}$):** The firewall **freezes the transaction before capture**, increments the **"Protected by AI Firewall"** metric, and alerts the user with a **Human-in-the-Loop review card** offering one-click **Force Capture via PayPal** or **Reject & Dispute**.

The dashboard never auto-arms a rule it just parsed: extracted fields appear in an editable review panel first, so a bad NLP guess can be corrected before a mandate goes live.

```
[ Inbound Bill / Webhook ]
            │
            ▼
┌────────────────────────┐
│  AI Mandate Firewall   │ ──( Condition Violated )──► [ FREEZE TRANSACTION ]
│ (Reasoning & Pre-Check)│                              │ • $0.00 debited
└───────────┬────────────┘                              │ • Flag Human Review
            │ (Condition Met)                           ▼
            ▼                                  [ Approve or Dispute ]
┌────────────────────────┐
│ PayPal Vault Token &   │
│ Orders v2 Capture      │
└────────────────────────┘
```

---

## 2. Real PayPal REST API v2/v3 Integrations

1. **OAuth2 Token Handshake (`POST /v1/oauth2/token`)**:
   * Authenticates merchant `client_id` and `client_secret` dynamically via HTTP Basic Auth.
   * Auto-switches between PayPal Sandbox (`api-m.sandbox.paypal.com`) and Production (`api-m.paypal.com`).
2. **Payment Method Vaulting (`POST /v3/vault/setup-tokens`)**:
   * Provisions reusable vault setup tokens to authorize future conditional autopay executions without re-prompting the customer.
3. **Orders v2 Checkout & Immediate Capture (`POST /v2/checkout/orders` & `/capture`)**:
   * Dispatches real-time captures with vaulted payment sources and full purchase unit tracking once the AI firewall verifies conditions.
4. **Webhook Signature Verification (`POST /v1/notifications/verify-webhook-signature`)**:
   * Inspects `PAYPAL-TRANSMISSION-ID`, algorithm, and signature headers (case-insensitively) to ensure ingested events originate from verified PayPal systems.
   * In connected mode, unverified events are rejected with **401** instead of moving money. Demo mode still accepts local simulator calls.

---

## 3. Safety & Usability Hardening

The hackathon build is deliberately demo-friendly, but it no longer fakes the dangerous parts:

* **No fabricated live captures.** When PayPal credentials are missing or an API call fails, the response is labelled `simulated: true` / `mode: DRY_RUN` and the audit trace says *Simulated*. A failed live capture is logged as `PAYMENT_FAILED`, no totals move, and the mandate is re-armed.
* **One capture per blocked invoice.** Approving an override twice now returns **400**; only `BLOCKED_BY_AI` mandates can be force-captured or disputed.
* **Lifecycle guards.** Pause/Resume only applies to `ACTIVE_WAITING`/`PAUSED`. Finished mandates must be explicitly **re-armed**, so an old rule cannot silently start spending again.
* **Input validation.** Rule creation rejects missing titles, bad emails, negative amounts, unknown categories/operators, and ignores any client-forged `status`.
* **No secret leakage.** `/api/state` exposes only whether a webhook signing secret is configured, never the secret itself.
* **Optional webhook auth for external triggers.** Set `AUTOPAY_REQUIRE_WEBHOOK_SECRET=1` to require `X-Webhook-Secret` on `POST /api/webhooks/external`.
* **Editable NLP preview.** Parsed fields are shown before arming; the user can correct payee, amount, category, operator, or threshold.
* **Honest demo mode.** The header shows `DEMO MODE`, settled totals are annotated as simulated, and simulate/real behaviour is visible in the audit ledger.
* **One-click judge controls.** *Load demo scenario* and *Reset demo* seed/clear the SpeedNet bill-cap and paycheck→rent mandates without touching credentials.
* **Better simulator + ledger.** Every mandate category can be simulated, and audit entries expand to show the full AI reasoning steps, PayPal API trace, and raw payload.

---

## 4. Relational Database Schema (`autopay.db`)

Initializes fresh schema with zero pre-seeded fake data across 4 relational tables:

* **`paypal_account`**: Stores active credentials, environment switch (`sandbox`/`live`), vault tokens, running `total_saved_by_ai`, and `total_auto_settled` totals. Credentials never leave the server; the API masks the client secret.
* **`rules`**: Stores user-configured conditional mandates (`CHAIN_REACTION`, `SPLIT_PAYMENT`, `BILL_CAP`, `MILESTONE`, `PRICE_DROP`), operator bounds (`>=`, `<=`, `==`), threshold values, and assigned PayPal Vault tokens.
* **`audit_logs`**: Immutable security ledger containing incoming payload snippets, structured step-by-step reasoning steps (`[✓]`, `[✗]`, `[!]`), and exact PayPal API execution traces.
* **`system_config`**: Key-value storage managing `gemini_api_key`, `webhook_secret_key`, and environment runtime modes.

---

## 5. Quickstart Guide (Local Setup)

### Prerequisites
* Python 3.10+
* Git

### Installation
```bash
# 1. Clone the repository
git clone https://github.com/<your-username>/paypal-conditional-autopay.git
cd paypal-conditional-autopay

# 2. Create and activate a virtual environment (optional but recommended)
python -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# 3. Install required dependencies
pip install -r requirements.txt
```

### Running the Test Suite
Verify all API routes, database transitions, AI firewall checks, and the new safety guards (double-override protection, validation, lifecycle state) pass:
```bash
python test_app.py
```
Expected: **12 tests, OK**.

### Launching the Application
```bash
python app.py
```
Open **[http://localhost:5000](http://localhost:5000)** in your browser.

Optional runtime environment variables:
```bash
PORT=5000 FLASK_DEBUG=0 python app.py
# Require a shared secret on external webhooks:
AUTOPAY_REQUIRE_WEBHOOK_SECRET=1 python app.py
```

> **Note for Judges (Offline / Zero-Config Testing):**  
> The application runs in **Demo Mode** out of the box: create rules, parse natural language, and fire webhooks without credentials. Captures are simulated locally and clearly labelled `simulated` in the audit ledger. To test with live PayPal Sandbox accounts, open **Settings**, enter the Sandbox Client ID/Secret, and save; the app performs a real OAuth2 handshake. Leaving the secret field blank keeps the previously saved value, so you can update only the Gemini key.

---

## 6. Judge Testing Script (Interactive Webhook Simulation)

While the server is running on `http://localhost:5000`, either use the UI simulator (**⚡ Trigger Webhook**) or run these commands in a separate terminal.

### Step 0: Load the Demo Scenario (one click)
Click **Load demo scenario** in the empty *Mandates* panel (or call the endpoint) to arm two mandates:
```bash
curl -X POST http://localhost:5000/api/demo/seed
```
This creates the **SpeedNet Wi-Fi Cap ($50)** and **Payday First, Rent Second ($500 after $1,000)** mandates. They start in `ACTIVE_WAITING`.

### Step 1: Register your own $50 Cap Mandate (optional)
Type this into the rule box, click **Parse Rule with AI**, review the editable fields, then click **Arm AI Mandate Firewall**:
* *Natural language:* `"Auto-pay invoices from billing@speednet.com only if total is $50 or less"`
* *Category:* `BILL_CAP`
* *Target Vendor:* `SpeedNet Wi-Fi`

> Try `"Pay my Wi-Fi bill no more than $50"` too: the parser now correctly reads negations as `≤`, not `≥`.

### Step 2: Test a Fair Bill (Pass & Settle)
Simulate a normal $45 monthly bill. The AI Firewall approves the transaction and triggers capture:
```bash
curl -X POST http://localhost:5000/api/webhooks/external \
  -H "Content-Type: application/json" \
  -d '{
    "trigger_source": "SpeedNet Wi-Fi",
    "amount": 45.00,
    "event_type": "INVOICE_BILLING"
  }'
```
*Expected Result:* Returns `verdict: "CAPTURED"` with `simulated: true` in demo mode, moves $45 into settled totals, status updates to `EXECUTED`.

### Step 3: Test a Spiked Bill (AI Firewall Block)
Simulate a spiked bill of $85 (exceeding the $50 cap):
```bash
curl -X POST http://localhost:5000/api/webhooks/external \
  -H "Content-Type: application/json" \
  -d '{
    "trigger_source": "SpeedNet Wi-Fi",
    "amount": 85.00,
    "event_type": "INVOICE_BILLING"
  }'
```
*Expected Result:* Returns `verdict: "BLOCKED"`, $0 deducted, mandate changes to `BLOCKED_BY_AI`, adds $85 to "Protected by AI", and renders the Human-in-the-Loop review card.

Click **Approve & pay** once — it settles. Clicking the same action twice is rejected with **400**, preventing a double charge. Use **Reject & dispute** on another blocked mandate to close it without moving money.

### Step 4: Simulate Downstream PayPal Chain Reaction
Simulate an incoming PayPal paycheck (`PAYMENT.CAPTURE.COMPLETED`) that automatically settles downstream rent only when sufficient funds arrive:
```bash
curl -X POST http://localhost:5000/api/webhooks/paypal \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "PAYMENT.CAPTURE.COMPLETED",
    "summary": "Employer Paycheck ACH Deposit",
    "resource": {
      "amount": {
        "value": "1200.00",
        "currency_code": "USD"
      }
    }
  }'
```
*Expected Result:* The $500 rent mandate fires once. A $600 paycheck produces `HOLD_INSUFFICIENT_TRIGGER` and keeps the mandate armed. In live mode, unsigned PayPal webhooks are rejected with **401**.

### Step 5: Review the Audit Ledger
Expand any ledger row to see the full AI reasoning steps, the exact PayPal API trace (including whether it was simulated), and the raw inbound payload. Use the verdict filter to isolate captured, blocked, held, failed, or human actions.

### Step 6: Reset Between Runs
Click **Reset demo** (keeps credentials) to clear mandates, audit logs, and running totals.

---

## 7. API Reference

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/state` | Metrics, mandates, audit ledger, connection status (no secrets) |
| `POST` | `/api/settings/paypal` | Save credentials + live OAuth2 handshake (blank fields keep existing values) |
| `POST` | `/api/paypal/vault-setup` | Provision a PayPal Vault setup token |
| `POST` | `/api/parse-rule` | Parse natural language into a structured mandate |
| `GET/POST` | `/api/rules` | List or create mandates (server-side validation) |
| `PATCH` | `/api/rules/<id>/toggle` | Pause/resume an active mandate |
| `POST` | `/api/rules/<id>/rearm` | Re-activate a finished mandate |
| `DELETE` | `/api/rules/<id>` | Delete a mandate (audit history retained) |
| `POST` | `/api/rules/<id>/override` | Human force-capture of a blocked invoice (once) |
| `POST` | `/api/rules/<id>/reject` | Human dispute/rejection of a blocked invoice |
| `POST` | `/api/webhooks/paypal` | PayPal event ingestion (signature-enforced when connected) |
| `POST` | `/api/webhooks/external` | Vendor invoice / milestone / price events |
| `POST` | `/api/demo/seed` | Create the two judge demo mandates |
| `POST` | `/api/demo/reset` | Clear mandates, logs and totals (keeps config) |

---

## 8. Project Structure

```
├── ai_engine.py          # Gemini 2.5 Flash parsing & conditional firewall engine
├── app.py                # Flask REST endpoints, webhooks & audit routes
├── database.py           # SQLite relational operations & audit logging
├── paypal_client.py      # PayPal OAuth2, Vault v3, & Orders v2 REST client
├── test_app.py           # Comprehensive integration & unit test suite
├── requirements.txt      # Production Python dependencies
├── LICENSE               # Open-source MIT License
├── README.md             # Architecture documentation & judge testing guide
└── templates/
    └── index.html        # Interactive single-page dashboard & ledger UI
```

---

## 9. License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.