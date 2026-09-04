import asyncio
import json
import os
import re
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import yt_dlp
from flask import Flask, jsonify, request
from telegram import InputMediaPhoto, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram")
EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_TELEGRAM_SIZE = 49 * 1024 * 1024
MAX_TELEGRAM_PHOTO_SIZE = 10 * 1024 * 1024
PROGRESS_UPDATE_INTERVAL = 1.5
DOWNLOAD_CONCURRENCY = 1
TIKWM_API = "https://tikwm.com/api/"
TIKTOK_API = "https://tdownv4.sl-bjs.workers.dev/"

progress_data = {}
progress_lock = threading.Lock()
download_semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)


def set_progress(chat_id, data):
    with progress_lock:
        progress_data[chat_id] = data


def clear_progress(chat_id):
    with progress_lock:
        progress_data.pop(chat_id, None)


def get_host(url):
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_supported_url(url):
    host = get_host(url)
    allowed = ("instagram.com", "tiktok.com", "x.com", "twitter.com")
    return any(host == domain or host.endswith("." + domain) for domain in allowed)


def is_tiktok_url(url):
    host = get_host(url)
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def is_tiktok_photo_url(url):
    return is_tiktok_url(url) and "/photo/" in urlparse(url).path.lower()


def is_instagram_url(url):
    host = get_host(url)
    return host == "instagram.com" or host.endswith(".instagram.com")


def run_ffmpeg(args, timeout=180):
    try:
        return subprocess.run(
            ["ffmpeg", "-y", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        print(f"FFmpeg skipped: {exc!r}")
        return None


def prepare_mp4_for_mobile(filename):
    if filename.suffix.lower() != ".mp4" or not filename.is_file():
        return filename
    optimized = filename.with_name(filename.stem + ".faststart.mp4")
    result = run_ffmpeg(["-i", str(filename), "-map", "0", "-c", "copy", "-movflags", "+faststart", str(optimized)])
    if result and result.returncode == 0 and optimized.is_file() and optimized.stat().st_size > 0:
        optimized.replace(filename)
    elif optimized.exists():
        optimized.unlink()
    return filename


def video_codecs(filename):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(filename)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=20, check=False,
        )
        video = (result.stdout or "").strip().splitlines()
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(filename)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=20, check=False,
        )
        audio = (result.stdout or "").strip().splitlines()
        return (video[0] if video else ""), (audio[0] if audio else "")
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return "", ""


