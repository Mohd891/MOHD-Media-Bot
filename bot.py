import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

import yt_dlp
import ytdlp_patches  # noqa: F401  (Instagram iOS-first + web fallback)
from flask import Flask, jsonify, request
from telegram import InputMediaPhoto, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import sys

# Show print() output in Render logs immediately (stdout is buffered otherwise).
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

TOKEN = os.getenv("BOT_TOKEN")
KEEP_AWAKE_INTERVAL = int(os.getenv("KEEP_AWAKE_INTERVAL", "600"))
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram").strip("/")
EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_TELEGRAM_SIZE = 49 * 1024 * 1024
MAX_TELEGRAM_PHOTO_SIZE = 10 * 1024 * 1024
PROGRESS_UPDATE_INTERVAL = 3.0
UPLOAD_TIMEOUT = 300
DOWNLOAD_CONCURRENCY = 1
TIKWM_API = "https://www.tikwm.com/api/"
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
    return any(host == d or host.endswith("." + d) for d in ("instagram.com", "tiktok.com", "x.com", "twitter.com"))


def is_tiktok_url(url):
    host = get_host(url)
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def is_tiktok_photo_url(url):
    if not is_tiktok_url(url):
        return False
    path = urlparse(url).path.lower()
    return "/photo/" in path or path.endswith("/photo")


def clean_tiktok_photo_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def is_instagram_url(url):
    host = get_host(url)
    return host == "instagram.com" or host.endswith(".instagram.com")


def run_ffmpeg(args, timeout=120):
    try:
        return subprocess.run(["ffmpeg", "-y", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        print(f"FFmpeg error: {exc!r}")
        return None


def probe_streams(filename):
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type,codec_name,width,height:stream_tags=rotate", "-of", "json", str(filename)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15, check=False)
        return json.loads(result.stdout or "{}").get("streams") or []
    except Exception:
        return []


def video_codecs(filename):
    streams = probe_streams(filename)
    video = next((s.get("codec_name") for s in streams if s.get("codec_type") == "video"), "")
    audio = next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), "")
    return video or "", audio or ""


def video_dimensions(filename):
    stream = next((s for s in probe_streams(filename) if s.get("codec_type") == "video"), None)
    if not stream:
        return None
    try:
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
        rotate = int(float((stream.get("tags") or {}).get("rotate") or 0)) % 360
        if rotate in (90, 270):
            width, height = height, width
        return (width, height) if width and height else None
    except Exception:
        return None


