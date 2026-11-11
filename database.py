"""
PayPal Conditional Autopay — Production Database Module
Relational SQLite engine (autopay.db) with atomic transactions and ZERO pre-seeded fake data.
Manages 4 production tables: paypal_account, rules, audit_logs, system_config.
"""

import sqlite3
import json
import os
import uuid
from datetime import datetime

DB_PATH = os.environ.get(
    "AUTOPAY_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'autopay.db')
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Initializes clean production schema without pre-seeded rules or balances."""
    conn = get_db()
    with conn:
        # 1. paypal_account table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paypal_account (
                id INTEGER PRIMARY KEY,
                client_id TEXT DEFAULT '',
                client_secret TEXT DEFAULT '',
                environment TEXT DEFAULT 'sandbox',
                merchant_email TEXT DEFAULT '',
                vault_setup_token TEXT DEFAULT '',
                vault_payment_token TEXT DEFAULT '',
                connected_status TEXT DEFAULT 'DISCONNECTED',
                total_saved_by_ai REAL DEFAULT 0.00,
                total_auto_settled REAL DEFAULT 0.00,
                updated_at TEXT NOT NULL
            )
        """)

        # 2. rules table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                natural_language_rule TEXT NOT NULL,
                rule_category TEXT NOT NULL,
                trigger_source TEXT NOT NULL,
                recipient_paypal_email TEXT NOT NULL,
                recipient_name TEXT NOT NULL,
                payment_amount REAL NOT NULL,
                currency TEXT DEFAULT 'USD',
                condition_operator TEXT NOT NULL,
                condition_threshold REAL NOT NULL,
                vault_token_id TEXT NOT NULL,
                recurring INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ACTIVE_WAITING',
                pending_invoice_amount REAL,
                pending_event_payload TEXT,
                execution_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Migrate older databases that predate the execution counter.
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(rules)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if "execution_count" not in existing_cols:
            conn.execute("ALTER TABLE rules ADD COLUMN execution_count INTEGER NOT NULL DEFAULT 0")

        # 3. audit_logs table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                rule_id INTEGER,
                event_source TEXT NOT NULL,
                event_title TEXT NOT NULL,
                raw_webhook_json TEXT NOT NULL,
                ai_reasoning_steps TEXT NOT NULL,
                paypal_api_trace TEXT NOT NULL,
                verdict TEXT NOT NULL,
                amount_impact REAL NOT NULL
            )
        """)

        # 4. system_config table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Lightweight migration for databases created before "recurring" existed.
        rule_columns = [row[1] for row in conn.execute("PRAGMA table_info(rules)")]
        if "recurring" not in rule_columns:
            conn.execute("ALTER TABLE rules ADD COLUMN recurring INTEGER NOT NULL DEFAULT 0")

        # 5. processed_events table — webhook idempotency ledger.
        # PayPal retries webhook deliveries; the same event id must never
        # move money twice.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_events (
                event_id TEXT PRIMARY KEY,
                event_source TEXT,
                processed_at TEXT NOT NULL
            )
        """)

        # Ensure singleton paypal_account record exists in clean disconnected state
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM paypal_account WHERE id = 1")
        if cur.fetchone()[0] == 0:
            now_iso = datetime.now().isoformat()
            cur.execute("""
                INSERT INTO paypal_account (
                    id, client_id, client_secret, environment, merchant_email,
                    vault_setup_token, vault_payment_token, connected_status,
                    total_saved_by_ai, total_auto_settled, updated_at
                ) VALUES (1, '', '', 'sandbox', '', '', '', 'DISCONNECTED', 0.00, 0.00, ?)
            """, (now_iso,))

        # Initialize default system configs if absent
        defaults = {
            "webhook_secret_key": f"whsec_{uuid.uuid4().hex}",
            "gemini_api_key": "",
            "mode": "production"
        }
        for k, v in defaults.items():
            cur.execute("SELECT COUNT(*) FROM system_config WHERE key = ?", (k,))
            if cur.fetchone()[0] == 0:
                cur.execute("INSERT INTO system_config (key, value) VALUES (?, ?)", (k, v))

    conn.close()


