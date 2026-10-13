"""Decide whether a video link may be added to the Movie Channel.

is_allowed(url) returns True only when the video is clearly free to download and
rebroadcast; otherwise (or if it can't be verified) it returns False:

  - Internet Archive: the item is marked public domain or Creative Commons
  - YouTube and other sites yt-dlp supports: the uploader marked it Creative Commons

The reason for each decision is logged to stderr.

CLI:  python license_check.py URL [URL ...]   prints true/false per URL
"""
import json
import sys
import urllib.request
from urllib.parse import urlparse

# licenseurl values on archive.org items that permit reuse
# (publicdomain covers both the Public Domain Mark and CC0)
ARCHIVE_ALLOWED = ("creativecommons.org/publicdomain/", "creativecommons.org/licenses/")


def is_allowed(url: str) -> bool:
    allowed, reason = check(url)
    print(f"{'ALLOW' if allowed else 'DENY '} {url} - {reason}", file=sys.stderr)
    return allowed


def check(url: str) -> tuple[bool, str]:
    """Like is_allowed, but returns (allowed, reason) and logs nothing."""
    try:
        return _check(url)
    except Exception as e:  # network errors, unsupported site, private/removed video...
        return False, f"could not verify ({type(e).__name__}: {e})"


def _check(url):
    host = (urlparse(url).hostname or "").lower()
    if host == "archive.org" or host.endswith(".archive.org"):
        return _check_archive(url)
    return _check_ytdlp(url)


def _check_archive(url):
    # https://archive.org/details/<id>[/...] or https://archive.org/download/<id>/<file>
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2 or parts[0] not in ("details", "download"):
        return False, "not an archive.org item link"
    item = parts[1]
    meta = _archive_metadata(item)
    if not meta:
        return False, f"archive.org item '{item}' not found"
    lic = meta.get("licenseurl") or ""
    if isinstance(lic, list):
        lic = " ".join(lic)
    title = meta.get("title", item)
    if any(marker in lic for marker in ARCHIVE_ALLOWED):
        return True, f"{title}: {lic}"
    return False, f"{title}: no public-domain or Creative Commons license on the item"


def _archive_metadata(item, attempts=3):
    # archive.org's metadata API is often slow; retry before giving up
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(f"https://archive.org/metadata/{item}", timeout=60) as r:
                return json.load(r).get("metadata") or {}
        except (TimeoutError, OSError):
            if attempt == attempts - 1:
                raise


class _SilentLogger:
    # errors still surface as exceptions; this just stops yt-dlp printing them too
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _check_ytdlp(url):
    from yt_dlp import YoutubeDL

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "logger": _SilentLogger()}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False, process=False)
    if info.get("_type") in ("playlist", "multi_video"):
        return False, "playlists aren't checked; send single video links"
    if info.get("is_live"):
        return False, "live streams can't be added"
    title = info.get("title") or url
    lic = info.get("license") or ""
    if "creative commons" in lic.lower():
        return True, f"{title}: {lic}"
    return False, f"{title}: {lic or 'standard license'} (no reuse rights)"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python license_check.py URL [URL ...]")
    results = [is_allowed(u) for u in sys.argv[1:]]
    for u, ok in zip(sys.argv[1:], results):
        print(f"{str(ok).lower()}\t{u}")
    sys.exit(0 if all(results) else 1)