def ensure_h264(filename, work_dir):
    """Re-encode non-H.264 video (Instagram often serves VP9) so every Telegram client can play it."""
    vcodec, _ = video_codecs(filename)
    if vcodec in ("", "h264"):
        return filename
    print(f"Converting {vcodec} video to H.264")
    output = work_dir / "converted_h264.mp4"
    result = run_ffmpeg(["-i", str(filename), "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale='min(720,iw)':-2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output)], timeout=300)
    if result and result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
        return output
    output.unlink(missing_ok=True)
    print("H.264 conversion failed; sending original video")
    return filename


def faststart_mp4(filename):
    if not filename.is_file() or filename.suffix.lower() != ".mp4":
        return filename
    optimized = filename.with_name(filename.stem + ".faststart.mp4")
    result = run_ffmpeg(["-threads", "1", "-i", str(filename), "-map", "0", "-c", "copy", "-movflags", "+faststart", str(optimized)], timeout=60)
    if result and result.returncode == 0 and optimized.is_file() and optimized.stat().st_size > 0:
        optimized.replace(filename)
    elif optimized.exists():
        optimized.unlink()
    return filename


def locate_video_file(directory, prefix=""):
    valid = []
    for p in directory.glob(f"{prefix}*"):
        if not p.is_file() or p.name.endswith(".part"):
            continue
        video, audio = video_codecs(p)
        if video:
            valid.append((bool(audio), p.stat().st_size, p.stat().st_mtime, p))
    if not valid:
        return None
    valid.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return valid[0][3]


def locate_audio_file(directory):
    valid = []
    for p in directory.glob("*"):
        if not p.is_file() or p.name.endswith(".part"):
            continue
        _, audio = video_codecs(p)
        if audio:
            valid.append((p.stat().st_size, p.stat().st_mtime, p))
    if not valid:
        return None
    valid.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return valid[0][2]


def mux_video_audio(video_path, audio_path, output_path):
    result = run_ffmpeg(["-threads", "1", "-i", str(video_path), "-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(output_path)], timeout=120)
    if result and result.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0:
        return output_path
    output_path.unlink(missing_ok=True)
    return None


def download_direct_file(media_url, filename, chat_id, accept="*/*", max_size=None):
    req = Request(media_url, headers={"User-Agent": "Mozilla/5.0", "Accept": accept})
    try:
        with urlopen(req, timeout=60) as response, filename.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            if max_size and total > max_size:
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
                set_progress(chat_id, {"stage": "download", "percent": downloaded / total * 100 if total else 0})
    except Exception:
        filename.unlink(missing_ok=True)
        raise
    if not filename.is_file() or filename.stat().st_size == 0:
        raise RuntimeError("Downloader returned an empty file")
    return filename


def tikwm_payload(url):
    api_url = f"{TIKWM_API}?url={quote(url, safe='')}&hd=1"
    req = Request(api_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*", "Referer": "https://www.tikwm.com/"})
    with urlopen(req, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != 0:
        raise RuntimeError(f"TikWM error: {payload.get('msg') or 'unknown error'}")
    return payload.get("data") or {}


def resolve_redirect_url(url):
    if get_host(url) not in ("vt.tiktok.com", "vm.tiktok.com", "m.tiktok.com", "www.tiktok.com"):
        return url
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"})
        with urlopen(req, timeout=12) as response:
            final_url = response.geturl()
        print(f"TikTok redirect: {url} -> {final_url}")
        return final_url or url
    except Exception as exc:
        print(f"TikTok redirect resolve failed: {exc!r}")
        return url


def download_tiktok_photo_post(url, chat_id):
    data = tikwm_payload(clean_tiktok_photo_url(url))
    images = data.get("images") or []
    if not images:
        raise RuntimeError("TikTok Photo Mode returned no images")
    post_id = str(data.get("id") or uuid4().hex)
    paths = []
    try:
        for index, image_url in enumerate(images, 1):
            filename = DOWNLOAD_DIR / f"{post_id}_{index:02d}.jpg"
            download_direct_file(image_url, filename, chat_id, "image/avif,image/webp,image/jpeg,image/*,*/*;q=0.8", MAX_TELEGRAM_PHOTO_SIZE)
            paths.append(filename)
        return paths, data.get("title") or "TikTok Photo"
    except Exception:
        for p in paths:
            p.unlink(missing_ok=True)
        raise


def download_from_tikwm(url, chat_id):
    data = tikwm_payload(url)
    if data.get("images"):
        raise RuntimeError("TikWM returned a photo post")
    media_url = data.get("hdplay") or data.get("play")
    if not media_url:
        raise RuntimeError("TikWM returned no video URL")
    filename = DOWNLOAD_DIR / f"tiktok_{uuid4().hex}.mp4"
    download_direct_file(media_url, filename, chat_id, "video/mp4,video/*,*/*;q=0.8", MAX_TELEGRAM_SIZE)
    return filename, data.get("title") or "TikTok Video"


def download_from_ttdown(url, chat_id):
    api_url = f"{TIKTOK_API}?down={quote(url, safe='')}"
    req = Request(api_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"})
    with urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    media_url = payload.get("download_url")
    if not media_url:
        raise RuntimeError(payload.get("message") or payload.get("error") or "Second TikTok API returned no video URL")
    filename = DOWNLOAD_DIR / f"tiktok_{uuid4().hex}.mp4"
    download_direct_file(media_url, filename, chat_id, "video/mp4,video/*,*/*;q=0.8", MAX_TELEGRAM_SIZE)
    return filename, payload.get("title") or "TikTok Video"


INSTAGRAM_APP_IDS = ("web", "ios")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")


def instagram_cookiefile(job_dir):
    cookie_text = os.getenv("INSTAGRAM_COOKIES", "").strip()
    if not cookie_text:
        return None
    cookiefile = job_dir / "instagram_cookies.txt"
    if not cookiefile.exists():
        cookiefile.write_text(cookie_text.replace("\r\n", "\n") + "\n", encoding="utf-8")
        print("Instagram cookies configured")
    return cookiefile


def find_key(obj, key):
    """Return the first non-empty value for key anywhere inside nested dicts/lists."""
    if isinstance(obj, dict):
        if obj.get(key):
            return obj[key]
        values = obj.values()
    elif isinstance(obj, list):
        values = obj
    else:
        return None
    for value in values:
        found = find_key(value, key)
        if found:
            return found
    return None


def best_url(candidates):
    items = [c for c in (candidates or []) if isinstance(c, dict) and c.get("url")]
    if not items:
        return None
    items.sort(key=lambda c: (c.get("width") or 0) * (c.get("height") or 0), reverse=True)
    return items[0]["url"]


def instagram_raw_media(url, job_dir, app_id):
    """Fetch Instagram's raw post JSON through yt-dlp (handles headers/tokens), without downloading."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
        "socket_timeout": 30,
        "extractor_args": {"instagram": {"app_id": [app_id]}},
    }
    cookiefile = instagram_cookiefile(job_dir)
    if cookiefile:
        options["cookiefile"] = str(cookiefile)
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False, process=False)
    raw = (info or {}).get("_mohd_raw") or {}
    if not raw:
        raise RuntimeError("Instagram returned no post data")
    return raw, info


def describe_formats(info):
    rows = []
    for f in (info or {}).get("formats") or []:
        rows.append(f"{f.get('format_id')}:{f.get('vcodec')}/{f.get('acodec')}:{f.get('height')}")
    return ", ".join(rows) or "none"


def instagram_music(raw):
    """Return (audio_url, start_seconds, duration_seconds) for a post's attached music, if any."""
    holders = [raw.get(k) for k in ("music_metadata", "clips_metadata", "music_info") if raw.get(k)] + [raw]
    for holder in holders:
        audio_url = find_key(holder, "progressive_download_url") or find_key(holder, "fast_start_progressive_download_url")
        if audio_url:
            start_ms = find_key(holder, "audio_asset_start_time_in_ms") or 0
            length_ms = find_key(holder, "overlap_duration_in_ms") or find_key(holder, "duration_in_ms") or 0
            try:
                start, length = float(start_ms) / 1000, float(length_ms) / 1000
            except (TypeError, ValueError):
                start, length = 0.0, 0.0
            return audio_url, max(start, 0.0), length
    return None


def media_duration(filename):
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(filename)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15, check=False)
        return float(result.stdout.strip() or 0)
    except Exception:
        return 0.0


def render_photos_with_audio(images, audio_file, start, length, output):
    """Turn one or more photos plus a music clip into an H.264 MP4 (slideshow if several photos)."""
    available = max(media_duration(audio_file) - start, 0)
    if available <= 0:
        start, available = 0.0, media_duration(audio_file)
    duration = length if 0 < length <= available else min(available or 15.0, 30.0)
    duration = min(max(duration, 3.0), 60.0)
    count = len(images)
    per_image = max(duration / count, 2.0)
    total = per_image * count
    args = ["-threads", "2"]
    for image in images:
        args += ["-loop", "1", "-framerate", "15", "-t", f"{per_image:.2f}", "-i", str(image)]
    args += ["-ss", f"{start:.2f}", "-i", str(audio_file)]
    if count == 1:
        graph = "[0:v]scale='min(1080,iw)':-2,setsar=1,format=yuv420p[v]"
    else:
        graph = "".join(
            f"[{i}:v]scale=1080:1350:force_original_aspect_ratio=decrease,pad=1080:1350:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[v{i}];"
            for i in range(count)
        ) + "".join(f"[v{i}]" for i in range(count)) + f"concat=n={count}:v=1:a=0[v]"
    args += ["-filter_complex", graph, "-map", "[v]", "-map", f"{count}:a:0", "-af", "apad",
             "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
             "-crf", "23", "-r", "15", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output)]
    result = run_ffmpeg(args, timeout=300)
    if result and result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
        return output
    output.unlink(missing_ok=True)
    return None


def download_instagram_photos(raw, chat_id, job_dir):
    """Photos, carousels (photos + videos), and photos with attached music -> dict result."""
    items = raw.get("carousel_media") or [raw]
    photos, videos = [], []
    for index, item in enumerate(items, 1):
        set_progress(chat_id, {"stage": "download", "percent": (index - 1) / len(items) * 100})
        video_url = best_url(item.get("video_versions"))
        if video_url:
            path = job_dir / f"item_{index:02d}.mp4"
            download_direct_file(video_url, path, chat_id, "video/mp4,video/*,*/*;q=0.8", MAX_TELEGRAM_SIZE)
            path = faststart_mp4(ensure_h264(path, job_dir))
            stable = DOWNLOAD_DIR / f"result_{uuid4().hex}.mp4"
            path.replace(stable)
            videos.append(stable)
            continue
        image_url = best_url(find_key(item, "candidates")) or item.get("display_url")
        if image_url:
            path = DOWNLOAD_DIR / f"photo_{uuid4().hex}.jpg"
            download_direct_file(image_url, path, chat_id, "image/jpeg,image/*,*/*;q=0.8", MAX_TELEGRAM_PHOTO_SIZE)
            photos.append(path)
    if not photos and not videos:
        raise RuntimeError("Instagram post has no downloadable media")

    music_videos = []
    music = instagram_music(raw)
    print(f"Instagram post: photos={len(photos)} videos={len(videos)} music={'yes' if music else 'no'}")
    if music and photos:
        audio_url, start, length = music
        audio_file = job_dir / "music.m4a"
        try:
            download_direct_file(audio_url, audio_file, chat_id, "audio/*,*/*;q=0.8", MAX_TELEGRAM_SIZE)
            print("Rendering photo(s) with music into a video")
            rendered = render_photos_with_audio(photos, audio_file, start, length, job_dir / "photo_music.mp4")
            if rendered and rendered.stat().st_size <= MAX_TELEGRAM_SIZE:
                stable = DOWNLOAD_DIR / f"result_{uuid4().hex}.mp4"
                rendered.replace(stable)
                music_videos.append(stable)
        except Exception as exc:
            print(f"Instagram music render failed: {exc!r}")
    title = (raw.get("caption") or {}).get("text") if isinstance(raw.get("caption"), dict) else None
    username = (raw.get("user") or {}).get("username") if isinstance(raw.get("user"), dict) else None
    # A single photo with music is sent as the video only; carousels also get the original photos.
    if music_videos and len(photos) == 1 and not videos:
        for p in photos:
            p.unlink(missing_ok=True)
        photos = []
    return {"videos": music_videos + videos, "photos": photos, "title": title or (f"Instagram @{username}" if username else "Instagram")}


def download_instagram_video(url, chat_id, job_dir, app_id):
    def hook(data):
        if data.get("status") == "downloading":
            done = data.get("downloaded_bytes", 0) or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            set_progress(chat_id, {"stage": "download", "percent": done / total * 100 if total else 0})

    video_dir = job_dir / f"video_{app_id}"
    video_dir.mkdir()
    options = {
        "outtmpl": str(video_dir / "video.%(id)s.%(ext)s"),
        "format": "bv*[vcodec^=avc1]+ba/bv*+ba/b[ext=mp4]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "progress_hooks": [hook],
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_args": {"instagram": {"app_id": [app_id]}},
    }
    cookiefile = instagram_cookiefile(job_dir)
    if cookiefile:
        options["cookiefile"] = str(cookiefile)
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
    print(f"Instagram formats ({app_id}): {describe_formats(info)}")
    filename = locate_video_file(video_dir, "video.")
    if not filename:
        raise RuntimeError("Instagram video was not downloaded")
    print(f"Instagram file ({app_id}): {filename.name}; streams={video_codecs(filename)}")
    return filename, info


def download_instagram(url, chat_id, job_dir):
    path = urlparse(url).path.lower()
    if "/p/" in path:
        # Posts can be photos, carousels or videos: inspect the post first.
        raw = None
        for app_id in INSTAGRAM_APP_IDS:
            try:
                raw, _ = instagram_raw_media(url, job_dir, app_id)
                break
            except Exception as exc:
                print(f"Instagram post data ({app_id}) failed: {exc!r}")
        if raw is None:
            raise RuntimeError("Instagram post data could not be read")
        print(f"Instagram post media_type={raw.get('media_type')} carousel={bool(raw.get('carousel_media'))} keys={sorted(raw.keys())[:60]}")
        if raw.get("carousel_media") or not raw.get("video_versions"):
            return download_instagram_photos(raw, chat_id, job_dir)

    silent_file, info, last_error = None, None, None
    for app_id in INSTAGRAM_APP_IDS:
        try:
            filename, info = download_instagram_video(url, chat_id, job_dir, app_id)
        except Exception as exc:
            last_error = exc
            print(f"Instagram yt-dlp ({app_id}) failed: {exc!r}")
            continue
        if video_codecs(filename)[1]:
            break
        silent_file = silent_file or filename
        print(f"Instagram video from {app_id} has no audio; trying next source")
    else:
        filename = None

    if not filename and silent_file:
        # Last resort: some reels carry their soundtrack as attached music in the post data.
        try:
            raw, _ = instagram_raw_media(url, job_dir, INSTAGRAM_APP_IDS[0])
            music = instagram_music(raw)
            print(f"Instagram silent video; attached music={'yes' if music else 'no'}")
            if music:
                audio_file = job_dir / "music.m4a"
                download_direct_file(music[0], audio_file, chat_id, "audio/*,*/*;q=0.8", MAX_TELEGRAM_SIZE)
                filename = mux_video_audio(silent_file, audio_file, job_dir / "instagram_with_audio.mp4")
        except Exception as exc:
            print(f"Instagram music fallback failed: {exc!r}")
        if not filename:
            # Video genuinely has no sound: send it rather than failing.
            print("Sending Instagram video without audio")
            filename = silent_file
    if not filename:
        raise RuntimeError(str(last_error) if last_error else "Instagram video was not downloaded")

    filename = faststart_mp4(ensure_h264(filename, job_dir))
    if filename.stat().st_size > MAX_TELEGRAM_SIZE:
        raise RuntimeError("حجم الفيديو أكبر من حد Telegram المسموح للبوت (50 MB).")
    stable = DOWNLOAD_DIR / f"result_{uuid4().hex}.mp4"
    filename.replace(stable)
    return stable, (info or {}).get("title") or "Instagram Reel"


def download_video(url, chat_id):
    if is_instagram_url(url):
        job_dir = DOWNLOAD_DIR / f".job_{uuid4().hex}"
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            return download_instagram(url, chat_id, job_dir)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def hook(data):
        if data.get("status") == "downloading":
            done = data.get("downloaded_bytes", 0) or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            set_progress(chat_id, {"stage": "download", "percent": done / total * 100 if total else 0})

    job_dir = DOWNLOAD_DIR / f".job_{uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        options = {
            "outtmpl": str(job_dir / "media.%(id)s.%(ext)s"),
            "format": "b[ext=mp4]/b/bv*+ba",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "progress_hooks": [hook],
            "socket_timeout": 30,
            "retries": 2,
            "fragment_retries": 2,
        }
        if is_tiktok_url(url):
            options["extractor_args"] = {"tiktok": {"app_info": ["7355728856979392260", "7351149742343391009", "7351144126450059040"]}}
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
        filename = locate_video_file(job_dir, "media.")
        if not filename:
            raise RuntimeError("Downloaded video file not found")
        if filename.stat().st_size > MAX_TELEGRAM_SIZE:
            raise RuntimeError("حجم الفيديو أكبر من حد Telegram المسموح للبوت (50 MB).")
        stable = DOWNLOAD_DIR / f"result_{uuid4().hex}.mp4"
        filename.replace(stable)
        return stable, info.get("title") or "Video"
    except Exception as primary_error:
        if not is_tiktok_url(url):
            raise
        print(f"TikTok yt-dlp failed, trying TikWM: {primary_error!r}")
        try:
            return download_from_tikwm(url, chat_id)
        except Exception as tikwm_error:
            print(f"TikWM failed, trying second TikTok API: {tikwm_error!r}")
            return download_from_ttdown(url, chat_id)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def update_status_message(message, stop_event, chat_id):
    last_text = "⏳ <b>جاري معالجة الرابط...</b>"
    while not stop_event.is_set():
        await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)
        if stop_event.is_set():
            break
        with progress_lock:
            data = dict(progress_data.get(chat_id) or {})
        percent = data.get("percent") or 0
        text = f"⬇️ <b>جاري التحميل... {percent:.0f}%</b>" if percent else last_text
        if text == last_text:
            continue
        try:
            await message.edit_text(text, parse_mode="HTML")
            last_text = text
        except Exception:
            pass


async def delete_quietly(message):
    try:
        await message.delete()
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎬 <b>MOHD MEDIA BOT</b>\n\nأرسل الرابط وسأتولى الباقي.\n\n✅ Instagram: Reels، صور، ألبومات، وصور مع موسيقى (تتحول لفيديو)\n✅ TikTok\n✅ X / Twitter\n\n🚀 تحميل بأفضل جودة متاحة\n⚡ بدون ضغط تلقائي\n\n🔗 أرسل الرابط الآن.", parse_mode="HTML")


async def send_video_file(message, filename, title):
    if filename.stat().st_size > MAX_TELEGRAM_SIZE:
        raise RuntimeError("❌ حجم الفيديو أكبر من الحد المسموح في Telegram (50 MB).")
    dimensions = video_dimensions(filename)
    width, height = dimensions if dimensions else (None, None)
    with filename.open("rb") as video:
        await message.reply_video(video=video, caption=f"🎬 {title[:900]}", width=width, height=height, supports_streaming=True, write_timeout=UPLOAD_TIMEOUT, read_timeout=UPLOAD_TIMEOUT)


async def send_instagram_bundle(message, result):
    """Send an Instagram post: videos (incl. photo+music videos) first, then the photos as an album."""
    title = result.get("title") or "Instagram"
    for video_path in result.get("videos") or []:
        if video_path.is_file():
            await send_video_file(message, video_path, title)
    photos = [p for p in result.get("photos") or [] if p.is_file()]
    if photos:
        await send_photo_album(message, photos, title, label="🖼️ صورة من Instagram")


async def send_photo_album(message, paths, title, label="🖼️ صورة من TikTok Photo Mode"):
    for start in range(0, len(paths), 10):
        batch = paths[start:start + 10]
        photo_paths = [p for p in batch if p.is_file() and p.stat().st_size <= MAX_TELEGRAM_PHOTO_SIZE]
        large_paths = [p for p in batch if p.is_file() and p.stat().st_size > MAX_TELEGRAM_PHOTO_SIZE]
        if len(photo_paths) == 1:
            with photo_paths[0].open("rb") as photo:
                await message.reply_photo(photo=photo, caption=f"🖼️ {title[:900]}", write_timeout=UPLOAD_TIMEOUT, read_timeout=UPLOAD_TIMEOUT)
        elif photo_paths:
            handles, media = [], []
            try:
                for path in photo_paths:
                    handle = path.open("rb")
                    handles.append(handle)
                    media.append(InputMediaPhoto(media=handle))
                if start == 0:
                    media[0].caption = f"🖼️ {title[:900]}"
                await message.reply_media_group(media=media, write_timeout=UPLOAD_TIMEOUT, read_timeout=UPLOAD_TIMEOUT)
            finally:
                for handle in handles:
                    handle.close()
        for path in large_paths:
            with path.open("rb") as photo:
                await message.reply_document(document=photo, caption=label, write_timeout=UPLOAD_TIMEOUT, read_timeout=UPLOAD_TIMEOUT)


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
    if is_tiktok_url(url):
        expanded = await asyncio.to_thread(resolve_redirect_url, url)
        if expanded and is_supported_url(expanded):
            url = expanded

    chat_id = update.effective_chat.id
    status_message = await update.message.reply_text("⏳ <b>جاري معالجة الرابط...</b>", parse_mode="HTML")
    stop_event = threading.Event()
    progress_task = asyncio.create_task(update_status_message(status_message, stop_event, chat_id))
    result = None
    try:
        async with download_semaphore:
            photo_mode = is_tiktok_photo_url(url)
            print(f"Routing URL: host={get_host(url)} tiktok_photo={photo_mode} path={urlparse(url).path}")
            if photo_mode:
                result = await asyncio.to_thread(download_tiktok_photo_post, url, chat_id)
            else:
                result = await asyncio.to_thread(download_video, url, chat_id)
        stop_event.set()
        await progress_task
        if isinstance(result, dict):
            await send_instagram_bundle(update.message, result)
            await delete_quietly(status_message)
            return
        if isinstance(result[0], list):
            await send_photo_album(update.message, result[0], result[1])
            await delete_quietly(status_message)
            return
        filename, title = result
        if not filename or not filename.is_file():
            raise RuntimeError("Downloaded file is missing")
        await send_video_file(update.message, filename, title)
        await delete_quietly(status_message)
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
            await update.message.reply_text(user_message)
    finally:
        stop_event.set()
        if not progress_task.done():
            progress_task.cancel()
            try:
                await progress_task
            except BaseException:
                pass
        clear_progress(chat_id)
        if result:
            if isinstance(result, dict):
                paths = list(result.get("videos") or []) + list(result.get("photos") or [])
            else:
                paths = result[0] if isinstance(result[0], list) else [result[0]]
            for path in paths:
                path.unlink(missing_ok=True)


def create_application():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    application = Application.builder().token(TOKEN).concurrent_updates(True).build()
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
        # Local run (no public URL): drop any old webhook and use polling instead.
        bot_loop.run_until_complete(telegram_application.bot.delete_webhook())
        bot_loop.run_until_complete(telegram_application.updater.start_polling())
        print("MOHD Media Bot is running locally with polling")
    bot_loop.run_forever()


@app.get("/")
def health():
    return jsonify({"status": "ok", "service": "MOHD Media Bot"})


@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        return jsonify({"ok": False}), 403
    try:
        data = request.get_json(force=True, silent=False)
        update = Update.de_json(data, telegram_application.bot)
        asyncio.run_coroutine_threadsafe(telegram_application.process_update(update), bot_loop)
        return jsonify({"ok": True})
    except Exception as exc:
        print(f"Webhook error: {exc!r}")
        return jsonify({"ok": False}), 500


def keep_awake():
    """Ping our own public URL so Render's free plan doesn't put the bot to sleep."""
    import time
    while True:
        time.sleep(KEEP_AWAKE_INTERVAL)
        try:
            with urlopen(Request(f"{EXTERNAL_URL}/", headers={"User-Agent": "MOHD-keep-awake"}), timeout=30) as response:
                response.read()
        except Exception as exc:
            print(f"Keep-awake ping failed: {exc!r}")


if __name__ == "__main__":
    threading.Thread(target=bot_loop_runner, daemon=True).start()
    if EXTERNAL_URL and KEEP_AWAKE_INTERVAL > 0:
        threading.Thread(target=keep_awake, daemon=True).start()
        print(f"Keep-awake ping every {KEEP_AWAKE_INTERVAL}s")
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
