"""
Provider-agnostic LLM client.

DeepSeek is the default brain and speaks the OpenAI-compatible
`/chat/completions` shape. Ollama is kept behind the same interface as an
offline fallback, and remains the *only* embeddings provider (see
services/rag_service.py) because DeepSeek exposes no embeddings endpoint.

Call sites should never build provider payloads themselves — they call
`llm_service.complete(...)` and let this module translate.
"""
import ssl
import base64
import asyncio
import aiohttp
import aiofiles
from pathlib import Path
from services.json_utils import extract_json
from config import (
    LLM_PROVIDER,
    LLM_TIMEOUT,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_VISION_MODEL,
    DEEPSEEK_REASONING_MODEL,
    DEEPSEEK_DEEP_EFFORT,
    OLLAMA_API_URL,
    OLLAMA_MODEL,
)

# Extension → MIME. DeepSeek sniffs the real format from content, but a sane
# declared type keeps the data URI well-formed.
_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_DEFAULT_MIME = "image/png"

# DeepSeek caps a single inline image at 32 MiB; stay well under it since a
# chart screenshot has no business being large.
MAX_IMAGE_BYTES = 20 * 1024 * 1024


# DeepSeek's thinking controls. "medium"/"xhigh" are accepted by the API but
# mapped to "high" server-side, so they're not offered here.
VALID_EFFORTS = ("off", "low", "high", "max")

# Reasoning tokens are billed and counted as OUTPUT tokens, so they consume
# max_tokens before any content is generated. Below roughly this budget a
# thinking model routinely exhausts the allowance and returns empty content —
# which surfaces as a baffling "returned empty content" rather than a clear
# truncation error. Warn instead of silently failing.
MIN_TOKENS_WITH_THINKING = 512


def thinking_payload(effort: str | None) -> dict | None:
    """
    Translate an effort setting into DeepSeek's `thinking` object.

    Returns None when the parameter should be omitted entirely, which lets the
    API apply its own default (currently "high"). Thinking tokens bill as
    output tokens, so "off" is a real saving on tasks that don't reason.
    """
    if not effort:
        return None
    effort = str(effort).strip().lower()
    if effort in ("off", "none", "disabled", "false"):
        return {"type": "disabled"}
    if effort in ("low", "high", "max"):
        return {"type": "enabled", "reasoning_effort": effort}
    print(f"[LLM] Unknown reasoning effort {effort!r}; omitting the parameter. "
          f"Valid values: {', '.join(VALID_EFFORTS)}")
    return None


def build_ssl_context() -> ssl.SSLContext | None:
    """
    Build an SSL context that verifies properly on a stock machine.

    Prefers the system trust store, so corporate/self-signed CAs installed
    there keep working. Falls back to certifi's bundle only when the system
    store is empty — which is the normal state for python.org builds on macOS
    until `Install Certificates.command` has been run, and produces an
    otherwise baffling CERTIFICATE_VERIFY_FAILED on every HTTPS call.

    Returns None to let aiohttp use its own default if neither source works.
    """
    try:
        context = ssl.create_default_context()
        if context.get_ca_certs():
            return context
    except Exception:
        context = None

    try:
        import certifi
        print("[LLM] System CA store is empty; falling back to certifi. "
              "On macOS, run '/Applications/Python 3.x/Install Certificates.command' "
              "to fix this properly.")
        return ssl.create_default_context(cafile=certifi.where())
    except Exception as e:
        print(f"[LLM] Could not load certifi ({e}); using aiohttp's default TLS handling.")
        return context


class LLMError(RuntimeError):
    """Raised when a provider call fails after exhausting retries."""


def mime_for(file_name) -> str:
    """Best-effort MIME type for an image filename."""
    return _MIME_BY_SUFFIX.get(Path(file_name).suffix.lower(), _DEFAULT_MIME)


def to_data_uri(raw_b64: str, file_name: str = "image.png") -> str:
    """Wrap raw base64 in a data URI. Already-wrapped input is passed through."""
    if raw_b64.startswith("data:"):
        return raw_b64
    return f"data:{mime_for(file_name)};base64,{raw_b64}"


def strip_data_uri(image: str) -> str:
    """Reduce a data URI back to raw base64 (what Ollama's API expects)."""
    if image.startswith("data:") and "," in image:
        return image.split(",", 1)[1]
    return image


