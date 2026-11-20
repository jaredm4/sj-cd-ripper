"""Animated cover art from Apple Music, saved as an animated WebP (cover.webp).

Same approach as github.com/m8tec/apple-music-animated-artworks:
  1. the album's Apple Music id comes from the iTunes search (see cover.py)
  2. a public token is read from the Apple Music web player's JavaScript
  3. the catalogue API (amp-api.music.apple.com) is asked for the album with
     extend=editorialVideo; editorialVideo.motionDetailSquare.video is an HLS (.m3u8) stream
  4. the stream is downloaded and converted with ffmpeg

This uses Apple's web player interface, which is not a documented public API: it can change
or stop working at any time. Every failure is reported and never stops the pipeline.
"""
from __future__ import annotations

import base64
import io
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from .config import log
from .util import NetworkError, NotFound, http_get

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_JWT = re.compile(r"[\"'](eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+)[\"']")
_JS = re.compile(r"(/assets/[^\"']+\.js)")
_token: tuple[str, float] | None = None          # (token, expiry) - reused for the whole run
_last_catalog = 0.0
stats = {"catalog": 0}                           # Apple Music catalogue lookups this run


class Unavailable(Exception):
    """No animated cover could be made; the message says why."""


@dataclass
class Animated:
    webp: bytes
    width: int
    height: int
    frames: int
    note: str


def _jwt_expiry(token: str) -> float:
    try:
        part = token.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["exp"])
    except (ValueError, KeyError, IndexError):
        return time.time() + 3600


def _web_token(storefront: str, album_id: str, refresh: bool = False) -> str:
    """The token the Apple Music web player itself uses, read from its JavaScript."""
    global _token
    if _token and not refresh and _token[1] > time.time() + 300:
        return _token[0]
    page = http_get(f"https://music.apple.com/{storefront}/album/{album_id}", BROWSER_UA).decode("utf-8", "replace")
    scripts = [p for p in dict.fromkeys(_JS.findall(page))
               if any(k in p for k in ("index", "web-client", "apple-music"))]
    for path in scripts:
        try:
            js = http_get("https://music.apple.com" + path, BROWSER_UA).decode("utf-8", "replace")
        except (NotFound, NetworkError):
            continue
        m = _JWT.search(js)
        if m:
            _token = (m.group(1), _jwt_expiry(m.group(1)))
            return _token[0]
    raise Unavailable(f"could not find the Apple Music web token ({len(scripts)} script(s) checked)")


def motion_stream(storefront: str, album_id: str, interval: float = 0.0) -> str | None:
    """URL of the album's square animated cover (HLS master playlist), or None if it has none.
    Lookups are spaced `interval` seconds apart (Apple does not publish a limit for this API)."""
    global _last_catalog
    url = (f"https://amp-api.music.apple.com/v1/catalog/{storefront}/albums/{album_id}"
           "?extend=editorialVideo&platform=web")
    for attempt in (0, 1):
        headers = {"Authorization": f"Bearer {_web_token(storefront, album_id, refresh=bool(attempt))}",
                   "Origin": "https://music.apple.com"}
        wait = interval - (time.monotonic() - _last_catalog)
        if wait > 0:
            time.sleep(wait)
        try:
            stats["catalog"] += 1
            data = json.loads(http_get(url, BROWSER_UA, headers=headers, retries=1))
            break
        except NetworkError as e:
            if attempt or "401" not in str(e):      # a stale token gets one refresh
                raise
        finally:
            _last_catalog = time.monotonic()
    attrs = ((data.get("data") or [{}])[0]).get("attributes") or {}
    return ((attrs.get("editorialVideo") or {}).get("motionDetailSquare") or {}).get("video")


def _attrs(line: str) -> dict[str, str]:
    return {k.upper(): v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(":", 1)[1])}


def pick_variant(master: str, base_url: str, max_size: int) -> tuple[str, int, int]:
    """The best stream in a master playlist: the largest that fits max_size (else the
    smallest), preferring SDR and H.264. Returns (url, width, height)."""
    lines = [l.strip() for l in master.splitlines() if l.strip()]
    found = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF") or i + 1 >= len(lines) or lines[i + 1].startswith("#"):
            continue
        a = _attrs(line)
        w, h = (int(x) for x in a.get("RESOLUTION", "0x0").lower().split("x"))
        found.append(dict(url=urljoin(base_url, lines[i + 1]), w=w, h=h, bw=int(a.get("BANDWIDTH", 0) or 0),
                          sdr=a.get("VIDEO-RANGE", "SDR").upper() == "SDR",
                          avc=a.get("CODECS", "").lower().startswith("avc")))
    if not found:
        return base_url, 0, 0                       # it was already a media playlist
    fits = [v for v in found if max(v["w"], v["h"]) <= max_size]
    pool = fits or [min(found, key=lambda v: v["w"] * v["h"])]
    best = max(pool, key=lambda v: (v["sdr"], v["w"] * v["h"], v["avc"], v["bw"]))
    return best["url"], best["w"], best["h"]


def download_stream(media_url: str, ua: str) -> bytes:
    """All of a media playlist's data (init section + segments) as one file ffmpeg can read."""
    text = http_get(media_url, ua).decode("utf-8", "replace")
    parts = []
    for line in (l.strip() for l in text.splitlines()):
        if line.startswith("#EXT-X-KEY") and _attrs(line).get("METHOD", "NONE").upper() != "NONE":
            raise Unavailable("the animated cover stream is encrypted")
        if line.startswith("#EXT-X-MAP"):
            parts.append(urljoin(media_url, _attrs(line)["URI"]))
        elif line and not line.startswith("#"):
            parts.append(urljoin(media_url, line))
    parts = list(dict.fromkeys(parts))              # byte-range playlists name one file many times
    if not parts:
        raise Unavailable("the animated cover stream has no segments")
    return b"".join(http_get(p, ua, timeout=60) for p in parts)


