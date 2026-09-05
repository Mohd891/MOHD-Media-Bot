"""Runtime compatibility tweaks for yt-dlp on hosted environments."""

# Instagram can intermittently return an empty media response through the web
# app endpoint. yt-dlp supports Instagram's iOS app ID as an alternative API
# path, so use it automatically for Instagram requests in this bot.
try:
    import yt_dlp

    _original_ytdlp_init = yt_dlp.YoutubeDL.__init__

    def _patched_ytdlp_init(self, params=None, *args, **kwargs):
        params = dict(params or {})
        extractor_args = dict(params.get("extractor_args") or {})
        instagram_args = dict(extractor_args.get("instagram") or {})
        instagram_args.setdefault("app_id", ["ios"])
        extractor_args["instagram"] = instagram_args
        params["extractor_args"] = extractor_args
        return _original_ytdlp_init(self, params, *args, **kwargs)

    yt_dlp.YoutubeDL.__init__ = _patched_ytdlp_init
    print("Instagram yt-dlp compatibility: iOS app ID enabled")
except Exception as exc:
    print(f"Instagram yt-dlp compatibility patch failed: {exc!r}")
