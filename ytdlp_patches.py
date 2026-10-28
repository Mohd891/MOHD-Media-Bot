"""Runtime tweaks for yt-dlp's Instagram extractor, imported by bot.py."""

try:
    import yt_dlp
    from yt_dlp.extractor import instagram as _instagram

    _original_ytdlp_init = yt_dlp.YoutubeDL.__init__
    _original_extract_product = _instagram.InstagramBaseIE._extract_product

    def _patched_ytdlp_init(self, params=None, *args, **kwargs):
        # Default to the web app ID (bot.py passes an explicit app_id when it needs another).
        params = dict(params or {})
        extractor_args = dict(params.get("extractor_args") or {})
        instagram_args = dict(extractor_args.get("instagram") or {})
        instagram_args.setdefault("app_id", ["web"])
        extractor_args["instagram"] = instagram_args
        params["extractor_args"] = extractor_args
        return _original_ytdlp_init(self, params, *args, **kwargs)

    def _patched_extract_product(self, product_info, *args, **kwargs):
        # Keep Instagram's raw post JSON so the bot can read photos, carousels and attached music.
        result = _original_extract_product(self, product_info, *args, **kwargs)
        raw = product_info[0] if isinstance(product_info, list) and product_info else product_info
        if isinstance(result, dict) and isinstance(raw, dict):
            result["_mohd_raw"] = raw
        return result

    yt_dlp.YoutubeDL.__init__ = _patched_ytdlp_init
    _instagram.InstagramBaseIE._extract_product = _patched_extract_product
    print("Instagram yt-dlp patches enabled (web app ID default, raw post data)")
except Exception as exc:
    print(f"Instagram yt-dlp patch failed: {exc!r}")
