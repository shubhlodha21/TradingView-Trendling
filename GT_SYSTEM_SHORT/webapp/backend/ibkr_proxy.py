"""
Manual-order proxy to IBKR.

This routes one-off orders submitted from the right-panel ticket
directly to TWS, using a dedicated `client_id` distinct from any
running run_live.py bot. Without the separate ID, the bot's order
registry could see the manual order's status events and corrupt its
own cycle state.

STATUS: scaffolded but not wired. Submitting a manual order returns
HTTP 501. Reason: routing live orders through the webapp is exactly
the kind of feature that needs deliberate sign-off (it's real money
flowing through new code with no audit trail integration yet). The
shape is here so the frontend can build against the API contract;
the implementation lands in the next iteration once we agree on:

  * Audit log destination (separate CSV vs reuse existing audit/?)
  * Notional / loss-per-day caps for manual orders (apply the same
    portfolio risk gate we built in src/strategy/risk.py?)
  * Confirm-step UX (two-click submit? typed confirmation?)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from .schemas import ManualOrderRequest, ManualOrderResponse


# Dedicated client_id for manual orders. MUST NOT collide with any
# value the user might set via the launch form. We default to 99
# because the form's client_id Field has max=999 but the bots'
# typical pattern is single digits.
MANUAL_CLIENT_ID = int(os.environ.get("GT_WEBAPP_MANUAL_CLIENT_ID", "99"))

# Hard cap on manual order notional. Prevents a fat-finger from
# blowing past the same dollar gate the bot enforces in risk.py.
MAX_MANUAL_NOTIONAL = float(os.environ.get("GT_WEBAPP_MAX_MANUAL_NOTIONAL", "25000"))


def validate(req: ManualOrderRequest) -> None:
    """Pre-flight validation that doesn't need an IBKR round-trip.

    Catches the obvious wrong shapes (LIMIT without limit_price, etc.)
    before we burn a session slot connecting to TWS."""
    if req.order_type in ("LIMIT", "STOP_LIMIT") and req.limit_price is None:
        raise HTTPException(400, f"{req.order_type} requires limit_price")
    if req.order_type in ("STOP", "STOP_LIMIT") and req.stop_price is None:
        raise HTTPException(400, f"{req.order_type} requires stop_price")
    if req.order_type == "MARKET" and (req.limit_price or req.stop_price):
        raise HTTPException(400, "MARKET must not set limit_price or stop_price")
    if req.acknowledged_notional > MAX_MANUAL_NOTIONAL:
        raise HTTPException(403, f"Notional ${req.acknowledged_notional:,.0f} exceeds "
                                 f"manual cap ${MAX_MANUAL_NOTIONAL:,.0f}")


async def submit(req: ManualOrderRequest) -> ManualOrderResponse:
    """Place the order against TWS.

    Not implemented — see module docstring for the open questions
    that need answers before this is safe to enable."""
    validate(req)
    raise HTTPException(
        status_code=501,
        detail=(
            "Manual order routing is scaffolded but not enabled. "
            "Audit destination, manual-order risk gates, and the confirmation "
            "UX need to be agreed before this connects to real IBKR. "
            "See webapp/backend/ibkr_proxy.py docstring."
        ),
    )


def synthetic_response(req: ManualOrderRequest) -> ManualOrderResponse:
    """Used by the (paper-only) `/api/orders?dry_run=true` path so the
    frontend can exercise the form without hitting the 501."""
    return ManualOrderResponse(
        order_id=f"DRY_{uuid.uuid4().hex[:8].upper()}",
        status="DRY_RUN",
        submitted_at=datetime.now(timezone.utc).isoformat(),
    )