def make_instagram_mobile_compatible(filename):
    if not filename.is_file():
        return filename
    video_codec, audio_codec = video_codecs(filename)
    if video_codec == "h264" and audio_codec in ("", "aac"):
        return prepare_mp4_for_mobile(filename)

    compatible = filename.with_name(filename.stem + ".compatible.mp4")
    result = run_ffmpeg([
        "-threads", "1", "-i", str(filename),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
        "-map_metadata", "-1", str(compatible),
    ], timeout=300)
    if result and result.returncode == 0 and compatible.is_file() and compatible.stat().st_size > 0:
        compatible.replace(filename)
        return filename
    if compatible.exists():
        compatible.unlink()
    return prepare_mp4_for_mobile(filename)


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
    prefix = prepared.stem + "."
    matches = sorted(
        (p for p in DOWNLOAD_DIR.glob(prefix + "*") if p.is_file() and not p.name.endswith(".part")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def download_direct_file(media_url, filename, chat_id, accept="*/*"):
    media_request = Request(media_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": accept,
    })
    with urlopen(media_request, timeout=60) as response, filename.open("wb") as output:
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        while True:
            chunk = response.read(512 * 1024)
            if not chunk:
                break
            output.write(chunk)
            downloaded += len(chunk)
            set_progress(chat_id, {
                "stage": "download", "percent": downloaded / total * 100 if total else 0,
                "downloaded": downloaded, "total": total,
            })
    if not filename.is_file() or filename.stat().st_size == 0:
        raise RuntimeError("Downloader returned an empty file")
    return filename


def tikwm_payload(url):
    api_url = f"{TIKWM_API}?url={quote(url, safe='')}&hd=1"
    req = Request(api_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
    })
    with urlopen(req, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != 0:
        raise RuntimeError(f"TikWM error: {payload.get('msg') or 'unknown error'}")
    return payload.get("data") or {}


def download_tiktok_photo_post(url, chat_id):
    data = tikwm_payload(url)
    images = data.get("images") or []
    if not images:
        raise RuntimeError("TikTok Photo Mode returned no images")

    post_id = str(data.get("id") or "tiktok_photo")
    paths = []
    try:
        for index, image_url in enumerate(images, start=1):
            filename = DOWNLOAD_DIR / f"{post_id}_{index:02d}.jpg"
            download_direct_file(
                image_url, filename, chat_id,
                accept="image/avif,image/webp,image/jpeg,image/*,*/*;q=0.8",
            )
            paths.append(filename)
        return paths, data.get("title") or "TikTok Photo"
    except Exception:
        for path in paths:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def download_from_tikwm(url, chat_id):
    data = tikwm_payload(url)
    media_url = data.get("hdplay") or data.get("play")
    if not media_url:
        raise RuntimeError("TikWM returned no video URL")
    video_id = str(data.get("id") or "tiktok")
    filename = DOWNLOAD_DIR / f"{video_id}.mp4"
    download_direct_file(media_url, filename, chat_id, accept="video/mp4,video/*,*/*;q=0.8")
    return prepare_mp4_for_mobile(filename), data.get("title") or "TikTok Video"


def download_from_ttdown(url, chat_id):
    api_url = f"{TIKTOK_API}?down={quote(url, safe='')}"
    req = Request(api_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
    })
    with urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    media_url = payload.get("download_url")
    if not media_url:
        raise RuntimeError(payload.get("message") or payload.get("error") or "Second TikTok API returned no video URL")
    video_id = str(payload.get("video_id") or "tiktok")
    filename = DOWNLOAD_DIR / f"{video_id}.mp4"
    download_direct_file(media_url, filename, chat_id, accept="video/mp4,video/*,*/*;q=0.8")
    return prepare_mp4_for_mobile(filename), payload.get("title") or "TikTok Video"


def download_video(url, chat_id):
    def progress_hook(data):
        status = data.get("status")
        if status == "downloading":
            downloaded = data.get("downloaded_bytes", 0) or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            set_progress(chat_id, {
                "stage": "download", "percent": downloaded / total * 100 if total else 0,
                "downloaded": downloaded, "total": total,
                "speed": data.get("speed") or 0, "eta": data.get("eta"),
            })
        elif status == "finished":
            set_progress(chat_id, {"stage": "download_finished", "percent": 100})

    options = {
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "format": "bv*+ba/b", "merge_output_format": "mp4", "noplaylist": True,
        "quiet": True, "no_warnings": True, "restrictfilenames": True,
        "progress_hooks": [progress_hook],
    }
    if is_tiktok_url(url):
        options["extractor_args"] = {"tiktok": {"app_info": [
            "7355728856979392260", "7351149742343391009", "7351144126450059040"
        ]}}

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = find_downloaded_file(info, ydl)
            if not filename:
                raise RuntimeError("Downloaded file not found")
            filename = make_instagram_mobile_compatible(filename) if is_instagram_url(url) else prepare_mp4_for_mobile(filename)
            return filename, info.get("title") or "Video"
    except Exception as primary_error:
        if not is_tiktok_url(url):
            raise
        print(f"TikTok yt-dlp failed, trying TikWM: {primary_error!r}")
        try:
            return download_from_tikwm(url, chat_id)
        except Exception as tikwm_error:
            print(f"TikWM failed, trying second TikTok API: {tikwm_error!r}")
            return download_from_ttdown(url, chat_id)


async def update_status_message(message, stop_event):
    while not stop_event.is_set():
        try:
            await message.edit_text("⏳ <b>جاري معالجة الرابط...</b>", parse_mode="HTML")
        except Exception:
            pass
        await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 <b>MOHD MEDIA BOT</b>\n\n"
        "أرسل الرابط وسأتولى الباقي.\n\n"
        "✅ Instagram / Reels\n✅ TikTok\n✅ X / Twitter\n\n"
        "🚀 تحميل بأفضل جودة متاحة\n⚡ بدون ضغط تلقائي\n\n"
        "🔗 أرسل الرابط الآن.", parse_mode="HTML",
    )