def get_account():
    """Retrieves account record (client secrets masked for safety in API responses)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM paypal_account WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        "id": 1,
        "client_id": "",
        "client_secret": "",
        "environment": "sandbox",
        "merchant_email": "",
        "vault_setup_token": "",
        "vault_payment_token": "",
        "connected_status": "DISCONNECTED",
        "total_saved_by_ai": 0.00,
        "total_auto_settled": 0.00,
        "updated_at": datetime.now().isoformat()
    }


def update_account_credentials(client_id, client_secret, environment, merchant_email, status="CONNECTED"):
    conn = get_db()
    now_iso = datetime.now().isoformat()
    with conn:
        conn.execute("""
            UPDATE paypal_account
            SET client_id = ?,
                client_secret = ?,
                environment = ?,
                merchant_email = ?,
                connected_status = ?,
                updated_at = ?
            WHERE id = 1
        """, (client_id, client_secret, environment, merchant_email, status, now_iso))
    conn.close()
    return get_account()


def update_vault_tokens(vault_setup_token=None, vault_payment_token=None):
    conn = get_db()
    now_iso = datetime.now().isoformat()
    updates = []
    params = []
    if vault_setup_token is not None:
        updates.append("vault_setup_token = ?")
        params.append(vault_setup_token)
    if vault_payment_token is not None:
        updates.append("vault_payment_token = ?")
        params.append(vault_payment_token)
    updates.append("updated_at = ?")
    params.append(now_iso)
    params.append(1)

    with conn:
        conn.execute(f"UPDATE paypal_account SET {', '.join(updates)} WHERE id = ?", params)
    conn.close()
    return get_account()


def increment_metrics(saved_delta=0.0, settled_delta=0.0):
    conn = get_db()
    now_iso = datetime.now().isoformat()
    with conn:
        conn.execute("""
            UPDATE paypal_account
            SET total_saved_by_ai = MAX(0, total_saved_by_ai + ?),
                total_auto_settled = MAX(0, total_auto_settled + ?),
                updated_at = ?
            WHERE id = 1
        """, (float(saved_delta), float(settled_delta), now_iso))
    conn.close()
    return get_account()


def get_rules(status=None):
    conn = get_db()
    cur = conn.cursor()
    if status:
        cur.execute("SELECT * FROM rules WHERE status = ? ORDER BY id DESC", (status,))
    else:
        cur.execute("SELECT * FROM rules ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rule_by_id(rule_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rules WHERE id = ?", (rule_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def create_rule(data):
    conn = get_db()
    now_iso = datetime.now().isoformat()
    vault_token_id = data.get("vault_token_id")
    if not vault_token_id:
        acc = get_account()
        vault_token_id = acc.get("vault_setup_token") or f"v_tok_{uuid.uuid4().hex[:12]}"

    with conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO rules (
                title, natural_language_rule, rule_category, trigger_source,
                recipient_paypal_email, recipient_name, payment_amount, currency,
                condition_operator, condition_threshold, vault_token_id, recurring, status,
                pending_invoice_amount, pending_event_payload, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("title", "Custom Autopay Mandate"),
            data.get("natural_language_rule", ""),
            data.get("rule_category", "BILL_CAP"),
            data.get("trigger_source", "Inbound Webhook"),
            data.get("recipient_paypal_email", "merchant@example.com"),
            data.get("recipient_name", "Vendor Merchant"),
            float(data.get("payment_amount", 0.0)),
            data.get("currency", "USD"),
            data.get("condition_operator", "<="),
            float(data.get("condition_threshold", 0.0)),
            vault_token_id,
            1 if data.get("recurring") else 0,
            data.get("status", "ACTIVE_WAITING"),
            data.get("pending_invoice_amount"),
            data.get("pending_event_payload"),
            now_iso,
            now_iso
        ))
        new_id = cur.lastrowid
    conn.close()
    return get_rule_by_id(new_id)


def update_rule_status(rule_id, status, pending_invoice_amount=None, pending_event_payload=None):
    conn = get_db()
    now_iso = datetime.now().isoformat()
    payload_str = pending_event_payload
    if isinstance(pending_event_payload, (dict, list)):
        payload_str = json.dumps(pending_event_payload)

    with conn:
        conn.execute("""
            UPDATE rules
            SET status = ?,
                pending_invoice_amount = ?,
                pending_event_payload = ?,
                updated_at = ?
            WHERE id = ?
        """, (status, pending_invoice_amount, payload_str, now_iso, rule_id))
    conn.close()
    return get_rule_by_id(rule_id)


def toggle_rule_pause(rule_id):
    """Pauses an ACTIVE_WAITING mandate or resumes a PAUSED one.

    Terminal states (EXECUTED / BLOCKED_BY_AI / REJECTED / OVERRIDDEN) must not
    be silently re-armed by the pause toggle; use rearm_rule for that.
    """
    rule = get_rule_by_id(rule_id)
    if not rule:
        return None
    if rule["status"] not in ("ACTIVE_WAITING", "PAUSED"):
        raise ValueError(
            f"Mandate is {rule['status']} and cannot be paused/resumed. "
            "Use the re-arm action instead."
        )
    new_status = "PAUSED" if rule["status"] == "ACTIVE_WAITING" else "ACTIVE_WAITING"
    return update_rule_status(rule_id, new_status, rule["pending_invoice_amount"], rule["pending_event_payload"])


def rearm_rule(rule_id):
    """Re-activates a finished mandate (EXECUTED / REJECTED / OVERRIDDEN / PAUSED)."""
    rule = get_rule_by_id(rule_id)
    if not rule:
        return None
    if rule["status"] == "BLOCKED_BY_AI":
        raise ValueError("Blocked mandates must be approved or rejected before re-arming.")
    return update_rule_status(rule_id, "ACTIVE_WAITING", None, None)


def delete_rule(rule_id):
    conn = get_db()
    with conn:
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    conn.close()
    return True


def log_audit(rule_id, event_source, event_title, raw_webhook_json, ai_reasoning_steps, paypal_api_trace, verdict, amount_impact):
    conn = get_db()
    now_iso = datetime.now().isoformat()
    raw_str = raw_webhook_json if isinstance(raw_webhook_json, str) else json.dumps(raw_webhook_json, indent=2)
    reasoning_str = ai_reasoning_steps if isinstance(ai_reasoning_steps, str) else json.dumps(ai_reasoning_steps, indent=2)
    api_str = paypal_api_trace if isinstance(paypal_api_trace, str) else json.dumps(paypal_api_trace, indent=2)

    with conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO audit_logs (
                timestamp, rule_id, event_source, event_title,
                raw_webhook_json, ai_reasoning_steps, paypal_api_trace,
                verdict, amount_impact
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now_iso, rule_id, event_source, event_title,
            raw_str, reasoning_str, api_str,
            verdict, float(amount_impact)
        ))
        log_id = cur.lastrowid
    conn.close()
    return log_id


def get_audit_logs(limit=100):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        for json_field in ["raw_webhook_json", "ai_reasoning_steps", "paypal_api_trace"]:
            try:
                d[json_field] = json.loads(d[json_field])
            except Exception:
                pass
        results.append(d)
    return results


def get_config(key, default=""):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM system_config WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_config(key, value):
    conn = get_db()
    with conn:
        conn.execute("""
            INSERT INTO system_config (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))
    conn.close()
    return True