def webp_encoder(ffmpeg: str) -> str | None:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                             timeout=30, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return next((e for e in ("libwebp_anim", "libwebp") if re.search(rf"\s{e}\s", out)), None)


def to_webp(ffmpeg: str, encoder: str, video: bytes, max_size: int, quality: int, fps: int) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / ("in.ts" if video[:1] == b"\x47" else "in.mp4")
        dst = Path(tmp) / "out.webp"
        src.write_bytes(video)
        vf = f"scale='min(iw,{max_size})':-1:flags=lanczos" + (f",fps={fps}" if fps else "")
        r = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(src), "-an",
                            "-vf", vf, "-c:v", encoder, "-loop", "0", "-q:v", str(quality), str(dst)],
                           capture_output=True, text=True, errors="replace", timeout=900)
        if r.returncode != 0 or not dst.is_file():
            raise Unavailable(f"ffmpeg could not convert the stream: {(r.stderr or '').strip()[-300:]}")
        return dst.read_bytes()


FPS_STEPS = (24, 15, 12)        # frames per second to step down through
QUALITY_STEP, QUALITY_FLOOR = 15, 30
SIZE_STEPS = (700, 600)         # px


def shrink_steps(max_size: int, quality: int, fps: int) -> list[tuple[int, int, int]]:
    """Encoder settings (size, quality, fps) to try in turn until the file fits the size limit.
    Starting from the configured ones, it takes one step down in frame rate, then one in
    quality, then one in size, and round again, each step keeping the earlier ones
    (fps 24 -> 15 -> 12, quality -15 each time down to 30, size 700 -> 600 px). An axis with no
    steps left is skipped."""
    fps_left = [f for f in FPS_STEPS if not fps or f < fps]
    q_left = list(range(quality - QUALITY_STEP, QUALITY_FLOOR - 1, -QUALITY_STEP))
    if quality > QUALITY_FLOOR and (not q_left or q_left[-1] > QUALITY_FLOOR):
        q_left.append(QUALITY_FLOOR)
    size_left = [n for n in SIZE_STEPS if n < max_size]
    size, q, f = max_size, quality, fps
    steps = [(size, q, f)]
    while fps_left or q_left or size_left:
        for left, axis in ((fps_left, "f"), (q_left, "q"), (size_left, "s")):
            if left:
                v = left.pop(0)
                size, q, f = (v, q, f) if axis == "s" else (size, v, f) if axis == "q" else (size, q, v)
                steps.append((size, q, f))
    return steps


def to_webp_within(ffmpeg: str, encoder: str, video: bytes, max_size: int, quality: int, fps: int,
                   max_bytes: int) -> tuple[bytes, str]:
    """The WebP and a note on what had to give to fit max_bytes (0 = no limit). If nothing fits,
    the smallest attempt is kept and the note says so."""
    best = None
    for n, (size, q, f) in enumerate(shrink_steps(max_size, quality, fps) if max_bytes else [(max_size, quality, fps)]):
        webp = to_webp(ffmpeg, encoder, video, size, q, f)
        if best is None or len(webp) < len(best[0]):
            best = (webp, size, q, f)
        if not max_bytes or len(webp) <= max_bytes:
            break
        log.info("animated cover: %.1f MB is over the %.1f MB limit, encoding again smaller",
                 len(webp) / 1e6, max_bytes / 1e6)
    webp, size, q, f = best
    if (size, q, f) == (max_size, quality, fps):
        return webp, ""
    how = ", ".join(x for x in ((f"{f} fps" if f != fps else ""), (f"quality {q}" if q != quality else ""),
                                (f"at most {size}px" if size != max_size else "")) if x)
    fits = "to fit" if len(webp) <= max_bytes else "but still over"
    return webp, f"; reduced {fits} the {max_bytes / 1e6:.3g} MB limit ({how})"


def animated_cover(cfg: dict, album_id: str) -> Animated | None:
    """Animated square cover for an Apple Music album id; None if the album has none.
    Raises Unavailable (or NetworkError) when one may exist but could not be made."""
    c = cfg["cover"]
    ffmpeg = shutil.which(cfg["tools"]["ffmpeg"])
    if not ffmpeg:
        raise Unavailable("ffmpeg not found - needed to make the animated cover (set [tools] ffmpeg)")
    encoder = webp_encoder(ffmpeg)
    if not encoder:
        raise Unavailable("this ffmpeg build has no WebP encoder (libwebp)")
    storefront = c["itunes_country"].lower()
    master_url = motion_stream(storefront, album_id, c["apple_music_interval"])
    if not master_url:
        return None
    master = http_get(master_url, BROWSER_UA).decode("utf-8", "replace")
    media_url, w, h = pick_variant(master, master_url, c["animated_max_size"])
    video = download_stream(media_url, BROWSER_UA)
    webp, shrunk = to_webp_within(ffmpeg, encoder, video, c["animated_max_size"], c["animated_quality"],
                                  c["animated_fps"], int(float(c["animated_max_mb"] or 0) * 1e6))
    from PIL import Image
    with Image.open(io.BytesIO(webp)) as im:
        width, height, frames = im.width, im.height, getattr(im, "n_frames", 1)
    note = (f"Apple Music animated cover: {width}x{height}, {frames} frames, {len(webp) / 1e6:.1f} MB"
            + (f" (from a {w}x{h} stream)" if w else "") + shrunk)
    log.info("animated cover: %dx%d, %d frames, %.1f MB", width, height, frames, len(webp) / 1e6)
    return Animated(webp, width, height, frames, note)
