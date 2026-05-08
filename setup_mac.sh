#!/bin/bash

echo "💻 Setting up Mac Development Environment..."

# 1. Environment Setup
# We don't nuke the venv on Mac unless you want to, 
# but it's safer to ensure it exists.
if [ ! -d "venv" ]; then
    echo "🐍 Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate

# 2. Install from your requirements file
if [ -f "requirements.txt" ]; then
    echo "📥 Installing packages from requirements.txt..."
    pip install --upgrade pip
    pip install -r requirements.txt
else
    echo "❌ Error: requirements.txt not found."
    exit 1
fi

echo "✅ Mac setup complete. Run 'source venv/bin/activate' to start coding."