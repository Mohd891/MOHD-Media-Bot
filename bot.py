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
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram").strip("/")
EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_TELEGRAM_SIZE = 49 * 1024 * 1024
MAX_TELEGRAM_PHOTO_SIZE = 10 * 1024 * 1024
PROGRESS_UPDATE_INTERVAL = 2.0
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
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        print(f"FFmpeg skipped: {exc!r}")
        return None


def video_codecs(filename):
    try:
        def probe(selector):
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", selector,
                 "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(filename)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=20,
                check=False,
            )
            values = (result.stdout or "").strip().splitlines()
            return values[0] if values else ""
        return probe("v:0"), probe("a:0")
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return "", ""


def video_dimensions(filename):
    """Return the video's display width/height without changing the media."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:stream_tags=rotate",
                "-of", "json", str(filename),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
            check=False,
        )
        streams = (json.loads(result.stdout or "{}").get("streams") or [])
        if not streams:
            return None
        stream = streams[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        if not width or not height:
            return None
        rotate = int(float((stream.get("tags") or {}).get("rotate") or 0)) % 360
        if rotate in (90, 270):
            width, height = height, width
        return width, height
    except (ValueError, TypeError, json.JSONDecodeError, FileNotFoundError, subprocess.SubprocessError, OSError):
        return None


def prepare_mp4_for_mobile(filename):
    if filename.suffix.lower() != ".mp4" or not filename.is_file():
        return filename
    optimized = filename.with_name(filename.stem + ".faststart.mp4")
    result = run_ffmpeg([
        "-threads", "1", "-i", str(filename), "-map", "0",
        "-c", "copy", "-movflags", "+faststart", str(optimized)
    ])
    if result and result.returncode == 0 and optimized.is_file() and optimized.stat().st_size > 0:
        optimized.replace(filename)
    elif optimized.exists():
        optimized.unlink()
    return filename


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
        "-map_metadata", "-1", str(compatible)
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

    prefix = prepared.stem + "."
    candidates.extend(p for p in DOWNLOAD_DIR.glob(prefix + "*") if p.is_file() and not p.name.endswith(".part"))

    unique = []
    seen = set()
    for candidate in candidates:
        if candidate.is_file() and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)

    # When yt-dlp downloads separate video/audio streams, several files can
    # temporarily exist. Never accidentally pick the audio-only file.
    inspected = []
    for candidate in unique:
        video_codec, audio_codec = video_codecs(candidate)
        if video_codec:
            inspected.append((bool(audio_codec), candidate.stat().st_mtime, candidate))

    if inspected:
        inspected.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return inspected[0][2]
    return None


def download_direct_file(media_url, filename, chat_id, accept="*/*", max_size=None):
    media_request = Request(media_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": accept,
    })
    try:
        with urlopen(media_request, timeout=60) as response, filename.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            if max_size and total and total > max_size:
                raise RuntimeError("Media file is larger than Telegram's upload limit")
            downloaded = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if max_size and downloaded > max_size:
                    raise RuntimeError("Media file is larger than Telegram's upload limit")
                output.write(chunk)
                set_progress(chat_id, {
                    "stage": "download",
                    "percent": downloaded / total * 100 if total else 0,
                    "downloaded": downloaded,
                    "total": total,
                })
    except Exception:
        try:
            filename.unlink()
        except OSError:
            pass
        raise
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
                max_size=MAX_TELEGRAM_PHOTO_SIZE,
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
    download_direct_file(media_url, filename, chat_id,
                         accept="video/mp4,video/*,*/*;q=0.8", max_size=MAX_TELEGRAM_SIZE)
    return filename, data.get("title") or "TikTok Video"


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
    download_direct_file(media_url, filename, chat_id,
                         accept="video/mp4,video/*,*/*;q=0.8", max_size=MAX_TELEGRAM_SIZE)
    return filename, payload.get("title") or "TikTok Video"


def download_video(url, chat_id):
    def progress_hook(data):
        if data.get("status") == "downloading":
            downloaded = data.get("downloaded_bytes", 0) or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            set_progress(chat_id, {
                "stage": "download", "percent": downloaded / total * 100 if total else 0,
                "downloaded": downloaded, "total": total,
                "speed": data.get("speed") or 0, "eta": data.get("eta"),
            })
        elif data.get("status") == "finished":
            set_progress(chat_id, {"stage": "download_finished", "percent": 100})

    format_selector = "bv*+ba/b" if is_instagram_url(url) else "b[ext=mp4]/b/bv*+ba"
    options = {
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "format": format_selector,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "progress_hooks": [progress_hook],
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
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
                raise RuntimeError("Downloaded video file not found")
            if filename.stat().st_size > MAX_TELEGRAM_SIZE:
                raise RuntimeError("حجم الفيديو أكبر من حد Telegram المسموح للبوت (50 MB).")

            if is_instagram_url(url):
                _, audio_codec = video_codecs(filename)
                if not audio_codec:
                    raise RuntimeError("Instagram Reel was downloaded without an audio stream")
                filename = make_instagram_mobile_compatible(filename)
                _, audio_codec = video_codecs(filename)
                if not audio_codec:
                    raise RuntimeError("Instagram Reel audio stream is missing after processing")

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
        "🎬 <b>MOHD MEDIA BOT</b>\n\nأرسل الرابط وسأتولى الباقي.\n\n"
        "✅ Instagram / Reels\n✅ TikTok\n✅ X / Twitter\n\n"
        "🚀 تحميل بأفضل جودة متاحة\n⚡ بدون ضغط تلقائي\n\n🔗 أرسل الرابط الآن.",
        parse_mode="HTML")


async def send_photo_album(message, paths, title):
    for start in range(0, len(paths), 10):
        batch = paths[start:start + 10]
        photo_paths = [p for p in batch if p.is_file() and p.stat().st_size <= MAX_TELEGRAM_PHOTO_SIZE]
        large_paths = [p for p in batch if p.is_file() and p.stat().st_size > MAX_TELEGRAM_PHOTO_SIZE]
        if photo_paths:
            handles, media = [], []
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
            if is_tiktok_photo_url(url):
                result = await asyncio.to_thread(download_tiktok_photo_post, url, chat_id)
            else:
                result = await asyncio.to_thread(download_video, url, chat_id)
        stop_event.set()
        await progress_task

        if isinstance(result[0], list):
            paths, title = result
            if not paths:
                raise RuntimeError("No images were downloaded")
            await send_photo_album(update.message, paths, title)
            return

        filename, title = result
        if not filename or not filename.is_file():
            raise RuntimeError("Downloaded file is missing")
        if filename.stat().st_size > MAX_TELEGRAM_SIZE:
            raise RuntimeError("❌ حجم الفيديو أكبر من الحد المسموح في Telegram (50 MB).")

        # Telegram accepts explicit video dimensions. Supplying the real display
        # dimensions prevents some TikTok files with rotation/display metadata
        # from being rendered as a square in Telegram.
        dimensions = video_dimensions(filename)
        if dimensions:
            width, height = dimensions
            print(f"Sending video dimensions: {width}x{height}")
        else:
            width = height = None

        with filename.open("rb") as video:
            await update.message.reply_video(
                video=video,
                caption=f"🎬 {title[:900]}",
                width=width,
                height=height,
                supports_streaming=True,
            )

    except Exception as exc:
        stop_event.set()
        try:
            await progress_task
        except Exception:
            pass
        print(f"Download error for {url}: {exc!r}")
        message = str(exc)
        if "50 MB" in message:
            user_message = "❌ حجم الفيديو أكبر من الحد المسموح في Telegram (50 MB)."
        elif is_tiktok_photo_url(url):
            user_message = "❌ تعذر تحميل صور TikTok. تأكد أن الرابط عام ثم جرّب مرة أخرى."
        elif "without an audio stream" in message or "audio stream is missing" in message:
            user_message = "❌ تعذر تجهيز صوت الريل. جرّب الرابط مرة أخرى."
        else:
            user_message = "❌ تعذر تحميل الفيديو\n\nتأكد أن الرابط عام ويمكن فتحه بدون تسجيل دخول، ثم جرّب مرة أخرى."
        try:
            await status_message.edit_text(user_message)
        except Exception:
            try:
                await update.message.reply_text(user_message)
            except Exception:
                pass
    finally:
        stop_event.set()
        if not progress_task.done():
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        clear_progress(chat_id)
        if result:
            paths = result[0] if isinstance(result[0], list) else [result[0]]
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def create_application():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application


app = Flask(__name__)
telegram_application = create_application()
bot_loop = asyncio.new_event_loop()


def bot_loop_runner():
    asyncio.set_event_loop(bot_loop)
    bot_loop.run_until_complete(telegram_application.initialize())
    bot_loop.run_until_complete(telegram_application.start())
    if EXTERNAL_URL:
        webhook_url = f"{EXTERNAL_URL}/{WEBHOOK_PATH}"
        kwargs = {"url": webhook_url}
        if WEBHOOK_SECRET:
            kwargs["secret_token"] = WEBHOOK_SECRET
        bot_loop.run_until_complete(telegram_application.bot.set_webhook(**kwargs))
        print(f"MOHD Media Bot is running: {webhook_url}")
    else:
        print("MOHD Media Bot is running without RENDER_EXTERNAL_URL")
    bot_loop.run_forever()


@app.get("/")
def health():
    return jsonify({"status": "ok", "service": "MOHD Media Bot"})


@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if WEBHOOK_SECRET:
        provided_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if provided_secret != WEBHOOK_SECRET:
            return jsonify({"ok": False}), 403
    try:
        data = request.get_json(force=True, silent=False)
        update = Update.de_json(data, telegram_application.bot)
        asyncio.run_coroutine_threadsafe(telegram_application.process_update(update), bot_loop)
        return jsonify({"ok": True})
    except Exception as exc:
        print(f"Webhook error: {exc!r}")
        return jsonify({"ok": False}), 500


if __name__ == "__main__":
    threading.Thread(target=bot_loop_runner, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
