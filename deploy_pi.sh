#!/bin/bash

# Configuration
USER_NAME="malith"
BOT_DIR="/home/$USER_NAME/bot"
SERVICE_NAME="tradingbot"

echo "🚀 Starting Pi Deployment for Malith..."

# 1. Timezone Sync
sudo timedatectl set-timezone Australia/Melbourne

# 2. Cleanup & Environment Setup
cd $BOT_DIR
echo "🧹 Removing old venv to prevent architecture mismatches..."
rm -rf venv bin lib include pyvenv.cfg

echo "🐍 Creating fresh ARM64 virtual environment..."
python3 -m venv venv
source venv/bin/activate

# 3. Install from your requirements file
if [ -f "requirements.txt" ]; then
    echo "📥 Installing packages from requirements.txt..."
    pip install --upgrade pip
    pip install -r requirements.txt
else
    echo "⚠️ requirements.txt not found! Installing defaults..."
    pip install discord.py aiohttp python-dotenv
fi

# 4. Systemd Service Configuration
echo "⚙️ Configuring systemd service..."
sudo tee /etc/systemd/system/$SERVICE_NAME.service <<EOF
[Unit]
Description=Discord Trading Bot
After=network-target

[Service]
User=$USER_NAME
Group=$USER_NAME
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python $BOT_DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 5. Launch
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME.service
sudo systemctl restart $SERVICE_NAME.service

echo "✅ Deployment complete. Use 'journalctl -u $SERVICE_NAME -f' to see logs."