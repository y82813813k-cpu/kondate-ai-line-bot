# Kondate AI LINE Bot

LINE group bot that suggests healthy but satisfying dinner menus for two people.

## Features

- Sends three dinner options every morning at 08:00 Japan time
- Learns likes, dislikes, and rejected suggestions from conversation
- Manages ingredients and quantities
- Reads receipt images and asks before adding detected ingredients
- Updates inventory after the user confirms the meal was cooked

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
ENABLE_SCHEDULER=false
TASK_SECRET=
PORT=5000
```

## Render Free deploy settings

- Service type: Web Service
- Runtime: Python 3
- Instance type: Free
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn --workers 1 --bind 0.0.0.0:$PORT app:app`
- `ENABLE_SCHEDULER=false`

Render Free web services can sleep after inactivity. To send the daily 08:00 JST message reliably enough for a free prototype, this repo uses GitHub Actions to call the deployed app once per day.

Set these GitHub repository secrets:

```text
DAILY_TASK_URL=https://YOUR_DEPLOYED_DOMAIN/tasks/daily
TASK_SECRET=same_value_as_render_task_secret
```

Set the same `TASK_SECRET` value in Render environment variables.

## Persistent data

Render Free uses an ephemeral filesystem. The prototype will still work, but inventory and preferences can be lost when the service restarts or redeploys.

For a paid Render service with a persistent disk, set:

```env
DB_PATH=/var/data/kondate_ai.sqlite3
```

Mount the disk at `/var/data`.

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