async def encode_image_file(path) -> str | None:
    """
    Read an image from disk and return it as a data URI, or None if the file is
    missing or implausibly large. Oversized charts are skipped rather than
    failing the whole analysis.
    """
    path = Path(path)
    if not path.exists():
        return None
    if path.stat().st_size > MAX_IMAGE_BYTES:
        print(f"[WARN] Skipping oversized image {path.name} ({path.stat().st_size} bytes)")
        return None

    async with aiofiles.open(path, "rb") as f:
        raw = base64.b64encode(await f.read()).decode("utf-8")
    return to_data_uri(raw, path.name)


class LLMService:
    """A thin async client that hides provider differences behind `complete()`."""

    def __init__(self, provider: str | None = None):
        self._provider = (provider or LLM_PROVIDER).lower()
        self._session: aiohttp.ClientSession | None = None

    # --- session lifecycle (mirrors CouchDBService) ---

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Built per-session (inside the loop) so the connector binds correctly.
            context = build_ssl_context()
            connector = aiohttp.TCPConnector(ssl=context) if context else None
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # --- introspection ---

    @property
    def provider(self) -> str:
        return self._provider

    def is_configured(self) -> tuple[bool, str]:
        """Whether the active provider can actually be called, plus why not."""
        if self._provider == "deepseek" and not DEEPSEEK_API_KEY:
            return False, "DEEPSEEK_API_KEY is not set. Add it to your .env file."
        return True, ""

    def model_for(self, *, images: bool = False, deep: bool = False) -> str:
        """Pick the right model for the job."""
        if self._provider == "ollama":
            return OLLAMA_MODEL
        if images:
            # Only the vision-exp model accepts images; the others return 400.
            return DEEPSEEK_VISION_MODEL
        if deep:
            return DEEPSEEK_REASONING_MODEL
        return DEEPSEEK_MODEL

    # --- the one entry point ---

    async def complete(
        self,
        prompt: str,
        *,
        images: list[str] | None = None,
        system: str | None = None,
        json_mode: bool = False,
        deep: bool = False,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        retries: int = 3,
        retry_delay: float = 5.0,
        timeout: int | None = None,
    ) -> str:
        """
        Run a single-turn completion and return the raw text response.

        images: data URIs (see encode_image_file) or bare base64 strings.
        json_mode: ask the provider for a JSON object back.
        reasoning_effort: "off" | "low" | "high" | "max". Omitted when None,
            letting the provider default apply. `deep=True` implies the
            configured deep effort unless this overrides it.
        deep: prefer the stronger/slower model where the provider has one.

        Raises LLMError if every attempt fails.
        """
        ok, reason = self.is_configured()
        if not ok:
            raise LLMError(reason)

        images = images or []
        last_error = None

        for attempt in range(1, retries + 1):
            try:
                return await self._call(
                    prompt,
                    images=images,
                    system=system,
                    json_mode=json_mode,
                    deep=deep,
                    reasoning_effort=reasoning_effort,
                    max_tokens=max_tokens,
                    timeout=timeout or LLM_TIMEOUT,
                )
            except asyncio.TimeoutError:
                last_error = "Timeout"
                print(f"[LLM] attempt {attempt}/{retries} timed out.")
            except LLMError as e:
                last_error = str(e)
                print(f"[LLM] attempt {attempt}/{retries} failed — {e}")
                # A bad key or malformed request will fail identically on retry.
                if getattr(e, "permanent", False):
                    break
            except Exception as e:
                last_error = str(e)
                print(f"[LLM] attempt {attempt}/{retries} error: {e}")

            if attempt < retries:
                await asyncio.sleep(retry_delay)

        raise LLMError(f"All {retries} attempt(s) failed. Last error: {last_error}")

    async def _call(self, prompt, *, images, system, json_mode, deep, reasoning_effort,
                    max_tokens, timeout) -> str:
        if self._provider == "ollama":
            # Ollama has no equivalent knob; effort is a DeepSeek concept.
            return await self._call_ollama(prompt, images, system, json_mode, timeout)
        return await self._call_deepseek(prompt, images, system, json_mode, deep,
                                         reasoning_effort, max_tokens, timeout)

    # --- DeepSeek (OpenAI-compatible chat completions) ---

    async def _call_deepseek(self, prompt, images, system, json_mode, deep,
                             reasoning_effort, max_tokens, timeout) -> str:
        # With images, the user content becomes an array of typed blocks.
        if images:
            content = [{"type": "text", "text": prompt}]
            for img in images:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": to_data_uri(img)},
                })
        else:
            content = prompt

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        payload = {
            "model": self.model_for(images=bool(images), deep=deep),
            "messages": messages,
            "stream": False,
        }

        # An explicit effort always wins; otherwise a deep call takes the
        # configured deep effort and everything else leaves it to the API.
        effort = reasoning_effort if reasoning_effort is not None else (
            DEEPSEEK_DEEP_EFFORT if deep else None)
        thinking = thinking_payload(effort)
        if thinking:
            payload["thinking"] = thinking

        if json_mode:
            # DeepSeek requires the word "json" to appear in the prompt as well;
            # callers embed a literal JSON example, which satisfies that.
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
            if thinking and thinking.get("type") == "enabled" and max_tokens < MIN_TOKENS_WITH_THINKING:
                print(f"[LLM] max_tokens={max_tokens} with reasoning enabled "
                      f"({thinking.get('reasoning_effort')}). Thinking tokens count toward "
                      f"this budget, so the response may come back empty. Raise max_tokens "
                      f"above {MIN_TOKENS_WITH_THINKING} or pass reasoning_effort='off'.")

        session = await self._get_session()
        url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        }

        data = await self._post_deepseek(session, url, headers, payload, timeout)

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"Unexpected DeepSeek response shape: {str(data)[:300]}")

        # DeepSeek documents occasional empty content in JSON mode — treat it as
        # a retryable failure rather than silently returning nothing.
        if text is None or not text.strip():
            raise LLMError("DeepSeek returned empty content.")

        return text

    async def _post_deepseek(self, session, url, headers, payload, timeout) -> dict:
        """
        POST once, and if the request is rejected specifically because of the
        `thinking` parameter, drop it and retry once.

        Not every model necessarily accepts thinking controls, and a hard
        failure on an optional knob would be a silly way to lose a chart
        analysis. A 400 for any other reason still propagates as permanent.
        """
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                return await resp.json()
            body = (await resp.text())[:300]

        rejected_thinking = (
            resp.status == 400
            and "thinking" in payload
            and any(k in body.lower() for k in ("thinking", "reasoning_effort"))
        )
        if rejected_thinking:
            print(f"[LLM] `{payload['model']}` rejected the thinking parameter; "
                  f"retrying without it. ({body[:120]})")
            retry_payload = {k: v for k, v in payload.items() if k != "thinking"}
            async with session.post(url, json=retry_payload, headers=headers,
                                    timeout=timeout) as resp2:
                if resp2.status == 200:
                    return await resp2.json()
                body = (await resp2.text())[:300]
                resp = resp2

        err = LLMError(f"DeepSeek HTTP {resp.status}: {body}")
        # 401/403 = bad key, 400 = malformed request. Retrying won't help.
        err.permanent = resp.status in (400, 401, 403, 404)
        raise err

    # --- Ollama (native generate API) ---

    async def _call_ollama(self, prompt, images, system, json_mode, timeout) -> str:
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            # Ollama wants bare base64, not data URIs.
            "images": [strip_data_uri(img) for img in images],
            "stream": False,
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"

        session = await self._get_session()
        async with session.post(OLLAMA_API_URL, json=payload, timeout=timeout) as resp:
            if resp.status != 200:
                body = (await resp.text())[:300]
                raise LLMError(f"Ollama HTTP {resp.status}: {body}")
            data = await resp.json()

        text = data.get("response")
        if not text or not text.strip():
            raise LLMError("Ollama returned empty content.")
        return text

    async def complete_json(self, prompt: str, fallback: dict, **kwargs) -> dict:
        """
        Convenience wrapper: run a JSON-mode completion and parse it, returning
        `fallback` if the call or the parse fails. Callers that need to know
        about failure should use complete() directly.
        """
        try:
            raw = await self.complete(prompt, json_mode=True, **kwargs)
        except LLMError as e:
            print(f"[LLM] complete_json failed: {e}")
            return dict(fallback)

        return extract_json(raw, fallback)


# Singleton instance, mirroring couchdb_service / rag_service.
llm_service = LLMService()
