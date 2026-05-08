#!/bin/bash

# 1. Setup Virtual Environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# 2. Activate and Install Dependencies
echo "Syncing dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 3. Start the Bot
echo "🚀 Starting bot in foreground..."
python3 bot.py