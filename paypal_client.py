"""
PayPal Conditional Autopay — Real PayPal REST API v2/v3 Client
Direct integration with PayPal's official endpoints for:
- OAuth2 token authentication (POST /v1/oauth2/token)
- Payment Method Vaulting Setup Tokens (POST /v3/vault/setup-tokens)
- Orders v2 Creation and Immediate Capture (POST /v2/checkout/orders & /capture)
- Webhook signature validation (POST /v1/notifications/verify-webhook-signature)
"""

import os
import uuid
import json
import hmac
from datetime import datetime
import requests

SANDBOX_URL = "https://api-m.sandbox.paypal.com"
LIVE_URL = "https://api-m.paypal.com"


def get_base_url(env="sandbox"):
    return LIVE_URL if env.lower() == "live" else SANDBOX_URL


def get_access_token(client_id: str, client_secret: str, env="sandbox") -> dict:
    """
    Exchanges client_id and client_secret for a PayPal OAuth2 Bearer token.
    Calls POST /v1/oauth2/token with HTTP Basic Authentication.
    """
    if not client_id or not client_secret:
        raise ValueError("PayPal Client ID and Client Secret are required.")

    url = f"{get_base_url(env)}/v1/oauth2/token"
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en_US"
    }
    data = {"grant_type": "client_credentials"}

    try:
        resp = requests.post(
            url,
            auth=(client_id.strip(), client_secret.strip()),
            headers=headers,
            data=data,
            timeout=10
        )
        if resp.status_code == 200:
            token_data = resp.json()
            return {
                "success": True,
                "access_token": token_data.get("access_token"),
                "token_type": token_data.get("token_type", "Bearer"),
                "expires_in": token_data.get("expires_in", 32400),
                "scope": token_data.get("scope", "")
            }
        else:
            return {
                "success": False,
                "status_code": resp.status_code,
                "error": resp.json() if resp.text else resp.reason
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def test_connection(client_id: str, client_secret: str, env="sandbox") -> dict:
    """Performs live OAuth2 handshake with PayPal to verify merchant credentials."""
    res = get_access_token(client_id, client_secret, env)
    return res


def create_vault_setup_token(client_id: str, client_secret: str, env="sandbox", customer_id: str = None) -> dict:
    """
    Creates a PayPal Vault Setup Token (POST /v3/vault/setup-tokens)
    for authorized conditional autopay executions.
    """
    creds_present = bool(client_id and client_secret)
    token_res = get_access_token(client_id, client_secret, env) if creds_present else {"success": False}

    if creds_present:
        if not token_res.get("success"):
            return {
                "success": False,
                "simulated": False,
                "status": "OAUTH_FAILED",
                "error": token_res.get("error", "Could not authenticate with PayPal.")
            }

        access_token = token_res["access_token"]
        url = f"{get_base_url(env)}/v3/vault/setup-tokens"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "PayPal-Request-Id": str(uuid.uuid4())
        }
        payload = {
            "payment_source": {
                "paypal": {
                    "usage_type": "MERCHANT",
                    "experience_context": {
                        "brand_name": "Conditional Autopay AI",
                        "shipping_preference": "NO_SHIPPING",
                        "user_action": "CONTINUE",
                        "return_url": "https://example.com/vault-return",
                        "cancel_url": "https://example.com/vault-cancel"
                    }
                }
            }
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            if resp.status_code in [200, 201]:
                data = resp.json()
                return {
                    "success": True,
                    "simulated": False,
                    "vault_setup_token": data.get("id"),
                    "status": data.get("status", "CREATED"),
                    "links": data.get("links", []),
                    "raw_response": data
                }
            return {
                "success": False,
                "simulated": False,
                "status": "SETUP_TOKEN_FAILED",
                "error": resp.json() if resp.text else resp.reason
            }
        except Exception as e:
            return {
                "success": False,
                "simulated": False,
                "status": "REQUEST_FAILED",
                "error": str(e)
            }

    # No credentials -> clearly-labelled local simulation for the offline demo.
    mock_token = f"vt_setup_pp_{uuid.uuid4().hex[:12]}"
    return {
        "success": True,
        "simulated": True,
        "vault_setup_token": mock_token,
        "status": "APPROVED",
        "mode": "SANDBOX_MANDATE_STAGED",
        "description": "PayPal Vault Mandate Setup Token generated for Conditional Autopay (simulated)"
    }


def create_and_capture_order(
    recipient_email: str,
    amount: float,
    currency: str = "USD",
    client_id: str = None,
    client_secret: str = None,
    env: str = "sandbox",
    vault_token: str = None,
    description: str = "Conditional Autopay Mandate Execution"
) -> dict:
    """
    Executes a real or authentic-trace PayPal Orders v2 checkout & capture:
    1. POST /v2/checkout/orders
    2. POST /v2/checkout/orders/{id}/capture
    """
    amount_str = f"{amount:.2f}"
    order_id = f"ORD-PP-{uuid.uuid4().hex[:12].upper()}"
    capture_id = f"CAP-PP-{uuid.uuid4().hex[:12].upper()}"

    # If credentials are configured we MUST use the real API and report real
    # failures. Fabricating a COMPLETED capture here would mark money as settled
    # when PayPal never moved a cent.
    if client_id and client_secret:
        token_info = get_access_token(client_id, client_secret, env)
        if not token_info.get("success"):
            return {
                "success": False,
                "simulated": False,
                "status": "OAUTH_FAILED",
                "error": token_info.get("error", "Could not authenticate with PayPal."),
                "timestamp": datetime.now().isoformat()
            }

        access_token = token_info["access_token"]
        base_url = get_base_url(env)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "PayPal-Request-Id": str(uuid.uuid4())
        }

        order_payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {
                    "currency_code": currency,
                    "value": amount_str
                },
                "payee": {
                    "email_address": recipient_email
                },
                "description": description
            }]
        }
        # Charge the vaulted payment method (PayPal Vault v3 pattern) so the
        # mandate's consented token — not a fresh payer approval — funds the order.
        if vault_token:
            order_payload["payment_source"] = {
                "paypal": {"vault_id": vault_token}
            }

        try:
            # 1. Create Order
            order_resp = requests.post(f"{base_url}/v2/checkout/orders", headers=headers, json=order_payload, timeout=12)
            if order_resp.status_code not in [200, 201]:
                return {
                    "success": False,
                    "simulated": False,
                    "status": "ORDER_FAILED",
                    "error": order_resp.json() if order_resp.text else order_resp.reason,
                    "timestamp": datetime.now().isoformat()
                }

            real_order_id = order_resp.json()["id"]

            # 2. Capture Order
            capture_resp = requests.post(f"{base_url}/v2/checkout/orders/{real_order_id}/capture", headers=headers, timeout=12)
            if capture_resp.status_code not in [200, 201]:
                return {
                    "success": False,
                    "simulated": False,
                    "order_id": real_order_id,
                    "status": "CAPTURE_FAILED",
                    "error": capture_resp.json() if capture_resp.text else capture_resp.reason,
                    "timestamp": datetime.now().isoformat()
                }

            capture_data = capture_resp.json()
            return {
                "success": True,
                "simulated": False,
                "order_id": real_order_id,
                "status": capture_data.get("status", "COMPLETED"),
                "amount": amount,
                "currency": currency,
                "recipient_email": recipient_email,
                "environment": env,
                "api_endpoint": f"{base_url}/v2/checkout/orders/{real_order_id}/capture",
                "capture_details": capture_data,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            return {
                "success": False,
                "simulated": False,
                "status": "REQUEST_FAILED",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }

    # No credentials -> clearly-labelled local simulation for the offline demo.
    return {
        "success": True,
        "simulated": True,
        "mode": "DRY_RUN",
        "order_id": order_id,
        "capture_id": capture_id,
        "status": "COMPLETED",
        "intent": "CAPTURE",
        "amount": amount,
        "currency": currency,
        "recipient_email": recipient_email,
        "payment_source": {
            "vault_token": vault_token or "vt_paypal_vaulted_source",
            "type": "PAYPAL_VAULT"
        },
        "purchase_units": [{
            "reference_id": "default",
            "amount": {
                "currency_code": currency,
                "value": amount_str
            },
            "payee": {
                "email_address": recipient_email
            },
            "description": description,
            "payments": {
                "captures": [{
                    "id": capture_id,
                    "status": "COMPLETED",
                    "amount": {
                        "currency_code": currency,
                        "value": amount_str
                    },
                    "final_capture": True,
                    "seller_protection": {
                        "status": "ELIGIBLE"
                    },
                    "create_time": datetime.now().isoformat()
                }]
            }
        }],
        "environment": env,
        "execution_engine": "PayPal Orders v2 (Simulated)",
        "timestamp": datetime.now().isoformat()
    }


def verify_webhook_signature(headers: dict, body_str: str, webhook_id: str, client_id: str = None, client_secret: str = None, env="sandbox") -> bool:
    """Validates PayPal webhook events using POST /v1/notifications/verify-webhook-signature.

    Flask header names are case-insensitive but the dict keys are not, so we
    normalise them before building the verification payload. When live
    credentials are configured we fail closed (return False) if PayPal cannot
    verify the event.
    """
    # Normalise header casing once (Flask emits e.g. "Paypal-Transmission-Id")
    h = {str(k).lower(): v for k, v in (headers or {}).items()}

    if not client_id or not client_secret or not webhook_id:
        # Demo / offline mode. A shared secret, when presented, must actually
        # match (constant-time compare) — presenting any non-empty value must
        # not pass. Events without either mechanism are handled downstream.
        provided_secret = h.get("x-webhook-secret")
        if provided_secret:
            return hmac.compare_digest(str(provided_secret), str(webhook_id))
        required = ["paypal-transmission-id", "paypal-transmission-time",
                    "paypal-cert-url", "paypal-auth-algo", "paypal-transmission-sig"]
        return any(h.get(name) for name in required)

    token_res = get_access_token(client_id, client_secret, env)
    if not token_res.get("success"):
        return False

    url = f"{get_base_url(env)}/v1/notifications/verify-webhook-signature"
    verify_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token_res['access_token']}"
    }
    payload = {
        "auth_algo": h.get("paypal-auth-algo", ""),
        "cert_url": h.get("paypal-cert-url", ""),
        "transmission_id": h.get("paypal-transmission-id", ""),
        "transmission_sig": h.get("paypal-transmission-sig", ""),
        "transmission_time": h.get("paypal-transmission-time", ""),
        "webhook_id": webhook_id,
        "webhook_event": json.loads(body_str) if isinstance(body_str, str) else body_str
    }
    try:
        resp = requests.post(url, headers=verify_headers, json=payload, timeout=8)
        if resp.status_code == 200:
            return resp.json().get("verification_status") == "SUCCESS"
    except Exception:
        pass
    return False
