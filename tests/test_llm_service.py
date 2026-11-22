"""
Unit tests for services/llm_service.py and services/json_utils.py

Covers:
  - extract_json(): clean, wrapped, buried, nested, garbage, fallback isolation
  - data URI helpers and image encoding
  - model selection (text / vision / reasoning, per provider)
  - DeepSeek request shape: auth, messages, json mode, image blocks
  - Ollama request shape: raw base64, native format flag
  - retry / permanent-failure / empty-content behaviour
"""
import json
import base64
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.json_utils import extract_json
from services.llm_service import (
    LLMService, LLMError, to_data_uri, strip_data_uri, mime_for, encode_image_file,
)

FALLBACK = {"good_habits": [], "mistakes": []}


def _mock_session(status=200, payload=None, text=""):
    """Build a mock aiohttp session whose post() yields one canned response."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload or {})
    resp.text = AsyncMock(return_value=text)

    session = MagicMock()
    session.closed = False
    session.post.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def _deepseek_payload(content="hello"):
    return {"choices": [{"message": {"content": content}}]}


# ──────────────────────────────────────────────
# extract_json
# ──────────────────────────────────────────────

class TestExtractJson:

    def test_clean(self):
        assert extract_json('{"a": 1}', FALLBACK) == {"a": 1}

    def test_markdown_wrapped(self):
        assert extract_json('```json\n{"a": 1}\n```', FALLBACK) == {"a": 1}

    def test_buried_in_prose(self):
        assert extract_json('Sure!\n{"a": 1}\nHope that helps.', FALLBACK) == {"a": 1}

    def test_nested_object_survives(self):
        """The greedy pass must not truncate at the first inner closing brace."""
        raw = 'Result: {"outer": {"inner": [1, 2]}, "b": 3}'
        assert extract_json(raw, FALLBACK) == {"outer": {"inner": [1, 2]}, "b": 3}

    def test_garbage_returns_fallback(self):
        assert extract_json("not json at all", FALLBACK) == FALLBACK

    def test_empty_and_none(self):
        assert extract_json("", FALLBACK) == FALLBACK
        assert extract_json(None, FALLBACK) == FALLBACK

    def test_non_dict_json_returns_fallback(self):
        """A bare list is valid JSON but not the contract."""
        assert extract_json('[1, 2, 3]', FALLBACK) == FALLBACK

    def test_fallback_is_deep_copied(self):
        first = extract_json("garbage", FALLBACK)
        first["good_habits"].append("polluted")
        second = extract_json("garbage", FALLBACK)
        assert second["good_habits"] == []
        assert FALLBACK["good_habits"] == []

    def test_unicode(self):
        assert extract_json('{"a": "C2 → C3"}', FALLBACK)["a"] == "C2 → C3"


# ──────────────────────────────────────────────
# Image helpers
# ──────────────────────────────────────────────

class TestImageHelpers:

    def test_mime_for(self):
        assert mime_for("chart.png") == "image/png"
        assert mime_for("chart.JPG") == "image/jpeg"
        assert mime_for("chart.jpeg") == "image/jpeg"
        assert mime_for("chart.webp") == "image/webp"
        assert mime_for("chart.unknown") == "image/png"  # sane default

    def test_to_data_uri(self):
        assert to_data_uri("QUJD", "c.png") == "data:image/png;base64,QUJD"
        assert to_data_uri("QUJD", "c.jpg") == "data:image/jpeg;base64,QUJD"

    def test_to_data_uri_is_idempotent(self):
        existing = "data:image/png;base64,QUJD"
        assert to_data_uri(existing) == existing

    def test_strip_data_uri(self):
        assert strip_data_uri("data:image/png;base64,QUJD") == "QUJD"
        assert strip_data_uri("QUJD") == "QUJD"  # already bare

    def test_roundtrip(self):
        assert strip_data_uri(to_data_uri("QUJD", "c.webp")) == "QUJD"

    @pytest.mark.asyncio
    async def test_encode_image_file(self, tmp_path):
        img = tmp_path / "chart.png"
        img.write_bytes(b"\x89PNGfake")
        uri = await encode_image_file(img)
        assert uri.startswith("data:image/png;base64,")
        assert base64.b64decode(strip_data_uri(uri)) == b"\x89PNGfake"

    @pytest.mark.asyncio
    async def test_encode_missing_file_returns_none(self, tmp_path):
        assert await encode_image_file(tmp_path / "nope.png") is None

    @pytest.mark.asyncio
    async def test_encode_oversized_file_is_skipped(self, tmp_path):
        img = tmp_path / "huge.png"
        img.write_bytes(b"x" * 100)
        with patch("services.llm_service.MAX_IMAGE_BYTES", 10):
            assert await encode_image_file(img) is None


# ──────────────────────────────────────────────
# Model selection
# ──────────────────────────────────────────────

class TestModelSelection:

    def test_deepseek_text_model(self):
        svc = LLMService(provider="deepseek")
        with patch("services.llm_service.DEEPSEEK_MODEL", "text-m"):
            assert svc.model_for() == "text-m"

    def test_deepseek_vision_model_when_images(self):
        """Only the vision model accepts images; the others 400."""
        svc = LLMService(provider="deepseek")
        with patch("services.llm_service.DEEPSEEK_VISION_MODEL", "vision-m"):
            assert svc.model_for(images=True) == "vision-m"

    def test_deepseek_reasoning_model_when_deep(self):
        svc = LLMService(provider="deepseek")
        with patch("services.llm_service.DEEPSEEK_REASONING_MODEL", "deep-m"):
            assert svc.model_for(deep=True) == "deep-m"

    def test_vision_wins_over_deep(self):
        """Images are a hard constraint; 'deep' is only a preference."""
        svc = LLMService(provider="deepseek")
        with patch("services.llm_service.DEEPSEEK_VISION_MODEL", "vision-m"), \
             patch("services.llm_service.DEEPSEEK_REASONING_MODEL", "deep-m"):
            assert svc.model_for(images=True, deep=True) == "vision-m"

    def test_ollama_always_uses_its_single_model(self):
        svc = LLMService(provider="ollama")
        with patch("services.llm_service.OLLAMA_MODEL", "local-m"):
            assert svc.model_for(images=True, deep=True) == "local-m"

    def test_provider_is_case_insensitive(self):
        assert LLMService(provider="DeepSeek").provider == "deepseek"


# ──────────────────────────────────────────────
# Configuration guard
# ──────────────────────────────────────────────

class TestConfiguration:

    def test_deepseek_without_key_is_unconfigured(self):
        svc = LLMService(provider="deepseek")
        with patch("services.llm_service.DEEPSEEK_API_KEY", ""):
            ok, reason = svc.is_configured()
        assert not ok
        assert "DEEPSEEK_API_KEY" in reason

    def test_ollama_needs_no_key(self):
        svc = LLMService(provider="ollama")
        with patch("services.llm_service.DEEPSEEK_API_KEY", ""):
            assert svc.is_configured()[0]

    @pytest.mark.asyncio
    async def test_complete_raises_when_unconfigured(self):
        svc = LLMService(provider="deepseek")
        with patch("services.llm_service.DEEPSEEK_API_KEY", ""):
            with pytest.raises(LLMError, match="DEEPSEEK_API_KEY"):
                await svc.complete("hi")


# ──────────────────────────────────────────────
# DeepSeek request shape
# ──────────────────────────────────────────────

class TestDeepSeekRequest:

    @pytest.fixture
    def svc(self):
        return LLMService(provider="deepseek")

    @pytest.mark.asyncio
    async def test_text_request_shape(self, svc):
        session = _mock_session(payload=_deepseek_payload("the answer"))
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch("services.llm_service.DEEPSEEK_BASE_URL", "https://api.deepseek.com"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            result = await svc.complete("what is a CISD?")

        assert result == "the answer"
        url = session.post.call_args[0][0]
        body = session.post.call_args.kwargs["json"]
        headers = session.post.call_args.kwargs["headers"]

        assert url == "https://api.deepseek.com/chat/completions"
        assert headers["Authorization"] == "Bearer sk-test"
        assert body["messages"] == [{"role": "user", "content": "what is a CISD?"}]
        assert body["stream"] is False
        assert "response_format" not in body

    @pytest.mark.asyncio
    async def test_base_url_trailing_slash_is_handled(self, svc):
        session = _mock_session(payload=_deepseek_payload())
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch("services.llm_service.DEEPSEEK_BASE_URL", "https://api.deepseek.com/"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            await svc.complete("hi")
        assert session.post.call_args[0][0] == "https://api.deepseek.com/chat/completions"

    @pytest.mark.asyncio
    async def test_system_prompt_is_prepended(self, svc):
        session = _mock_session(payload=_deepseek_payload())
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            await svc.complete("hi", system="you are a coach")

        messages = session.post.call_args.kwargs["json"]["messages"]
        assert messages[0] == {"role": "system", "content": "you are a coach"}
        assert messages[1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_json_mode_sets_response_format(self, svc):
        session = _mock_session(payload=_deepseek_payload('{"a":1}'))
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            await svc.complete("give me json", json_mode=True)

        assert session.post.call_args.kwargs["json"]["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_images_become_content_blocks(self, svc):
        session = _mock_session(payload=_deepseek_payload())
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch("services.llm_service.DEEPSEEK_VISION_MODEL", "vision-m"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            await svc.complete("read this chart", images=["data:image/png;base64,QUJD"])

        body = session.post.call_args.kwargs["json"]
        assert body["model"] == "vision-m"
        content = body["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "read this chart"}
        assert content[1] == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,QUJD"},
        }

    @pytest.mark.asyncio
    async def test_bare_base64_is_wrapped_as_data_uri(self, svc):
        """Call sites may pass raw base64; DeepSeek needs a data URI."""
        session = _mock_session(payload=_deepseek_payload())
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            await svc.complete("chart", images=["QUJD"])

        content = session.post.call_args.kwargs["json"]["messages"][0]["content"]
        assert content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"

    @pytest.mark.asyncio
    async def test_max_tokens_passed_through(self, svc):
        session = _mock_session(payload=_deepseek_payload())
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            await svc.complete("hi", max_tokens=512)
        assert session.post.call_args.kwargs["json"]["max_tokens"] == 512


# ──────────────────────────────────────────────
# Failure handling
# ──────────────────────────────────────────────

class TestFailureHandling:

    @pytest.fixture
    def svc(self):
        return LLMService(provider="deepseek")

    @pytest.mark.asyncio
    async def test_retries_transient_server_error(self, svc):
        session = _mock_session(status=500, text="boom")
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            with pytest.raises(LLMError):
                await svc.complete("hi", retries=3, retry_delay=0)
        assert session.post.call_count == 3

    @pytest.mark.asyncio
    async def test_auth_error_is_permanent_and_not_retried(self, svc):
        """A bad key fails identically every time — burning retries is waste."""
        session = _mock_session(status=401, text="unauthorized")
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-bad"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            with pytest.raises(LLMError):
                await svc.complete("hi", retries=3, retry_delay=0)
        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_bad_request_is_permanent(self, svc):
        session = _mock_session(status=400, text="bad model")
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            with pytest.raises(LLMError):
                await svc.complete("hi", retries=3, retry_delay=0)
        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_empty_content_is_retried(self, svc):
        """DeepSeek documents occasional empty content in JSON mode."""
        session = _mock_session(payload=_deepseek_payload(""))
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            with pytest.raises(LLMError, match="empty"):
                await svc.complete("hi", retries=2, retry_delay=0)
        assert session.post.call_count == 2

    @pytest.mark.asyncio
    async def test_unexpected_response_shape(self, svc):
        session = _mock_session(payload={"nope": True})
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            with pytest.raises(LLMError):
                await svc.complete("hi", retries=1, retry_delay=0)

    @pytest.mark.asyncio
    async def test_succeeds_on_second_attempt(self, svc):
        ok = AsyncMock()
        ok.status = 200
        ok.json = AsyncMock(return_value=_deepseek_payload("recovered"))
        bad = AsyncMock()
        bad.status = 503
        bad.text = AsyncMock(return_value="unavailable")

        session = MagicMock()
        session.closed = False
        session.post.return_value.__aenter__ = AsyncMock(side_effect=[bad, ok])
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            assert await svc.complete("hi", retries=3, retry_delay=0) == "recovered"
        assert session.post.call_count == 2

    @pytest.mark.asyncio
    async def test_complete_json_returns_fallback_on_failure(self, svc):
        session = _mock_session(status=500, text="boom")
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            result = await svc.complete_json("hi", FALLBACK, retries=1, retry_delay=0)
        assert result == FALLBACK

    @pytest.mark.asyncio
    async def test_complete_json_parses_success(self, svc):
        session = _mock_session(payload=_deepseek_payload('{"good_habits": ["x"], "mistakes": []}'))
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            result = await svc.complete_json("hi", FALLBACK)
        assert result["good_habits"] == ["x"]


# ──────────────────────────────────────────────
# Ollama fallback
# ──────────────────────────────────────────────

class TestOllamaProvider:

    @pytest.fixture
    def svc(self):
        return LLMService(provider="ollama")

    @pytest.mark.asyncio
    async def test_uses_native_generate_shape(self, svc):
        session = _mock_session(payload={"response": "local answer"})
        with patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            result = await svc.complete("hi", json_mode=True)

        assert result == "local answer"
        body = session.post.call_args.kwargs["json"]
        assert body["prompt"] == "hi"
        assert body["format"] == "json"       # Ollama's flag, not response_format
        assert "messages" not in body

    @pytest.mark.asyncio
    async def test_data_uris_are_stripped_to_raw_base64(self, svc):
        """Ollama wants bare base64, unlike DeepSeek."""
        session = _mock_session(payload={"response": "ok"})
        with patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            await svc.complete("chart", images=["data:image/png;base64,QUJD"])
        assert session.post.call_args.kwargs["json"]["images"] == ["QUJD"]

    @pytest.mark.asyncio
    async def test_error_status_raises(self, svc):
        session = _mock_session(status=500, text="down")
        with patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            with pytest.raises(LLMError):
                await svc.complete("hi", retries=1, retry_delay=0)


# ──────────────────────────────────────────────
# Reasoning effort
# ──────────────────────────────────────────────

from services.llm_service import thinking_payload, VALID_EFFORTS


class TestThinkingPayload:

    def test_off_disables_thinking(self):
        assert thinking_payload("off") == {"type": "disabled"}

    @pytest.mark.parametrize("alias", ["off", "none", "disabled", "false", "OFF", " Off "])
    def test_disable_aliases(self, alias):
        assert thinking_payload(alias) == {"type": "disabled"}

    @pytest.mark.parametrize("effort", ["low", "high", "max"])
    def test_enabled_levels(self, effort):
        assert thinking_payload(effort) == {"type": "enabled", "reasoning_effort": effort}

    def test_case_insensitive(self):
        assert thinking_payload("MAX") == {"type": "enabled", "reasoning_effort": "max"}

    def test_none_omits_the_parameter(self):
        """None means 'let the API apply its own default', not 'disable'."""
        assert thinking_payload(None) is None
        assert thinking_payload("") is None

    def test_unknown_value_omits_rather_than_guessing(self):
        assert thinking_payload("turbo") is None

    def test_valid_efforts_are_all_handled(self):
        for effort in VALID_EFFORTS:
            assert thinking_payload(effort) is not None


class TestEffortOnTheWire:

    @pytest.fixture
    def svc(self):
        return LLMService(provider="deepseek")

    async def _send(self, svc, **kwargs):
        session = _mock_session(payload=_deepseek_payload())
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            await svc.complete("hi", **kwargs)
        return session.post.call_args.kwargs["json"]

    @pytest.mark.asyncio
    async def test_omitted_by_default(self, svc):
        """A plain call shouldn't force a thinking mode on the API."""
        assert "thinking" not in await self._send(svc)

    @pytest.mark.asyncio
    async def test_explicit_off(self, svc):
        body = await self._send(svc, reasoning_effort="off")
        assert body["thinking"] == {"type": "disabled"}

    @pytest.mark.asyncio
    async def test_explicit_max(self, svc):
        body = await self._send(svc, reasoning_effort="max")
        assert body["thinking"] == {"type": "enabled", "reasoning_effort": "max"}

    @pytest.mark.asyncio
    async def test_deep_uses_configured_deep_effort(self, svc):
        with patch("services.llm_service.DEEPSEEK_DEEP_EFFORT", "max"):
            body = await self._send(svc, deep=True)
        assert body["thinking"] == {"type": "enabled", "reasoning_effort": "max"}

    @pytest.mark.asyncio
    async def test_explicit_effort_overrides_deep(self, svc):
        with patch("services.llm_service.DEEPSEEK_DEEP_EFFORT", "max"):
            body = await self._send(svc, deep=True, reasoning_effort="off")
        assert body["thinking"] == {"type": "disabled"}

    @pytest.mark.asyncio
    async def test_ollama_ignores_effort(self):
        """Effort is a DeepSeek concept; Ollama must not receive it."""
        svc = LLMService(provider="ollama")
        session = _mock_session(payload={"response": "ok"})
        with patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            await svc.complete("hi", reasoning_effort="max")
        body = session.post.call_args.kwargs["json"]
        assert "thinking" not in body
        assert "reasoning_effort" not in body