async def send_photo_album(message, paths, title):
    for start in range(0, len(paths), 10):
        batch = paths[start:start + 10]
        photo_paths = [p for p in batch if p.is_file() and p.stat().st_size <= MAX_TELEGRAM_PHOTO_SIZE]
        large_paths = [p for p in batch if p.is_file() and p.stat().st_size > MAX_TELEGRAM_PHOTO_SIZE]
        if photo_paths:
            handles = []
            media = []
            try:
                for path in photo_paths:
                    handle = path.open("rb")
                    handles.append(handle)
                    media.append(InputMediaPhoto(media=handle))
                if start == 0:
                    media[0].caption = f"🖼️ {title[:900]}"
                await message.reply_media_group(media=media)
            finally:
                for handle in handles:
                    handle.close()
        for path in large_paths:
            with path.open("rb") as photo:
                await message.reply_document(document=photo, caption="🖼️ صورة من TikTok Photo Mode")


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
        await update.message.reply_text("❌ هذا الموقع غير مدعوم حاليًا.\n\nالمواقع المدعومة:\n• Instagram\n• TikTok\n• X / Twitter")
        return

    chat_id = update.effective_chat.id
    status_message = await update.message.reply_text("⏳ <b>جاري معالجة الرابط...</b>", parse_mode="HTML")
    stop_event = threading.Event()
    progress_task = asyncio.create_task(update_status_message(status_message, stop_event))
    result = None

    try:
        async with download_semaphore:
            set_progress(chat_id, {"stage": "starting"})
            if is_tiktok_photo_url(url):
                result = await asyncio.to_thread(download_tiktok_photo_post, url, chat_id)
            else:
                result = await asyncio.to_thread(download_video, url, chat_id)

        stop_event.set()
        await progress_task

        if isinstance(result[0], list):
            paths, title = result
            await status_message.edit_text("📤 <b>جاري إرسال الصور...</b>", parse_mode="HTML")
            await send_photo_album(update.message, paths, title)
            await status_message.edit_text("✅ <b>تم التحميل والإرسال بنجاح</b>", parse_mode="HTML")
        else:
            filename, title = result
            if filename.stat().st_size > MAX_TELEGRAM_SIZE:
                raise RuntimeError("حجم الفيديو أكبر من الحد المسموح به للإرسال عبر Telegram Bot API.")
            await status_message.edit_text("📤 <b>جاري إرسال الفيديو...</b>", parse_mode="HTML")
            with filename.open("rb") as video:
                await update.message.reply_video(video=video, caption=f"🎬 {title[:900]}", supports_streaming=True)
            await status_message.edit_text("✅ <b>تم التحميل والإرسال بنجاح</b>", parse_mode="HTML")
    except Exception as exc:
        print(f"ERROR: {exc!r}")
        stop_event.set()
        try:
            await progress_task
        except Exception:
            pass
        if "أكبر من الحد" in str(exc):
            error_text = "⚠️ <b>الفيديو أكبر من الحد المسموح به</b>\n\nTelegram Bot API يسمح للبوت بإرسال ملفات تصل إلى 50 MB تقريبًا."
        else:
            error_text = "❌ <b>تعذر تحميل الرابط</b>\n\nتأكد أن الرابط عام ويمكن فتحه بدون تسجيل دخول، ثم جرب مرة أخرى."
        try:
            await status_message.edit_text(error_text, parse_mode="HTML")
        except Exception:
            pass
    finally:
        stop_event.set()
        clear_progress(chat_id)
        if result:
            paths = result[0] if isinstance(result[0], list) else [result[0]]
            for path in paths:
                if path and path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass


async def error_handler(update, context):
    print(f"Telegram error: {context.error!r}")


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
        future = asyncio.run_coroutine_threadsafe(application.update_queue.put(update), application_loop)
        future.result(timeout=5)
        return jsonify({"ok": True})
    except Exception as exc:
        print(f"Webhook error: {exc!r}")
        return jsonify({"ok": False}), 500


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


async def run_bot():
    global application, application_loop
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    if not EXTERNAL_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL is not set")
    application_loop = asyncio.get_running_loop()
    application = Application.builder().token(TOKEN).updater(None).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    await application.initialize()
    await application.start()
    webhook_url = f"{EXTERNAL_URL}/{WEBHOOK_PATH}"
    await application.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET or None, drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
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
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
