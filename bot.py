import asyncio
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram")
EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Leave a small safety margin below Telegram's official 50 MB bot upload limit.
MAX_TELEGRAM_SIZE = 49 * 1024 * 1024
PROGRESS_UPDATE_INTERVAL = 1.5
DOWNLOAD_CONCURRENCY = 1

progress_data = {}
progress_lock = threading.Lock()
download_semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def set_progress(chat_id, data):
    with progress_lock:
        progress_data[chat_id] = data


def get_progress(chat_id):
    with progress_lock:
        return dict(progress_data.get(chat_id, {}))


def clear_progress(chat_id):
    with progress_lock:
        progress_data.pop(chat_id, None)


def format_bytes(size):
    if not size:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.1f} {units[index]}"


def format_time(seconds):
    if seconds is None:
        return "--"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}ث"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}د {seconds}ث"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}س {minutes}د"


def progress_bar(percent, length=14):
    percent = max(0, min(100, float(percent)))
    filled = int(length * percent / 100)
    return "█" * filled + "░" * (length - filled)


def is_supported_url(url):
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False

    allowed = (
        "instagram.com",
        "tiktok.com",
        "x.com",
        "twitter.com",
    )
    return any(host == domain or host.endswith("." + domain) for domain in allowed)


def find_downloaded_file(info, ydl):
    prepared = Path(ydl.prepare_filename(info))
    candidates = [prepared]
    if prepared.suffix.lower() != ".mp4":
        candidates.append(prepared.with_suffix(".mp4"))
    for extension in (".webm", ".mkv", ".mov"):
        candidates.append(prepared.with_suffix(extension))

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    # Fallback for unusual post-processing/extension changes.
    prefix = prepared.stem + "."
    matches = sorted(
        DOWNLOAD_DIR.glob(prefix + "*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


# -----------------------------------------------------------------------------
# Download
# -----------------------------------------------------------------------------

def download_video(url, chat_id):
    def progress_hook(data):
        status = data.get("status")
        if status == "downloading":
            downloaded = data.get("downloaded_bytes", 0) or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            percent = (downloaded / total * 100) if total else 0
            set_progress(
                chat_id,
                {
                    "stage": "download",
                    "percent": percent,
                    "downloaded": downloaded,
                    "total": total,
                    "speed": data.get("speed") or 0,
                    "eta": data.get("eta"),
                },
            )
        elif status == "finished":
            set_progress(chat_id, {"stage": "download_finished", "percent": 100})

    options = {
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "progress_hooks": [progress_hook],
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = find_downloaded_file(info, ydl)
        if not filename:
            raise RuntimeError("Downloaded file not found")
        title = info.get("title") or "Video"
        return filename, title


# -----------------------------------------------------------------------------
# Telegram status
# -----------------------------------------------------------------------------

async def update_status_message(message, chat_id, stop_event):
    last_text = None
    while not stop_event.is_set():
        data = get_progress(chat_id)
        stage = data.get("stage")

        if stage == "download":
            percent = data.get("percent", 0)
            downloaded = data.get("downloaded", 0)
            total = data.get("total", 0)
            speed = data.get("speed", 0)
            eta = data.get("eta")
            text = (
                "🎬 <b>MOHD MEDIA</b>\n\n"
                "⬇️ <b>جاري تحميل الفيديو</b>\n\n"
                f"{progress_bar(percent)} <b>{percent:.0f}%</b>\n\n"
                f"📦 {format_bytes(downloaded)}"
            )
            if total:
                text += f" / {format_bytes(total)}"
            if speed:
                text += f"\n⚡ السرعة: {format_bytes(speed)}/ث"
            if eta is not None:
                text += f"\n⏱️ المتبقي: {format_time(eta)}"
        elif stage == "download_finished":
            text = (
                "🎬 <b>MOHD MEDIA</b>\n\n"
                "✅ اكتمل التحميل\n"
                "🔍 جاري تجهيز الفيديو..."
            )
        else:
            text = "🎬 <b>MOHD MEDIA</b>\n\n⏳ جاري معالجة الرابط..."

        if text != last_text:
            try:
                await message.edit_text(text, parse_mode="HTML")
                last_text = text
            except Exception:
                pass

        await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)


# -----------------------------------------------------------------------------
# Telegram handlers
# -----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 <b>MOHD MEDIA BOT</b>\n\n"
        "أرسل رابط الفيديو وسأتولى الباقي.\n\n"
        "✅ Instagram / Reels\n"
        "✅ TikTok\n"
        "✅ X / Twitter\n\n"
        "🚀 تحميل بأفضل جودة متاحة\n"
        "📊 عرض نسبة التحميل\n"
        "⚡ بدون ضغط تلقائي\n\n"
        "🔗 أرسل الرابط الآن.",
        parse_mode="HTML",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    urls = re.findall(r"https?://\S+", text)
    if not urls:
        await update.message.reply_text("🔗 أرسل رابط الفيديو.")
        return

    url = urls[0].rstrip(".,)")
    if not is_supported_url(url):
        await update.message.reply_text(
            "❌ هذا الموقع غير مدعوم حاليًا.\n\n"
            "المواقع المدعومة:\n"
            "• Instagram\n"
            "• TikTok\n"
            "• X / Twitter"
        )
        return

    chat_id = update.effective_chat.id
    status_message = await update.message.reply_text(
        "🎬 <b>MOHD MEDIA</b>\n\n⏳ جاري تجهيز الرابط...",
        parse_mode="HTML",
    )

    stop_event = threading.Event()
    progress_task = asyncio.create_task(
        update_status_message(status_message, chat_id, stop_event)
    )
    filename = None

    try:
        async with download_semaphore:
            set_progress(chat_id, {"stage": "starting"})
            filename, title = await asyncio.to_thread(download_video, url, chat_id)

        file_size = filename.stat().st_size
        if file_size > MAX_TELEGRAM_SIZE:
            raise RuntimeError(
                "حجم الفيديو أكبر من الحد المسموح به للإرسال عبر Telegram Bot API."
            )

        stop_event.set()
        await progress_task

        await status_message.edit_text(
            "📤 <b>جاري إرسال الفيديو...</b>",
            parse_mode="HTML",
        )

        with filename.open("rb") as video:
            await update.message.reply_video(
                video=video,
                caption=f"🎬 {title[:900]}",
                supports_streaming=True,
            )

        await status_message.edit_text(
            "✅ <b>تم التحميل والإرسال بنجاح</b>\n\n"
            f"📦 الحجم: {format_bytes(file_size)}",
            parse_mode="HTML",
        )

    except Exception as exc:
        print(f"ERROR: {exc!r}")
        stop_event.set()
        try:
            await progress_task
        except Exception:
            pass

        if "أكبر من الحد" in str(exc):
            error_text = (
                "⚠️ <b>الفيديو أكبر من الحد المسموح به</b>\n\n"
                "Telegram Bot API يسمح للبوت بإرسال ملفات تصل إلى 50 MB تقريبًا.\n"
                "هذا الإصدار لا يضغط الفيديو حتى لا يضيع الوقت أو الجودة."
            )
        else:
            error_text = (
                "❌ <b>تعذر تحميل الفيديو</b>\n\n"
                "تأكد أن الرابط عام ويمكن فتحه بدون تسجيل دخول، ثم جرب مرة أخرى."
            )

        try:
            await status_message.edit_text(error_text, parse_mode="HTML")
        except Exception:
            pass
    finally:
        stop_event.set()
        clear_progress(chat_id)
        if filename and filename.exists():
            try:
                filename.unlink()
            except Exception:
                pass


async def error_handler(update, context):
    print(f"Telegram error: {context.error!r}")


# -----------------------------------------------------------------------------
# Render / webhook server
# -----------------------------------------------------------------------------

flask_app = Flask(__name__)
application = None
application_loop = None


@flask_app.get("/")
def health():
    return jsonify({"status": "ok", "service": "MOHD Media Bot"})


@flask_app.get("/health")
def health_alias():
    return jsonify({"status": "ok"})


@flask_app.post(f"/{WEBHOOK_PATH}")
def telegram_webhook():
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if supplied != WEBHOOK_SECRET:
            return jsonify({"ok": False}), 403

    if application is None or application_loop is None:
        return jsonify({"ok": False, "error": "bot_not_ready"}), 503

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"ok": False}), 400

    try:
        update = Update.de_json(payload, application.bot)
        future = asyncio.run_coroutine_threadsafe(
            application.update_queue.put(update), application_loop
        )
        future.result(timeout=5)
        return jsonify({"ok": True})
    except Exception as exc:
        print(f"Webhook error: {exc!r}")
        return jsonify({"ok": False}), 500


def run_flask():
    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


async def run_bot():
    global application, application_loop

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    if not EXTERNAL_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL is not set")

    application_loop = asyncio.get_running_loop()
    application = (
        Application.builder()
        .token(TOKEN)
        .updater(None)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_error_handler(error_handler)

    await application.initialize()
    await application.start()

    webhook_url = f"{EXTERNAL_URL}/{WEBHOOK_PATH}"
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

    print(f"MOHD Media Bot is running: {webhook_url}")

    server_thread = threading.Thread(target=run_flask, daemon=True)
    server_thread.start()

    try:
        await asyncio.Event().wait()
    finally:
        try:
            await application.bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass
        await application.stop()
        await application.shutdown()


def main():
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("Bot stopped.")
    except Exception as exc:
        print(f"FATAL: {exc!r}")
        raise


if __name__ == "__main__":
    main()
