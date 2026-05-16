"""
Unit tests for webhook_server.py
"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp import web
from webhook_server import create_webhook_app, start_webhook_server

class TestWebhookServer:

    @pytest.fixture
    def bot(self):
        return MagicMock()

    @pytest.mark.asyncio
    async def test_webhook_valid_payload_no_token(self, aiohttp_client, bot):
        """Verifies 200 OK and bot dispatch when no token is configured."""
        app = create_webhook_app(bot)
        client = await aiohttp_client(app)
        payload = {"message": "test", "signal": "BULLISH"}
        
        with patch("webhook_server.WEBHOOK_TOKEN", ""):
            resp = await client.post("/webhook", json=payload)
            
        assert resp.status == 200
        bot.dispatch.assert_called_once_with("tradingview_alert", payload)

    @pytest.mark.asyncio
    async def test_webhook_valid_token(self, aiohttp_client, bot):
        """Verifies 200 OK when correct token is provided in query."""
        app = create_webhook_app(bot)
        client = await aiohttp_client(app)
        payload = {"message": "test"}
        token = "secret123"
        
        with patch("webhook_server.WEBHOOK_TOKEN", token):
            resp = await client.post(f"/webhook?token={token}", json=payload)
            
        assert resp.status == 200
        bot.dispatch.assert_called_once_with("tradingview_alert", payload)

    @pytest.mark.asyncio
    async def test_webhook_invalid_token(self, aiohttp_client, bot):
        """Verifies 403 Forbidden when wrong token is provided."""
        app = create_webhook_app(bot)
        client = await aiohttp_client(app)
        payload = {"message": "test"}
        
        with patch("webhook_server.WEBHOOK_TOKEN", "correct_token"):
            resp = await client.post("/webhook?token=wrong_token", json=payload)
            
        assert resp.status == 403
        bot.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_webhook_missing_token(self, aiohttp_client, bot):
        """Verifies 403 Forbidden when token is required but not provided."""
        app = create_webhook_app(bot)
        client = await aiohttp_client(app)
        payload = {"message": "test"}
        
        with patch("webhook_server.WEBHOOK_TOKEN", "required_token"):
            resp = await client.post("/webhook", json=payload)
            
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_webhook_invalid_json(self, aiohttp_client, bot):
        """Verifies 400 Bad Request on malformed JSON."""
        app = create_webhook_app(bot)
        client = await aiohttp_client(app)
        with patch("webhook_server.WEBHOOK_TOKEN", ""):
            resp = await client.post("/webhook", data="not json", headers={"Content-Type": "application/json"})
            
        assert resp.status == 400
        bot.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_webhook_server_starts_and_returns_runner(self, bot):
        with patch("webhook_server.web.AppRunner") as mock_runner_class, \
             patch("webhook_server.WEBHOOK_PORT", 9000):
            mock_runner = MagicMock()
            mock_runner.setup = AsyncMock()
            mock_runner_class.return_value = mock_runner

            with patch("webhook_server.web.TCPSite") as mock_site_class:
                mock_site = MagicMock()
                mock_site.start = AsyncMock()
                mock_site_class.return_value = mock_site

                runner = await start_webhook_server(bot)
                assert runner is mock_runner
                mock_runner.setup.assert_called_once()
                mock_site.start.assert_called_once()
                mock_site_class.assert_called_once_with(mock_runner, "0.0.0.0", 9000)
