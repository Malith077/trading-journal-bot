import os
import datetime
from dotenv import load_dotenv

load_dotenv()

# --- Discord Config ---
BOT_TOKEN = os.getenv('BOT_KEY')
REMINDER_CHANNEL_ID = 1501833920449351720
REMINDER_TIME = datetime.time(hour=8, minute=0)

# --- Ollama Config ---
OLLAMA_API_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma4:31b-cloud"

# --- Paths ---
CATEGORY_NAME = "Fractal_Trades"