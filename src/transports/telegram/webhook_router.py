"""Telegram webhook FastAPI router.

Mounts at /transports/telegram/ (via TelegramBridge.get_fastapi_router()).
Provides:
  - POST /bots/{bot_id}/update  — inbound webhook from Telegram
  - POST /bots               — register new bot
  - GET  /bots               — list all bots
  - GET  /bots/{bot_id}      — bot status
  - DELETE /bots/{bot_id}    — deregister bot
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from transports.telegram.bridge import TelegramBridge

logger = logging.getLogger(__name__)


def build_webhook_router(bridge: "TelegramBridge") -> APIRouter:
    """Build FastAPI router bound to a TelegramBridge instance.

    Returns APIRouter to be mounted at /transports/telegram/ by server.py.
    """
    router = APIRouter()

    # ── Inbound webhook from Telegram (POST /bots/{bot_id}/update) ─────────

    @router.post("/bots/{bot_id}/update")
    async def telegram_webhook(bot_id: str, request: Request) -> JSONResponse:
        """Receive update from Telegram for a specific bot.

        Must return 200 quickly — Telegram retries if response takes > 3s.
        Actual processing is fire-and-forget via create_task.
        """
        try:
            update = await request.json()
        except Exception as exc:
            logger.warning("telegram_webhook: invalid JSON body: %s", exc)
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

        instance = bridge.get_instance(bot_id)
        if instance is None:
            logger.warning("telegram_webhook: unknown bot_id=%s", bot_id)
            return JSONResponse({"ok": False, "error": "bot not found"}, status_code=404)

        # Fire-and-forget — must not block
        await instance.process_webhook_update(update)
        return JSONResponse({"ok": True})

    # ── Bot management endpoints ────────────────────────────────────────────

    @router.post("/bots")
    async def register_bot(request: Request) -> JSONResponse:
        """POST /bots — register a new Telegram bot.

        Body:
          {
            "bot_token": "...",
            "owner_user_id": "alice",
            "cluster_id": "cluster-A",
            "webhook_url": "https://..."   // optional
          }
        """
        try:
            body = await request.json()
        except Exception as exc:
            return JSONResponse({"error": f"Invalid JSON: {exc}"}, status_code=400)

        bot_token = body.get("bot_token", "").strip()
        owner_user_id = body.get("owner_user_id", "").strip()
        cluster_id = body.get("cluster_id", "").strip()
        webhook_url = body.get("webhook_url")

        if not bot_token or not owner_user_id or not cluster_id:
            return JSONResponse(
                {"error": "bot_token, owner_user_id, and cluster_id are required"},
                status_code=400,
            )

        try:
            result = await bridge.register_bot(
                bot_token=bot_token,
                owner_user_id=owner_user_id,
                cluster_id=cluster_id,
                webhook_url=webhook_url,
            )
            return JSONResponse(result, status_code=201)
        except Exception as exc:
            logger.error("register_bot failed: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    @router.get("/bots")
    async def list_bots() -> JSONResponse:
        """GET /bots — list all registered bots and their status."""
        bots = bridge.list_bots()
        return JSONResponse({"bots": bots, "total": len(bots)})

    @router.get("/bots/{bot_id}")
    async def get_bot(bot_id: str) -> JSONResponse:
        """GET /bots/{bot_id} — get bot status."""
        info = bridge.get_bot_info(bot_id)
        if info is None:
            return JSONResponse({"error": f"Bot {bot_id} not found"}, status_code=404)
        return JSONResponse(info)

    @router.delete("/bots/{bot_id}")
    async def deregister_bot(bot_id: str) -> JSONResponse:
        """DELETE /bots/{bot_id} — stop and deregister a bot."""
        try:
            await bridge.deregister_bot(bot_id)
            return JSONResponse({"deregistered": True, "bot_id": bot_id})
        except KeyError:
            return JSONResponse({"error": f"Bot {bot_id} not found"}, status_code=404)
        except Exception as exc:
            logger.error("deregister_bot failed: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    return router
