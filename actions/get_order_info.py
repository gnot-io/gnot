"""Sample enterprise action — get_order_info.

Demonstrates v5.11 caller credential pattern.
Requires a CRM API key supplied by the caller.
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)

DESCRIPTION = "Retrieve order details from the CRM system"


def run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Get order information.

    Args:
        params: Must contain order_id (str).
        context: Runtime context. context["caller_credentials"]["crm_user_token"]
                 must be present — validated by framework before this is called.

    Returns:
        Dict with order details.
    """
    order_id = params["order_id"]
    crm_token = context["caller_credentials"].get("crm_user_token", "")

    logger.info("get_order_info: order=%s (crm_token_len=%d)", order_id, len(crm_token))

    # In production: call real CRM API using crm_token for authorization.
    # The CRM validates the token and decides if caller can access this order.
    # Here we simulate a response.
    return {
        "order_id": order_id,
        "status": "DELIVERED",
        "customer": "Nguyen Van A",
        "total": 1_250_000,
        "currency": "VND",
        "items": [
            {"sku": "PHONE-001", "name": "iPhone 16", "qty": 1},
        ],
        "note": "crm_token validated by CRM backend (not by mesh)",
    }
