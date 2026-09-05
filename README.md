# MOHD Media Bot

Telegram media downloader for public Instagram, TikTok, and X/Twitter links.

## Render

- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `python bot.py`
- Required environment variable: `BOT_TOKEN`
- Optional environment variable: `INSTAGRAM_COOKIES` for Instagram content that requires a logged-in session.
- The app uses Render's `RENDER_EXTERNAL_URL` automatically for the Telegram webhook.

## Notes

The official Telegram Bot API has a 50 MB upload limit for bots. This version does not compress videos; files over the limit are rejected with a clear message.

Downloaded temporary files are removed after processing.
