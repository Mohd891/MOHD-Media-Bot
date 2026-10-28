"""Runtime compatibility tweaks for yt-dlp on hosted environments."""

# Instagram can intermittently return an empty media response through either
# the web or iOS endpoint. Try iOS first, then automatically retry through the
# web endpoint before giving up.
try:
    import yt_dlp

    _original_ytdlp_init = yt_dlp.YoutubeDL.__init__
    _original_extract_info = yt_dlp.YoutubeDL.extract_info

    def _patched_ytdlp_init(self, params=None, *args, **kwargs):
        params = dict(params or {})
        extractor_args = dict(params.get("extractor_args") or {})
        instagram_args = dict(extractor_args.get("instagram") or {})
        instagram_args.setdefault("app_id", ["ios"])
        extractor_args["instagram"] = instagram_args
        params["extractor_args"] = extractor_args
        return _original_ytdlp_init(self, params, *args, **kwargs)

    def _patched_extract_info(self, url, *args, **kwargs):
        try:
            return _original_extract_info(self, url, *args, **kwargs)
        except Exception as first_error:
            try:
                host = (yt_dlp.utils.urlparse(url).hostname or "").lower()
            except Exception:
                host = ""
            if host != "instagram.com" and not host.endswith(".instagram.com"):
                raise

            params = getattr(self, "params", {})
            extractor_args = dict(params.get("extractor_args") or {})
            instagram_args = dict(extractor_args.get("instagram") or {})
            current_app = instagram_args.get("app_id")
            if current_app == ["web"]:
                raise

            print(f"Instagram first extraction failed, retrying with web app ID: {first_error!r}")
            instagram_args["app_id"] = ["web"]
            extractor_args["instagram"] = instagram_args
            params["extractor_args"] = extractor_args
            self.params = params
            return _original_extract_info(self, url, *args, **kwargs)

    yt_dlp.YoutubeDL.__init__ = _patched_ytdlp_init
    yt_dlp.YoutubeDL.extract_info = _patched_extract_info
    print("Instagram yt-dlp compatibility: iOS first + web fallback enabled")
except Exception as exc:
    print(f"Instagram yt-dlp compatibility patch failed: {exc!r}")
