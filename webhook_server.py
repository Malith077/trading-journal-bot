"""
Lightweight aiohttp webhook server that receives TradingView alerts
and dispatches them as custom bot events.

Runs alongside the Discord bot on a configurable port.
"""
import json
from aiohttp import web
from config import WEBHOOK_PORT, WEBHOOK_TOKEN


def create_webhook_app(bot):
    """Create and return the aiohttp Application wired to the Discord bot."""
    app = web.Application()

    async def handle_webhook(request: web.Request) -> web.Response:
        # --- Token validation ---
        if WEBHOOK_TOKEN:
            token = request.query.get("token", "")
            if token != WEBHOOK_TOKEN:
                return web.Response(status=403, text="Forbidden: invalid token")

        # --- Parse JSON body ---
        try:
            raw = await request.text()
            payload = json.loads(raw)
        except (json.JSONDecodeError, Exception):
            return web.Response(status=400, text="Bad Request: invalid JSON")

        # --- Dispatch to bot ---
        bot.dispatch("tradingview_alert", payload)
        return web.Response(status=200, text="OK")

    app.router.add_post("/webhook", handle_webhook)
    return app


async def start_webhook_server(bot):
    """Start the webhook server. Returns the runner for clean shutdown."""
    app = create_webhook_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    print(f"🌐 Webhook server listening on port {WEBHOOK_PORT}")
    return runner
