"""
Unit tests for services/couchdb_service.py
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from services.couchdb_service import CouchDBService


class TestCouchDBService:

    @pytest.fixture
    def service(self):
        svc = CouchDBService()
        yield svc

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        return session

    @staticmethod
    def _make_context_mgr(mock_return, *, status=200):
        """Helper to create a mock async context manager that returns mock_return."""
        mgr = MagicMock()
        mgr.__aenter__ = AsyncMock(return_value=mock_return)
        mgr.__aexit__ = AsyncMock(return_value=None)
        return mgr

    @pytest.mark.asyncio
    async def test_check_connection_success(self, service, mock_session):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"version": "3.2.0"})
        mock_session.get.return_value = self._make_context_mgr(mock_resp)

        with patch.object(service, "_get_session", AsyncMock(return_value=mock_session)):
            with patch("services.couchdb_service.COUCHDB_URL", "http://admin:pass@127.0.0.1:5984"):
                result = await service.check_connection()
                assert result is True

    @pytest.mark.asyncio
    async def test_check_connection_failure_status(self, service, mock_session):
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_session.get.return_value = self._make_context_mgr(mock_resp)

        with patch.object(service, "_get_session", AsyncMock(return_value=mock_session)):
            with patch("services.couchdb_service.COUCHDB_URL", "http://admin:pass@127.0.0.1:5984"):
                result = await service.check_connection()
                assert result is False

    @pytest.mark.asyncio
    async def test_check_connection_exception(self, service, mock_session):
        mock_session.get.side_effect = Exception("Connection refused")

        with patch.object(service, "_get_session", AsyncMock(return_value=mock_session)):
            with patch("services.couchdb_service.COUCHDB_URL", "http://admin:pass@127.0.0.1:5984"):
                result = await service.check_connection()
                assert result is False

    @pytest.mark.asyncio
    async def test_save_alert_success(self, service, mock_session):
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_session.post.return_value = self._make_context_mgr(mock_resp)

        payload = {"signal": "BULLISH C2", "ticker": "ES"}

        with patch.object(service, "_get_session", AsyncMock(return_value=mock_session)):
            result = await service.save_alert(payload)
            assert result is True

    @pytest.mark.asyncio
    async def test_save_alert_creates_db_on_404(self, service, mock_session):
        resp_404 = MagicMock()
        resp_404.status = 404
        resp_201 = MagicMock()
        resp_201.status = 201

        # First POST returns 404, retry POST returns 201
        mock_session.post.side_effect = [
            self._make_context_mgr(resp_404),
            self._make_context_mgr(resp_201),
        ]
        mock_session.put.return_value = self._make_context_mgr(resp_201)

        payload = {"signal": "BULLISH"}

        with patch.object(service, "_get_session", AsyncMock(return_value=mock_session)):
            result = await service.save_alert(payload)
            assert result is True
            assert mock_session.put.called

    @pytest.mark.asyncio
    async def test_save_alert_failure(self, service, mock_session):
        mock_session.post.side_effect = Exception("Network error")

        with patch.object(service, "_get_session", AsyncMock(return_value=mock_session)):
            result = await service.save_alert({"signal": "TEST"})
            assert result is False

    @pytest.mark.asyncio
    async def test_save_bias_new_document(self, service, mock_session):
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_session.put.return_value = self._make_context_mgr(mock_resp)

        payload = {"date": "2026-05-16", "biases": {"ES": "Bullish"}, "updated_at": "2026-05-16T07:00:00"}

        with patch.object(service, "_get_session", AsyncMock(return_value=mock_session)):
            with patch.object(service, "get_bias_by_date", AsyncMock(return_value=None)):
                result = await service.save_bias(payload)
                assert result is True

    @pytest.mark.asyncio
    async def test_save_bias_update_existing_document(self, service, mock_session):
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_session.put.return_value = self._make_context_mgr(mock_resp)

        existing = {"_id": "2026-05-16", "_rev": "1-abc123", "biases": {"ES": "Neutral"}}
        payload = {"date": "2026-05-16", "biases": {"ES": "Bullish"}, "updated_at": "2026-05-16T07:00:00"}

        with patch.object(service, "_get_session", AsyncMock(return_value=mock_session)):
            with patch.object(service, "get_bias_by_date", AsyncMock(return_value=existing)):
                result = await service.save_bias(payload)
                assert result is True
                assert payload["_rev"] == "1-abc123"

    @pytest.mark.asyncio
    async def test_save_bias_no_date_falls_back_to_post(self, service, mock_session):
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_session.post.return_value = self._make_context_mgr(mock_resp)

        payload = {"biases": {"ES": "Bullish"}}

        with patch.object(service, "_get_session", AsyncMock(return_value=mock_session)):
            result = await service.save_bias(payload)
            assert result is True

    @pytest.mark.asyncio
    async def test_get_bias_by_date_success(self, service, mock_session):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"date": "2026-05-16", "biases": {"ES": "Bullish"}})
        mock_session.get.return_value = self._make_context_mgr(mock_resp)

        with patch.object(service, "_get_session", AsyncMock(return_value=mock_session)):
            result = await service.get_bias_by_date("2026-05-16")
            assert result is not None
            assert result["date"] == "2026-05-16"

    @pytest.mark.asyncio
    async def test_get_bias_by_date_not_found(self, service, mock_session):
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_session.get.return_value = self._make_context_mgr(mock_resp)

        with patch.object(service, "_get_session", AsyncMock(return_value=mock_session)):
            result = await service.get_bias_by_date("2026-05-16")
            assert result is None

    @pytest.mark.asyncio
    async def test_get_bias_by_date_exception(self, service, mock_session):
        mock_session.get.side_effect = Exception("Timeout")

        with patch.object(service, "_get_session", AsyncMock(return_value=mock_session)):
            result = await service.get_bias_by_date("2026-05-16")
            assert result is None

    @pytest.mark.asyncio
    async def test_close_closes_session(self, service):
        mock_session = AsyncMock()
        mock_session.closed = False
        service._session = mock_session

        await service.close()
        mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_skips_when_closed(self, service):
        mock_session = AsyncMock()
        mock_session.closed = True
        service._session = mock_session

        await service.close()
        mock_session.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_skips_when_no_session(self, service):
        service._session = None
        await service.close()

    @pytest.mark.asyncio
    async def test_get_session_creates_new(self, service):
        service._session = None
        session = await service._get_session()
        assert session is not None

    @pytest.mark.asyncio
    async def test_get_session_reuses_existing(self, service):
        fake_session = MagicMock()
        fake_session.closed = False
        service._session = fake_session
        result = await service._get_session()
        assert result is fake_session
