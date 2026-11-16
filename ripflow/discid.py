"""MusicBrainz disc ID / TOC maths (pure Python, no libdiscid needed)."""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

SECTOR = 588          # samples per CD frame at 44.1 kHz
LEAD_IN = 150         # the standard 2-second pregap


@dataclass
class Toc:
    first: int
    last: int
    leadout: int
    offsets: list[int]
    exact: bool = True   # False if it was estimated from files that aren't clean CD-frame multiples

    @property
    def discid(self) -> str:
        s = f"{self.first:02X}{self.last:02X}{self.leadout:08X}"
        s += "".join(f"{o:08X}" for o in (self.offsets + [0] * 99)[:99])
        digest = hashlib.sha1(s.encode("ascii")).digest()
        return base64.b64encode(digest, altchars=b"._").decode("ascii").replace("=", "-")

    @property
    def toc_param(self) -> str:
        """The form the MusicBrainz web service takes in ?toc=..."""
        return "+".join(str(x) for x in [self.first, self.last, self.leadout, *self.offsets])

    @property
    def attach_url(self) -> str:
        """Open this in a browser to attach the disc to (or add) a MusicBrainz release."""
        return (f"https://musicbrainz.org/cdtoc/attach?id={self.discid}"
                f"&tracks={self.last - self.first + 1}&toc={self.toc_param}")


def toc_from_samples(samples: list[int], sample_rate: int = 44100) -> Toc | None:
    """Rebuild a TOC from track lengths (assumes a plain audio CD, no data track or hidden pregap)."""
    if not samples or len(samples) > 99 or sample_rate != 44100:
        return None
    offsets, pos, exact = [], LEAD_IN, True
    for n in samples:
        offsets.append(pos)
        exact = exact and n % SECTOR == 0
        pos += -(-n // SECTOR)
    return Toc(1, len(samples), pos, offsets, exact)


def toc_from_param(text: str) -> Toc | None:
    try:
        nums = [int(x) for x in text.replace("+", " ").split()]
        return Toc(nums[0], nums[1], nums[2], nums[3:]) if len(nums) >= 4 else None
    except ValueError:
        return None
