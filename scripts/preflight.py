"""
Pre-flight environment check.

Run this BEFORE `python bot.py`. It verifies every external dependency and
tells you exactly what's missing, rather than letting the bot fail cryptically
at runtime. Nothing here posts to Discord or spends more than a fraction of a
cent.

    python scripts/preflight.py

Exit code 0 = ready to run. 1 = something required is broken.
"""
import os
import sys
import json
import asyncio
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

OK, WARN, FAIL = "✅", "⚠️ ", "❌"
_results = []


def record(level, name, detail=""):
    _results.append((level, name, detail))
    print(f"  {level} {name}" + (f"\n       {detail}" if detail else ""))


def check_env_file():
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        record(FAIL, ".env file", "Not found. Copy .env.example to .env and fill it in.")
        return False
    record(OK, ".env file", str(env))
    return True


def _application_id_from_token(token: str):
    """
    Decode the application id from a bot token's first segment.

    A bot token is `base64(user_id).timestamp.hmac`. Only the first segment is
    decoded here — it is the bot's public user/application id, the same number
    that appears in its invite URL. The secret parts are never touched.
    """
    import base64
    try:
        head = token.split(".")[0]
        head += "=" * (-len(head) % 4)  # restore stripped base64 padding
        return int(base64.b64decode(head).decode())
    except Exception:
        return None


def check_discord_token():
    from config import BOT_TOKEN, DEV_GUILD_ID
    if not BOT_TOKEN:
        record(FAIL, "Discord token", "BOT_KEY is not set in .env")
        return
    # Discord tokens are three dot-separated segments. Don't print any of it.
    parts = BOT_TOKEN.split(".")
    if len(parts) != 3 or len(BOT_TOKEN) < 50:
        record(WARN, "Discord token",
               "Present but doesn't look like a bot token (expected 3 dot-separated parts). "
               "Make sure you copied the BOT token, not the Client Secret or Application ID.")
    else:
        app_id = _application_id_from_token(BOT_TOKEN)
        if app_id:
            record(OK, "Discord token",
                   f"present ({len(BOT_TOKEN)} chars) — belongs to application {app_id}\n"
                   f"       Confirm this is your DEV app, not production. If your Pi runs the\n"
                   f"       same application id, both will fight over the gateway and neither\n"
                   f"       will stay connected.")
        else:
            record(OK, "Discord token", f"present ({len(BOT_TOKEN)} chars, format looks right)")

    if DEV_GUILD_ID:
        record(OK, "DEV_GUILD_ID", f"{DEV_GUILD_ID} — slash commands will sync instantly")
    else:
        record(WARN, "DEV_GUILD_ID",
               "Not set. Slash commands will sync GLOBALLY and can take up to an hour "
               "to appear. Set DEV_GUILD_ID to your test server id for instant sync.")


# Application flag bits that indicate a privileged intent is approved.
# "LIMITED" means approved but capped to <100 guilds, which is fine for us.
GATEWAY_MESSAGE_CONTENT = 1 << 18
GATEWAY_MESSAGE_CONTENT_LIMITED = 1 << 19
GATEWAY_GUILD_MEMBERS = 1 << 14
GATEWAY_GUILD_MEMBERS_LIMITED = 1 << 15


