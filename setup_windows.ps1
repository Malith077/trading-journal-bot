# 1. Setup Virtual Environment if it doesn't exist
if (-not (Test-Path "venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    python -m venv venv
}

# 2. Sync Dependencies
Write-Host "Syncing dependencies..." -ForegroundColor Cyan
& ".\venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\venv\Scripts\pip.exe" install -r requirements.txt

# 3. Start the Bot
Write-Host "🚀 Starting bot in foreground..." -ForegroundColor Green
& ".\venv\Scripts\python.exe" bot.py