class TestThinkingRejectionFallback:
    """
    Not every model is guaranteed to accept thinking controls. Losing a chart
    analysis over an optional knob would be a silly failure, so a 400 that
    names the parameter is retried once without it.
    """

    @pytest.fixture
    def svc(self):
        return LLMService(provider="deepseek")

    def _two_responses(self, first, second):
        session = MagicMock()
        session.closed = False
        session.post.return_value.__aenter__ = AsyncMock(side_effect=[first, second])
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        return session

    @pytest.mark.asyncio
    async def test_retries_without_thinking_and_succeeds(self, svc):
        reject = AsyncMock()
        reject.status = 400
        reject.text = AsyncMock(return_value="Unsupported parameter: thinking")
        ok = AsyncMock()
        ok.status = 200
        ok.json = AsyncMock(return_value=_deepseek_payload("recovered"))

        session = self._two_responses(reject, ok)
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            result = await svc.complete("hi", reasoning_effort="max", retries=1)

        assert result == "recovered"
        assert session.post.call_count == 2
        # The retry must have dropped the offending parameter.
        assert "thinking" not in session.post.call_args_list[1].kwargs["json"]
        # ...while keeping everything else intact.
        assert session.post.call_args_list[1].kwargs["json"]["messages"]

    @pytest.mark.asyncio
    async def test_unrelated_400_is_not_retried(self, svc):
        """A genuine bad request must stay permanent, not burn a second call."""
        session = _mock_session(status=400, text="Invalid model name")
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            with pytest.raises(LLMError, match="Invalid model name"):
                await svc.complete("hi", reasoning_effort="max", retries=3, retry_delay=0)
        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_no_fallback_when_thinking_was_never_sent(self, svc):
        session = _mock_session(status=400, text="thinking is unsupported")
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            with pytest.raises(LLMError):
                await svc.complete("hi", retries=1)
        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_that_also_fails_raises(self, svc):
        reject = AsyncMock()
        reject.status = 400
        reject.text = AsyncMock(return_value="Unsupported parameter: thinking")
        still_bad = AsyncMock()
        still_bad.status = 401
        still_bad.text = AsyncMock(return_value="bad key")

        session = self._two_responses(reject, still_bad)
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            with pytest.raises(LLMError, match="401"):
                await svc.complete("hi", reasoning_effort="max", retries=1)


