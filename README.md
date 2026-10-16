# GTU Menu Bot

![tests](https://github.com/mustii9x/gtu-menu-bot/actions/workflows/tests.yml/badge.svg)

A Telegram bot that sends Gebze Technical University students the day's cafeteria menu every morning at 07:30 (Istanbul time), in Turkish or English, as a private message.

GTU publishes the menu once a month as a picture. Once a month the admin sends that picture to the bot, a vision LLM turns it into structured bilingual JSON, the bot validates it and shows a preview, and nothing reaches students until the admin approves it.

## Features

**Students**

| Command | What it does |
|---|---|
| `/start` | Subscribe to the daily menu and pick a language (pre-selected from the Telegram app language) |
| `/menu` | Today's menu (if GTU hasn't released this month's menu yet, it says so instead of "no menu") |
| `/tomorrow` | Tomorrow's menu (works across month boundaries) |
| `/language` | Switch between Türkçe and English |
| `/stop` | Stop the daily message (`/start` resumes) |
| `/privacy` | Exactly what data is stored |
| `/feedback` | Send a message to the admin |
| `/help` | Command list |

**Admin** (only from `ADMIN_CHAT_ID`)

- Send the monthly menu **photo** → AI extraction → validation → preview → `/approve` or `/reject`
- Upload a hand-fixed **`YYYY-MM.json`** file → same validation and preview
- `/pause` — stop the daily message for everyone (kept across restarts). The bot then offers to tell subscribers either *"GTU hasn't published the October menu yet"* or *"short pause"*, or nothing
- `/resume` — turn the daily message back on, optionally telling everyone it's back
- `/stats` — daily message status (on/paused), users, subscribers, TR/EN split, new users today/week/month, /menu and /tomorrow requests in the last 7 days
- Alerts: missing menu file on a weekday, >10% broadcast failures, restart after a crash, failed AI extraction
- Feedback messages from students, labelled with their language

## Architecture

```
bot/
├── main.py       # starts the bot, registers handlers and the daily job
├── handlers.py   # user and admin commands, language buttons
├── broadcast.py  # the 07:30 daily send, with per-user error handling
├── menu.py       # loads JSON, finds a day's menu, formats it in TR or EN
├── db.py         # SQLite users table + in-memory request counters
├── i18n.py       # looks up text in the user's language
├── pipeline.py   # photo -> AI -> JSON -> validation -> preview -> save
└── config.py     # reads .env
locales/          # tr.json, en.json — all user-facing text
data/menus/       # 2026-10.json (+ 2026-10.jpg), one file per month
tests/            # pytest suite
deploy/           # systemd unit
```

`menu.py`, `db.py`, `i18n.py` and the validation in `pipeline.py` know nothing about Telegram, so they are tested on their own. The broadcast and handlers are tested with small fakes, with no network needed.

**Stack:** Python 3.11+, python-telegram-bot 21 (job queue), SQLite, python-dotenv, Anthropic vision API, pytest, GitHub Actions, systemd.

### Menu file format

```json
{
  "2026-10-14": [
    {"tr": "Mercimek çorbası", "en": "Red lentil soup", "kcal": 209},
    {"options": [
      {"tr": "Pirinç pilavı", "en": "Rice pilaf", "kcal": 416},
      {"tr": "Acı soslu makarna", "en": "Pasta with spicy sauce", "kcal": 322}
    ]},
    {"tr": "Meyve", "en": "Fruit"}
  ]
}
```

Only days with a menu appear. Every dish has both `tr` and `en`; `kcal` is optional. An `options` entry is a choice (students take one), shown as `Pirinç pilavı / Acı soslu makarna — 416 / 322 kcal`. Students see each dish's calories and the day's total (a range when there's a choice).

The vegan alternative (the "Zeytinyağlı …" dish on the GTU picture) is left out for now; set `INCLUDE_VEGAN_OPTION = True` in `bot/pipeline.py` to include it in future extractions.

## Privacy

Four fields per user: `chat_id`, `language`, `subscribed`, `joined_at`. No names, usernames or phone numbers. Feedback is forwarded to the admin, never stored. The database lives only on the server and is git-ignored. This keeps the project simple under KVKK.

## Running locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # then fill in BOT_TOKEN, ADMIN_CHAT_ID, LLM_API_KEY
pytest
python -m bot.main
```

- `BOT_TOKEN`: create a bot with [@BotFather](https://t.me/BotFather).
- `ADMIN_CHAT_ID`: your own chat ID (message [@userinfobot](https://t.me/userinfobot)).
- `LLM_API_KEY`: an Anthropic API key. Set `LLM_MODEL` to a current vision-capable model. To use another provider, replace `call_vision_llm()` in `pipeline.py`.

## Monthly routine (~10 minutes)

If GTU is late (e.g. October's menu only comes out on 1 October), send `/pause` and tap **📭 menu not published** so students know why nothing arrived. Until the file exists, `/menu` and `/tomorrow` answer "not published yet" on their own. Once you `/approve` the new menu, send `/resume`.

1. Send the menu picture to the bot. Add the caption `2026-11` to pick the month; without one, a picture sent after the 20th is treated as next month's menu.
2. Read the preview. Warnings (missing weekdays, missing Turkish letters, more than 8 dishes) are listed first.
3. `/approve` to publish it, or `/reject` and resend the photo or upload a fixed `2026-11.json`.
4. On the server, commit the new files so every month has a history:
   `git add data/menus && git commit -m "Menu 2026-11" && git push`

## Deployment (Linux VPS)

The bot uses polling, so no domain, HTTPS or webhook is needed.

```bash
sudo adduser --disabled-password menubot
sudo -iu menubot
git clone https://github.com/YOUR_GITHUB_USERNAME/gtu-menu-bot.git && cd gtu-menu-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env && chmod 600 .env
exit
sudo cp /home/menubot/gtu-menu-bot/deploy/gtu-menu-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gtu-menu-bot
journalctl -u gtu-menu-bot -f
```

Updating: `git pull && sudo systemctl restart gtu-menu-bot`.

## Security

- Bot token, API key and admin chat ID live only in `.env` (git-ignored); the repo has `.env.example` with empty values.
- If the token ever leaks, revoke it in BotFather immediately.
- Admin commands check the sender's chat ID on every call; only the admin can trigger the paid AI pipeline.
- HTTP request logging is turned down so the token (part of every Telegram API URL) never lands in the logs.

## Out of scope for v1

Per-user delivery times, weekly summaries, a web page.
