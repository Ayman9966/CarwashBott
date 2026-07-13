# Wash & Scan Bot

Telegram bot for managing car wash / service orders with SQLite database, categories, services, reports, and CSV backup.

## Features

- Dynamic categories & services management
- Daily/monthly reports & statistics
- CSV export/import for backup
- Automatic daily backup to admin
- Delete last order
- Custom price entry support

## Setup

### 1. Get Bot Token
- Message @BotFather on Telegram
- Create a new bot and copy the token

### 2. Get Your Chat ID
- Message @userinfobot on Telegram
- Copy your ID number

### 3. Local Development

```bash
# Clone the repo
git clone <your-repo-url>
cd wash-and-scan-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create .env file
cp .env.example .env
# Edit .env and add your BOT_TOKEN and ADMIN_CHAT_ID

# Run
python bot.py
```

## Deploy to Render

### Method 1: Using render.yaml (Blueprint)
1. Push code to GitHub
2. In Render dashboard -> "New" -> "Blueprint"
3. Connect your GitHub repo
4. Render will read `render.yaml` automatically
5. Add environment variables in the dashboard:
   - `BOT_TOKEN` = your bot token
   - `ADMIN_CHAT_ID` = your chat ID

### Method 2: Manual Worker
1. In Render dashboard -> "New" -> "Background Worker"
2. Connect your GitHub repo
3. Set:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
4. Add environment variables:
   - `BOT_TOKEN`
   - `ADMIN_CHAT_ID`
5. Click "Create Background Worker"

> **Note:** Use a **Background Worker** (not Web Service) because this bot uses polling (`infinity_polling`), not webhooks.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Telegram bot token from @BotFather |
| `ADMIN_CHAT_ID` | Your Telegram chat ID for backup reports |

## Project Structure

```
.
|-- bot.py              # Main bot code
|-- requirements.txt    # Python dependencies
|-- render.yaml         # Render deployment config
|-- .env.example        # Environment variables template
|-- .gitignore          # Git ignore rules
|-- README.md           # This file
```

## License

MIT