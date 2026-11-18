"""Small shared helpers: HTTP, text matching, filename sanitising."""
from __future__ import annotations

import re
import time
import unicodedata
import urllib.error
import urllib.request
from difflib import SequenceMatcher


class NotFound(Exception):
    pass


class NetworkError(Exception):
    pass


def http_get(url: str, ua: str, timeout: int = 30, retries: int = 3, headers: dict | None = None) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                raise NotFound(url) from None
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        time.sleep(2 * (attempt + 1))
    raise NetworkError(f"{url}: {last}")


_EDITION = re.compile(
    r"\s*[\(\[][^\)\]]*\b(deluxe|remaster(ed)?|edition|version|expanded|bonus|explicit|clean|"
    r"anniversary|reissue|mono|stereo|disc \d+|cd ?\d+)\b[^\)\]]*[\)\]]", re.I)


def _punctuation_table() -> dict:
    """Unicode punctuation -> ASCII, mirroring MusicBrainz Picard's "Convert Unicode
    punctuation characters to ASCII" option, so names match a Picard-tagged library."""
    groups = {
        "'": "‘’‚‛′",
        '"': "“”„‟″〝〞",
        "-": "‐‑‒–—―﹘−•",
        "<<": "«《︽≪", ">>": "»》︾≫",
        "<": "‹〈︿", ">": "›〉﹀",
        "[": "⁅〔〘〚︹﹝", "]": "⁆〕〙〛︺﹞",
        "((": "｟⦅", "))": "｠⦆",
        "|": "︱︲∣", "||": "‖∥",
        "/": "⁄∕", "\\": "∖", "*": "⁎",
        ",": "︑﹑､", ".": "︒｡",
        "L": "Ŀ", "l": "ŀ",
        "": "­​",                      # soft hyphen, zero-width space: dropped
    }
    return {ord(ch): ascii_ for ascii_, chars in groups.items() for ch in chars}


_PUNCTUATION = _punctuation_table()


def ascii_punctuation(value):
    """Typographic punctuation -> ASCII: curly quotes, dashes, the ellipsis character (...),
    special spaces, ligatures and other compatibility forms. Works on a string, a list, or a
    dataclass (every string inside it, recursively); anything else is returned unchanged."""
    if isinstance(value, str):
        # NFKC handles the compatibility characters (ellipsis, odd spaces, ligatures, fullwidth forms)
        return unicodedata.normalize("NFKC", value).translate(_PUNCTUATION)
    if isinstance(value, list):
        return [ascii_punctuation(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        for name in value.__dataclass_fields__:
            setattr(value, name, ascii_punctuation(getattr(value, name)))
    return value


def strip_edition(text: str, all_brackets: bool = False) -> str:
    """"Hell: The Sequel (Deluxe Version)" -> "Hell: The Sequel". MusicBrainz keeps edition
    notes out of the title, so they must go before searching."""
    out = (re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", text or "") if all_brackets else _EDITION.sub("", text or "")).strip()
    return out or (text or "").strip()


def norm(text: str) -> str:
    """Loose comparison form: no accents, punctuation, edition suffixes or leading 'the'."""
    text = _EDITION.sub("", text or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower().replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^the ", "", text)


def similar(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


_UNSAFE = re.compile(r'[*:<>?|"/\\\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_field(text: str) -> str:
    """A tag value on its way into a file name, the way MusicBrainz Picard does it:
    each of * : < > ? | " (and path separators) becomes an underscore."""
    return _UNSAFE.sub("_", str(text)).strip()


def safe_component(text: str, is_dir: bool = False, limit: int = 180) -> str:
    """One finished folder or file name (without extension). As in Picard, a leading
    period becomes an underscore ("...And Justice for All" -> "_..And Justice for All").
    Trailing periods are kept on file names; on folder names a trailing period also
    becomes an underscore ("P.O.D." -> "P.O.D_"), since Windows cannot store it."""
    text = safe_field(text)[:limit].strip()
    if is_dir and text.endswith("."):
        text = text[:-1] + "_"
    if text.startswith("."):
        text = "_" + text[1:]
    if not text or text.upper().split(".")[0] in _RESERVED:
        text += "_"
    return text


def natural_key(text: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


def lead_int(text: str, default: int = 0) -> int:
    m = re.match(r"\s*(\d+)", text or "")
    return int(m.group(1)) if m else default
