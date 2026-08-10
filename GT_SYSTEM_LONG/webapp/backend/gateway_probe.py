"""
TCP-level health probe for the IB Gateway / TWS API port.

Why TCP-only (no login attempt): every successful IBKR login consumes
one of the user's session slots. A health probe that logs in every 5s
would race with the bots' connect attempts. A bare TCP connect is
enough to distinguish the three states we care about:

  reachable=True  → something is listening on (host, port); TWS is up.
  reachable=False → connect refused / timeout; TWS is off or the
                    security group blocks us.

We deliberately don't read the IBKR API banner — TWS only sends one
after a v100 handshake, which means writing API frames, which the
probe should not do.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from .schemas import GatewayStatus


DEFAULT_HOST = os.environ.get("GT_WEBAPP_TWS_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("GT_WEBAPP_TWS_PORT", "7496"))
PROBE_TIMEOUT_S = float(os.environ.get("GT_WEBAPP_PROBE_TIMEOUT_S", "1.0"))


async def probe(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> GatewayStatus:
    """Open a TCP connection to (host, port), close it immediately.

    Bounded by PROBE_TIMEOUT_S so a network black-hole doesn't stall
    the WebSocket broadcast loop.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=PROBE_TIMEOUT_S)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return GatewayStatus(host=host, port=port, reachable=True,
                             last_checked=now)
    except asyncio.TimeoutError:
        return GatewayStatus(host=host, port=port, reachable=False,
                             last_checked=now, error="timeout")
    except OSError as e:
        return GatewayStatus(host=host, port=port, reachable=False,
                             last_checked=now, error=f"{type(e).__name__}: {e}")
