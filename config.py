import os
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
# --- Discord Config ---
BOT_TOKEN = os.getenv('BOT_KEY')
# Set to your test server's ID to make slash commands appear INSTANTLY.
# Global sync (the default when this is unset) can take up to an hour to
# propagate, which makes local iteration painful.
def _optional_int(name: str):
    """Read an int env var, tolerating unset, empty and malformed values.

    Copying .env.example leaves keys present but blank, so an empty string is
    the normal case here, not an error worth crashing the bot over."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw) or None
    except ValueError:
        print(f"⚠️  {name}={raw!r} is not a number — ignoring it.")
        return None


DEV_GUILD_ID = _optional_int("DEV_GUILD_ID")
REMINDER_CHANNEL_ID = 1501833920449351720
REMINDER_TIME = datetime.time(hour=9, minute=30, tzinfo=ZoneInfo("Australia/Melbourne"))
HEALTH_CHANNEL_NAME = "bothealth"
HEALTH_CHANNEL_ID = 1502556311869591723
# --- LLM Provider Config ---
# DeepSeek is the brain for all text + chart analysis.
# Ollama is retained ONLY for embeddings (DeepSeek exposes no embeddings
# endpoint) and as an offline fallback for text generation.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "180"))

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# One model for everything. deepseek-v4-pro costs exactly 3x flash on every
# axis (input, cached input, output) while flash at reasoning_effort="max"
# roughly matches pro at its default effort — so capability is bought with the
# effort dial, not with a pricier model.
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
# Only this model accepts images; the others 400 on image input.
DEEPSEEK_VISION_MODEL = os.getenv("DEEPSEEK_VISION_MODEL", "deepseek-v4-flash-vision-exp")
# Deliberately flash, not pro. Override to "deepseek-v4-pro" only if the eval
# suite shows flash@max genuinely failing on your material.
DEEPSEEK_REASONING_MODEL = os.getenv("DEEPSEEK_REASONING_MODEL", "deepseek-v4-flash")

# --- Reasoning effort, per task ---
# "off" disables thinking entirely; otherwise "low" | "high" | "max".
# Thinking tokens bill as OUTPUT tokens, so effort is the real cost dial —
# paying for reasoning on a task that doesn't need it is pure waste.
#
# Journal extraction is pattern-matching over a short note, not reasoning.
DEEPSEEK_JOURNAL_EFFORT = os.getenv("DEEPSEEK_JOURNAL_EFFORT", "off")
# Coaching applies a framework and must resist drifting off it — worth thinking.
DEEPSEEK_COACH_EFFORT = os.getenv("DEEPSEEK_COACH_EFFORT", "high")
# The psych synthesis reasons over correlations; this is where max earns its keep.
DEEPSEEK_DEEP_EFFORT = os.getenv("DEEPSEEK_DEEP_EFFORT", "max")

# --- Ollama Config (embeddings + offline fallback) ---
OLLAMA_API_URL = "http://localhost:11434/api/generate"
# Only used when LLM_PROVIDER=ollama. Unset by default beyond this suggestion:
# a 27B model needs ~18GB, so this is a workstation fallback, not something the
# Pi can serve. nomic-embed-text (embeddings) is separate and runs anywhere.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.8:27b")
EMBED_MODEL = "nomic-embed-text"
# --- Paths ---
CATEGORY_NAME = "Fractal_Trades"
MISSED_CATEGORY_NAME = "Missed_Trades"

KB_DIR = BASE_DIR / "knowledge_base"
TRADES_DIR = BASE_DIR / "downloads" / "Fractal_Trades"
INSIGHTS_PATH = BASE_DIR / "master_insights.json"
TRACKER_PATH = BASE_DIR / "last_analyzed.txt"
CHROMA_DB_PATH = BASE_DIR / "chroma_db"
RAG_COLLECTION_NAME = "trading_knowledge"
CHECKLIST_PATH = BASE_DIR / "trade_checklist.json"
# --- Missed Trades (setups seen but never taken) ---
# Kept in separate files from taken trades: a missed trade has no execution to
# grade, so it produces a different kind of insight.
MISSED_DIR = BASE_DIR / "downloads" / "Missed_Trades"
MISSED_INSIGHTS_PATH = BASE_DIR / "missed_insights.json"
MISSED_TRACKER_PATH = BASE_DIR / "last_missed_analyzed.txt"
# Snapshots taken before destructive operations such as /reanalyze_trades.
BACKUPS_DIR = BASE_DIR / "backups"
# Persisted schedule/progress for /rebuild_all, so a restart (every deploy is
# one) resumes the rebuild instead of silently dropping it.
REBUILD_STATE_PATH = BASE_DIR / "rebuild_state.json"
# DeepSeek charges 2x during these UTC hours, Monday-Friday; weekends are
# off-peak all day. [start, end) in whole hours.
# Source: https://api-docs.deepseek.com/quick_start/pricing
DEEPSEEK_PEAK_HOURS_UTC = [(1, 4), (6, 10)]
# --- Trading Psychology (Denise Shull / ReThink method) ---
# Standalone emotional check-ins, appended one JSON object per line.
PSYCH_LOG_PATH = BASE_DIR / "psych_log.jsonl"
PSYCH_CHANNEL_NAME = "trading_psych"
# --- Live coaching ---
COACHING_DIR = BASE_DIR / "coaching"
COACH_PRINCIPLES_PATH = COACHING_DIR / "shull_principles.json"
COACH_LOG_PATH = BASE_DIR / "coach_log.jsonl"
COACH_SETTINGS_PATH = BASE_DIR / "coach_settings.json"
# Off: readings on every journal message were noise mid-entry. Reviews run
# when you press 🧠 Shull Review; /coach_auto turns per-message coaching on.
COACH_AUTO_DEFAULT = False
# --- Coach chat (#coach channel) ---
# A running conversation with a coach that can look up your own trades.
COACH_CHAT_CHANNEL_NAME = os.getenv("COACH_CHAT_CHANNEL_NAME", "coach")
COACH_CHAT_DIR = BASE_DIR / "coach_chat"
# Who the coach answers. Unset = anyone in the channel; set it to your Discord
# user id if other people can post there, since the memory is about one trader.
COACH_CHAT_USER_ID = _optional_int("COACH_CHAT_USER_ID")
# Coaching quality is the whole point here, so it thinks before replying.
DEEPSEEK_CHAT_EFFORT = os.getenv("DEEPSEEK_CHAT_EFFORT", "high")
# Your local clock, and the exchange clock the trades actually happen on.
LOCAL_TZ = "Australia/Melbourne"
MARKET_TZ = "America/New_York"
# Off-peak USD per 1M tokens for the flash model; peak is double.
# Source: https://api-docs.deepseek.com/quick_start/pricing
DEEPSEEK_PRICE_PER_M = {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60}
# --- Webhook Config ---
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")
ALERTS_CHANNEL_NAME = "trading_alerts"
COUCHDB_URL = os.getenv("COUCHDB_URL", "http://admin:password@127.0.0.1:5984")
COUCHDB_DB_NAME = "trading_alerts"
COUCHDB_BIAS_DB = "trading_bias"
# --- Bias Config ---
BIAS_CHANNEL_NAME = "trading_bias"
BIAS_ASSETS = ["ES", "NQ", "YM", "RTY", "GC", "SI", "CL", "RB", "OH"]
BIAS_RESET_TIME = datetime.time(hour=7, minute=0, tzinfo=ZoneInfo("Australia/Melbourne"))