# ──────────────────────────────────────────────
# TLS trust
# ──────────────────────────────────────────────

import ssl as _ssl
from services.llm_service import build_ssl_context


class TestSSLContext:
    """
    python.org macOS builds ship with no usable CA store until
    `Install Certificates.command` is run, which turns every HTTPS call into a
    baffling CERTIFICATE_VERIFY_FAILED. The system store is still preferred so
    corporate CAs keep working; certifi is only a fallback.
    """

    def test_returns_a_usable_context(self):
        ctx = build_ssl_context()
        assert isinstance(ctx, _ssl.SSLContext)
        assert ctx.get_ca_certs(), "context should carry CA certs from some source"

    def test_prefers_system_store_when_populated(self):
        """A populated system store must win, so custom/corporate CAs survive."""
        sentinel = _ssl.create_default_context()
        sentinel.get_ca_certs = lambda: [{"subject": "system-ca"}]
        with patch("ssl.create_default_context", return_value=sentinel):
            assert build_ssl_context() is sentinel

    def test_falls_back_to_certifi_when_system_store_empty(self):
        empty = _ssl.create_default_context()
        empty.get_ca_certs = lambda: []
        calls = []

        def fake_default(*args, **kwargs):
            calls.append(kwargs)
            return empty

        with patch("ssl.create_default_context", side_effect=fake_default):
            build_ssl_context()

        # First call bare, second with certifi's bundle.
        assert calls[0] == {}
        assert "cafile" in calls[1]
        assert calls[1]["cafile"].endswith("cacert.pem")

    def test_survives_a_broken_ssl_module(self):
        """Never crash the bot over TLS setup; degrade to aiohttp's default."""
        with patch("ssl.create_default_context", side_effect=OSError("no store")):
            assert build_ssl_context() is None

    @pytest.mark.asyncio
    async def test_session_uses_the_context(self):
        svc = LLMService(provider="deepseek")
        sentinel = _ssl.create_default_context()
        with patch("services.llm_service.build_ssl_context", return_value=sentinel):
            session = await svc._get_session()
        try:
            assert session.connector is not None
        finally:
            await svc.close()


