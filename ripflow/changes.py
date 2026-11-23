"""The before/after confirmation `process --ask` (and `review`) shows before changing an album.

Rows are (what, before, after). Building them changes nothing; the caller decides what to do
with the answer: y = go ahead, n = wrong release (pick another), s = skip the album.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

NONE = "(none)"
REMOVED = "(removed)"
# Tags that differ per track and are worth seeing one by one; other per-track differences
# (MusicBrainz ids, ISRCs...) are summed up in one row.
EACH_TRACK = ("TITLE", "ARTIST", "TRACKNUMBER", "DISCNUMBER")
# Shown first, in this order, when they change; everything else follows alphabetically.
ORDER = ("ALBUM", "ALBUMARTIST", "ARTIST", "TITLE", "DATE", "ORIGINALDATE", "ORIGINALYEAR", "RELEASECOUNTRY",
         "RELEASETYPE", "RELEASESTATUS", "MEDIA", "LABEL", "CATALOGNUMBER", "BARCODE", "GENRE",
         "TRACKNUMBER", "TRACKTOTAL", "TOTALTRACKS", "DISCNUMBER", "DISCTOTAL", "TOTALDISCS")
# Written again after you confirm, by measuring: shown as their own rows, not as tag changes.
MEASURED = ("DYNAMIC RANGE", "ALBUM DYNAMIC RANGE")


def _measured(key: str) -> bool:
    return key in MEASURED or key.startswith("REPLAYGAIN_")


def _join(values: list[str] | None) -> str:
    return "; ".join(values or [])


def tag_rows(old: list[dict[str, list[str]]], new: list[dict[str, list[str]] | None]) -> list[tuple[str, str, str]]:
    """Rows for the tag changes across an album's tracks (new[i] None = tags left alone)."""
    new = [o if n is None else n for o, n in zip(old, new)]
    keys = {k for tags in old + new for k in tags if not _measured(k)}
    rank = {k: i for i, k in enumerate(ORDER)}
    rows, per_track = [], []
    for key in sorted(keys, key=lambda k: (rank.get(k, len(ORDER)), k)):
        pairs = [(_join(o.get(key)), _join(n.get(key))) for o, n in zip(old, new)]
        changed = [(i, a, b) for i, (a, b) in enumerate(pairs, 1) if a != b]
        if not changed:
            continue
        if len(set(pairs)) == 1:                       # the same change on every track
            a, b = pairs[0]
            rows.append((key, a or NONE, b or REMOVED))
        elif key in EACH_TRACK:
            per_track += [(f"{key} #{i}", a or NONE, b or REMOVED) for i, a, b in changed]
        else:
            added = sum(1 for _, a, _b in changed if not a)
            dropped = sum(1 for _, _a, b in changed if not b)
            how = ("added" if added == len(changed) else "removed" if dropped == len(changed) else "changed")
            rows.append((key, "differs per track" if any(a for _, a, _b in changed) else NONE,
                         f"{how} on {len(changed)} of {len(pairs)} tracks"))
    # TITLE etc. per track go together, in track order
    per_track.sort(key=lambda r: (int(r[0].rsplit("#", 1)[1]), r[0]))
    return rows + per_track


def picture_size(data: bytes | None) -> str:
    if not data:
        return NONE
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return f"{im.width}x{im.height}"
    except Exception:
        return "unreadable image"


def render(rows: list[tuple[str, str, str]], width: int | None = None) -> str:
    """A plain ASCII table: what | before | after. Long values are shortened in the middle."""
    width = width or shutil.get_terminal_size((120, 40)).columns
    w0 = min(max([len("What")] + [len(r[0]) for r in rows]), 28)
    room = max(width - w0 - 10, 30)
    w1 = min(max([len("Before")] + [len(r[1]) for r in rows]), room // 2)
    w2 = min(max([len("After")] + [len(r[2]) for r in rows]), room - w1)

    def cut(text: str, w: int) -> str:
        text = " ".join(str(text).split())
        return text if len(text) <= w else text[: (w - 3) // 2] + "..." + text[len(text) - (w - 3 - (w - 3) // 2):]
    line = f"+-{'-' * w0}-+-{'-' * w1}-+-{'-' * w2}-+"
    out = [line, f"| {'What':<{w0}} | {'Before':<{w1}} | {'After':<{w2}} |", line]
    for a, b, c in rows:
        out.append(f"| {cut(a, w0):<{w0}} | {cut(b, w1):<{w1}} | {cut(c, w2):<{w2}} |")
    out.append(line)
    return "\n".join(out)


def ask(view=None) -> str:
    """y, n or s. Anything else (or just Enter) asks again; case does not matter. With `view`
    (a function), v calls it - shows the cover art before/after - and asks again."""
    prompt = ("Apply these changes? y = yes, n = no (pick another release), s = skip album"
              + (", v = view cover art" if view else "") + ": ")
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            return "s"                      # input closed: change nothing
        if view and answer in ("v", "view"):
            view()
            continue
        if answer in ("y", "yes"):
            return "y"
        if answer in ("n", "no"):
            return "n"
        if answer in ("s", "skip"):
            return "s"
        print("  please answer y, n or s" + (", or v to view the cover art" if view else ""))


def art_preview(tiles: list[tuple[str, bytes | None]], size: int = 420) -> Path | None:
    """One side-by-side picture of the given images (caption, image bytes), saved to a temporary
    file. Images that are missing get an empty tile. Returns the file, or None without Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    pad, caption_h = 16, 44
    sheet = Image.new("RGB", (pad + len(tiles) * (size + pad), size + caption_h + 2 * pad), (32, 32, 32))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=16)
    except TypeError:                       # Pillow < 10.1
        font = ImageFont.load_default()
    for n, (caption, data) in enumerate(tiles):
        x, y = pad + n * (size + pad), pad
        label = caption
        if data:
            try:
                with Image.open(io.BytesIO(data)) as im:
                    label += f"  ({im.width}x{im.height})"
                    im.seek(0)
                    tile = im.convert("RGB")
                tile.thumbnail((size, size), Image.Resampling.LANCZOS)
                sheet.paste(tile, (x + (size - tile.width) // 2, y + (size - tile.height) // 2))
            except Exception:
                label += "  (unreadable)"
        else:
            draw.rectangle((x, y, x + size - 1, y + size - 1), outline=(90, 90, 90))
            draw.text((x + size // 2, y + size // 2), "none", fill=(150, 150, 150), font=font, anchor="mm")
        draw.text((x, y + size + 10), label, fill=(230, 230, 230), font=font)
    fd, name = tempfile.mkstemp(prefix="ripflow-art-", suffix=".png")
    os.close(fd)
    sheet.save(name)
    return Path(name)


def open_file(path: Path) -> None:
    """Open a file in the system's default viewer, without waiting for it."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))                              # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"  could not open {path}: {e}")