async def check_discord_api():
    """
    Ask Discord directly which intents this application has, and which guilds
    it is actually in.

    Read-only. This answers two questions that otherwise take a crash and a lot
    of guessing: is MESSAGE CONTENT enabled, and did I invite the bot to the
    server I think I did? A new application has privileged intents OFF by
    default, and bot.py requests message_content — so a fresh app that was
    never toggled cannot hold a gateway connection.
    """
    import aiohttp
    from config import BOT_TOKEN, DEV_GUILD_ID
    if not BOT_TOKEN:
        return

    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    base = "https://discord.com/api/v10"

    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(f"{base}/applications/@me", timeout=10) as r:
                if r.status == 401:
                    record(FAIL, "Discord API", "Token rejected (401). Reset it in the portal and update .env.")
                    return
                if r.status != 200:
                    record(WARN, "Discord API", f"HTTP {r.status} querying application info.")
                    return
                app = await r.json()

            async with s.get(f"{base}/users/@me/guilds", timeout=10) as r:
                guilds = await r.json() if r.status == 200 else []
    except Exception as e:
        record(WARN, "Discord API", f"Couldn't reach Discord: {e}")
        return

    flags = app.get("flags", 0) or 0
    name = app.get("name", "?")

    # --- privileged intents ---
    msg_content = bool(flags & (GATEWAY_MESSAGE_CONTENT | GATEWAY_MESSAGE_CONTENT_LIMITED))
    if msg_content:
        record(OK, "MESSAGE CONTENT intent", f"enabled on '{name}'")
    else:
        record(FAIL, "MESSAGE CONTENT intent",
               f"NOT enabled on application '{name}' ({app.get('id')}).\n"
               f"       bot.py requests this intent, so the gateway will refuse the\n"
               f"       connection and you'll see 'session has been invalidated' on a loop.\n"
               f"       Fix: Developer Portal -> your app -> Bot -> Privileged Gateway\n"
               f"       Intents -> turn ON 'MESSAGE CONTENT INTENT' -> Save Changes.\n"
               f"       (New applications have this OFF by default.)")

    # --- guild membership ---
    if not guilds:
        record(FAIL, "Server membership", "The bot is not in ANY server. Re-invite it.")
        return

    listed = ", ".join(f"{g.get('name')} ({g.get('id')})" for g in guilds)
    if DEV_GUILD_ID and not any(str(g.get("id")) == str(DEV_GUILD_ID) for g in guilds):
        record(FAIL, "Server membership",
               f"Bot is NOT in DEV_GUILD_ID={DEV_GUILD_ID}.\n"
               f"       It is in: {listed}\n"
               f"       Either fix DEV_GUILD_ID in .env or invite this app to that server.")
    else:
        record(OK, "Server membership", f"in {len(guilds)} server(s): {listed}")


def check_python_deps():
    missing = []
    for mod, why in [("discord", "Discord client"), ("aiohttp", "HTTP"),
                     ("aiofiles", "async file IO"), ("dotenv", "env loading"),
                     ("chromadb", "RAG vector store — bot won't start without it")]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(f"{mod} ({why})")
    if missing:
        record(FAIL, "Python packages", "Missing: " + ", ".join(missing)
               + "\n       Run: pip install -r requirements.txt")
    else:
        record(OK, "Python packages", "all importable")


def check_corpus():
    try:
        from services.coach_service import load_corpus
        corpus = load_corpus()
        n = len(corpus["principles"])
        record(OK, "Shull principles corpus", f"v{corpus.get('version')}, {n} principles")
    except Exception as e:
        record(FAIL, "Shull principles corpus", str(e))


def _diagnose_api_failure(error: str) -> str:
    """Map a raw connection error to the thing that actually needs fixing."""
    lowered = error.lower()

    if "certificate" in lowered or "ssl" in lowered:
        return ("This is a TLS trust problem, NOT a bad key.\n"
                "       Your Python has no usable CA bundle. On macOS, fix it with:\n"
                "         /Applications/Python\\ 3.12/Install\\ Certificates.command\n"
                "       (adjust the version to match your Python), then re-run this check.")
    if "401" in lowered or "403" in lowered or "unauthor" in lowered:
        return ("The key was rejected. Check it at platform.deepseek.com — make sure you "
                "copied the whole thing and it hasn't been revoked.")
    if "empty content" in lowered:
        return ("The API answered but returned nothing. Usually max_tokens was consumed by\n"
                "       reasoning before any content was produced — thinking tokens bill as\n"
                "       output tokens. Raise max_tokens or lower the reasoning effort.")
    if "402" in lowered or "insufficient" in lowered or "balance" in lowered:
        return "The key is valid but the account is out of credit. Top up at platform.deepseek.com."
    if "404" in lowered or "model" in lowered:
        return ("The endpoint or model name was rejected. Check DEEPSEEK_MODEL in .env "
                "against the current model list in the DeepSeek docs.")
    if "cannot connect" in lowered or "timeout" in lowered or "dns" in lowered:
        return "Couldn't reach api.deepseek.com at all — check your network or any VPN/proxy."
    return "Check the key at platform.deepseek.com and that you have credit."