class TestTokenBudgetWithThinking:
    """
    Reasoning tokens are billed as output tokens and eat max_tokens before any
    content is produced. A tight budget plus thinking yields empty content,
    which reads as a mysterious API fault rather than truncation.
    """

    @pytest.fixture
    def svc(self):
        return LLMService(provider="deepseek")

    async def _call(self, svc, **kwargs):
        session = _mock_session(payload=_deepseek_payload())
        with patch("services.llm_service.DEEPSEEK_API_KEY", "sk-test"), \
             patch.object(svc, "_get_session", AsyncMock(return_value=session)):
            await svc.complete("hi", **kwargs)
        return session.post.call_args.kwargs["json"]

    @pytest.mark.asyncio
    async def test_warns_on_small_budget_with_thinking(self, svc, capsys):
        await self._call(svc, reasoning_effort="max", max_tokens=10)
        out = capsys.readouterr().out
        assert "max_tokens=10" in out
        assert "may come back empty" in out

    @pytest.mark.asyncio
    async def test_no_warning_when_thinking_disabled(self, svc, capsys):
        await self._call(svc, reasoning_effort="off", max_tokens=10)
        assert "may come back empty" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_no_warning_with_generous_budget(self, svc, capsys):
        await self._call(svc, reasoning_effort="max", max_tokens=16000)
        assert "may come back empty" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_no_warning_without_max_tokens(self, svc, capsys):
        await self._call(svc, reasoning_effort="max")
        assert "may come back empty" not in capsys.readouterr().out


class TestPreflightProbeIsSafe:
    """The reachability probe must not be able to trip the empty-content trap."""

    def test_probe_disables_thinking_and_has_headroom(self):
        import re
        from pathlib import Path
        src = Path(__file__).resolve().parent.parent / "scripts" / "preflight.py"
        text = src.read_text()
        probe = re.search(r'reply = await llm_service\.complete\((.*?)\)', text, re.DOTALL).group(1)
        assert 'reasoning_effort="off"' in probe
        budget = int(re.search(r"max_tokens=(\d+)", probe).group(1))
        assert budget >= 32


class TestDeepSynthesisBudget:
    """deep=True runs at max effort, so its budget must cover reasoning too."""

    def test_psych_budget_exceeds_thinking_floor(self):
        import re
        from pathlib import Path
        from services.llm_service import MIN_TOKENS_WITH_THINKING
        src = Path(__file__).resolve().parent.parent / "cogs" / "psych.py"
        call = re.search(r"complete_json\(prompt, fallback, deep=True, max_tokens=(\d+)\)",
                         src.read_text())
        assert call, "deep synthesis call not found"
        assert int(call.group(1)) > MIN_TOKENS_WITH_THINKING * 4