def clear_rules_and_logs():
    """Wipes mandates + audit ledger while preserving account/config (demo reset)."""
    conn = get_db()
    with conn:
        conn.execute("DELETE FROM rules")
        conn.execute("DELETE FROM audit_logs")
        conn.execute("DELETE FROM processed_events")
        conn.execute("""
            UPDATE paypal_account
            SET total_saved_by_ai = 0.00, total_auto_settled = 0.00
            WHERE id = 1
        """)
    conn.close()
    return True


def increment_rule_executions(rule_id):
    """Bumps a mandate's execution counter (recurring bill payments, overrides)."""
    conn = get_db()
    now_iso = datetime.now().isoformat()
    with conn:
        conn.execute("""
            UPDATE rules
            SET execution_count = execution_count + 1,
                updated_at = ?
            WHERE id = ?
        """, (now_iso, rule_id))
    conn.close()
    return get_rule_by_id(rule_id)


def was_event_processed(event_id):
    """True if this exact webhook event id has already been handled."""
    if not event_id:
        return False
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM processed_events WHERE event_id = ?", (str(event_id),))
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_event_processed(event_id, event_source=""):
    """Records an event id as handled. Returns False if it was already present."""
    if not event_id:
        return False
    conn = get_db()
    with conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO processed_events (event_id, event_source, processed_at)
            VALUES (?, ?, ?)
        """, (str(event_id), str(event_source or ""), datetime.now().isoformat()))
        inserted = cur.rowcount > 0
    conn.close()
    return inserted
