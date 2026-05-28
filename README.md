# Kondate AI LINE Bot

LINE group bot that suggests healthy but satisfying dinner menus for two people.

## Features

- Sends three dinner options every morning at 08:00 Japan time
- Learns likes, dislikes, and rejected suggestions from conversation
- Manages ingredients and quantities
- Reads receipt images and asks before adding detected ingredients
- Updates inventory after the user replies `作った`

## Environment variables

Set these on the deployment platform:

```env
OPENAI_API_KEY=
LINE_CHANNEL_SECRET=
LINE_CHANNEL_ACCESS_TOKEN=
OPENAI_MODEL=gpt-4o-mini
OPENAI_VISION_MODEL=gpt-4o-mini
DB_PATH=data/kondate_ai.sqlite3
SCHEDULE_HOUR=8
SCHEDULE_MINUTE=0
ENABLE_SCHEDULER=true
PORT=5000
```

For Render with a persistent disk, set:

```env
DB_PATH=/var/data/kondate_ai.sqlite3
```

Mount the disk at `/var/data`.

## Render deploy settings

- Service type: Web Service
- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn --workers 1 --bind 0.0.0.0:$PORT app:app`
- Instance type: Starter or higher is recommended for reliable 08:00 delivery

Free web services can spin down after inactivity, so the in-process scheduler might not run at exactly 08:00 if the service is asleep.

## LINE webhook

After deployment, set the LINE Developers webhook URL to:

```text
https://YOUR_DEPLOYED_DOMAIN/callback
```

Also enable:

- Webhook
- Allow bot to join group chats

## Local health check

```powershell
python app.py
```

Then open:

```text
http://localhost:5000/health
```
