# 💻 Windows Development Environment Setup for Malith
Write-Host "🚀 Starting Windows Dev Setup..." -ForegroundColor Cyan

# 1. Environment Setup
if (-not (Test-Path "venv")) {
    Write-Host "🐍 Creating virtual environment..." -ForegroundColor Yellow
    python -m venv venv
}

# 2. Activate Environment and Install Dependencies
Write-Host "📥 Installing packages from requirements.txt..." -ForegroundColor Yellow
& ".\venv\Scripts\python.exe" -m pip install --upgrade pip

if (Test-Path "requirements.txt") {
    & ".\venv\Scripts\pip.exe" install -r requirements.txt
} else {
    Write-Host "❌ Error: requirements.txt not found!" -ForegroundColor Red
    exit
}

# 3. Final Check
Write-Host "-----------------------------------------------" -ForegroundColor Green
Write-Host "✅ Windows setup complete!" -ForegroundColor Green
Write-Host "💡 To start coding, run: .\venv\Scripts\Activate.ps1" -ForegroundColor Gray
Write-Host "💡 Remember to update your .env with the new token!" -ForegroundColor Gray
Write-Host "-----------------------------------------------" -ForegroundColor Green