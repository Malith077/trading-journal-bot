import os
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
# --- Discord Config ---
BOT_TOKEN = os.getenv('BOT_KEY')
REMINDER_CHANNEL_ID = 1501833920449351720
REMINDER_TIME = datetime.time(hour=9, minute=30, tzinfo=ZoneInfo("Australia/Melbourne"))
HEALTH_CHANNEL_NAME = "bothealth"
HEALTH_CHANNEL_ID = 1502556311869591723
# --- Ollama Config ---
OLLAMA_API_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma4:31b-cloud"
EMBED_MODEL = "nomic-embed-text"
# --- Paths ---
CATEGORY_NAME = "Fractal_Trades"

KB_DIR = BASE_DIR / "knowledge_base"
TRADES_DIR = BASE_DIR / "downloads" / "Fractal_Trades"
INSIGHTS_PATH = BASE_DIR / "master_insights.json"
TRACKER_PATH = BASE_DIR / "last_analyzed.txt"
CHROMA_DB_PATH = BASE_DIR / "chroma_db"
RAG_COLLECTION_NAME = "trading_knowledge"