async def check_deepseek():
    from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL
    from services.llm_service import llm_service, LLMError

    ok, reason = llm_service.is_configured()
    if not ok:
        record(FAIL, "DeepSeek", reason)
        return

    record(OK, "DeepSeek key", f"present ({len(DEEPSEEK_API_KEY)} chars)")

    from config import (DEEPSEEK_VISION_MODEL, DEEPSEEK_REASONING_MODEL,
                        DEEPSEEK_JOURNAL_EFFORT, DEEPSEEK_COACH_EFFORT, DEEPSEEK_DEEP_EFFORT)
    record(OK, "Model routing",
           f"text={DEEPSEEK_MODEL}  charts={DEEPSEEK_VISION_MODEL}  deep={DEEPSEEK_REASONING_MODEL}\n"
           f"       effort: journal={DEEPSEEK_JOURNAL_EFFORT}  coach={DEEPSEEK_COACH_EFFORT}  deep={DEEPSEEK_DEEP_EFFORT}")
    try:
        # Smallest possible real call: confirms key, network and model name.
        # Thinking tokens bill as output tokens, so a tight max_tokens with
        # reasoning on returns empty content. A reachability probe needs neither.
        reply = await llm_service.complete(
            "Reply with the single word: ready",
            reasoning_effort="off", retries=1, max_tokens=64, timeout=30)
        record(OK, "DeepSeek API", f"`{DEEPSEEK_MODEL}` responded: {reply.strip()[:40]!r}")
    except LLMError as e:
        record(FAIL, "DeepSeek API", f"{e}\n       {_diagnose_api_failure(str(e))}")
    finally:
        await llm_service.close()


async def check_ollama():
    """Only needed for /ask and /reindex — DeepSeek has no embeddings endpoint."""
    import aiohttp
    from config import OLLAMA_API_URL, EMBED_MODEL
    base = OLLAMA_API_URL.replace("/api/generate", "")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/api/tags", timeout=3) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                models = [m["name"] for m in (await r.json()).get("models", [])]
    except Exception as e:
        record(WARN, "Ollama (embeddings only)",
               f"Not reachable at {base} ({e}).\n"
               f"       Everything works EXCEPT /ask and /reindex. "
               f"To enable: ollama serve && ollama pull {EMBED_MODEL}")
        return

    if any(m.startswith(EMBED_MODEL) for m in models):
        record(OK, "Ollama (embeddings)", f"{EMBED_MODEL} available")
    else:
        record(WARN, "Ollama (embeddings)",
               f"Running, but {EMBED_MODEL} is not pulled. Run: ollama pull {EMBED_MODEL}")


async def check_couchdb():
    """Only needed for /bias persistence and TradingView alert storage."""
    import aiohttp
    from config import COUCHDB_URL
    host = COUCHDB_URL.split("@")[-1]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(COUCHDB_URL, timeout=3) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                version = (await r.json()).get("version", "?")
        record(OK, "CouchDB", f"v{version} at {host}")
    except Exception as e:
        record(WARN, "CouchDB",
               f"Not reachable at {host} ({e}).\n"
               f"       Everything works EXCEPT /bias persistence and alert storage. "
               f"The bot starts fine without it.")


async def main():
    print("\n" + "=" * 62)
    print("  PRE-FLIGHT CHECK")
    print("=" * 62 + "\n")

    print("REQUIRED")
    has_env = check_env_file()
    check_python_deps()
    if has_env:
        check_discord_token()
    check_corpus()
    if has_env:
        await check_deepseek()

    if has_env:
        await check_discord_api()

    print("\nOPTIONAL (bot runs without these)")
    await check_ollama()
    await check_couchdb()

    fails = [r for r in _results if r[0] == FAIL]
    warns = [r for r in _results if r[0] == WARN]

    print("\n" + "=" * 62)
    if fails:
        print(f"  ❌ NOT READY — {len(fails)} blocking issue(s):")
        for _, name, _d in fails:
            print(f"       · {name}")
        print("=" * 62 + "\n")
        return 1

    print(f"  ✅ READY TO RUN" + (f"  ({len(warns)} optional feature(s) unavailable)" if warns else ""))
    print("\n     Start the bot with:  python bot.py")
    print("=" * 62 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
