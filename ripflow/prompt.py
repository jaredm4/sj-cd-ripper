"""Asking the user which MusicBrainz release to use (only for `ripflow process` at a terminal)."""
from __future__ import annotations

import re

from .musicbrainz import Candidate, _media, comment_for_title, full_title

_MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _describe(n: int, c: Candidate, recommended: bool) -> list[str]:
    rel = c.release
    infos = rel.get("label-info") or []
    label = ", ".join(dict.fromkeys(name for i in infos if (name := (i.get("label") or {}).get("name"))))
    catno = ", ".join(dict.fromkeys(x for i in infos if (x := i.get("catalog-number"))))
    title = full_title(rel.get("title", ""), comment_for_title(rel, []))   # incl. the release group's comment
    regions = len(rel.get("release-events") or [])
    facts = [(c.country or "no country") + (f" (+{regions - 1} more regions)" if regions > 1 else ""),
             rel.get("date") or "no date", _media(rel, c.medium)]
    facts += [x for x in (label, catno and f"cat. {catno}",
                          rel.get("barcode") and f"barcode {rel['barcode']}") if x]
    if c.key[0] == 0:
        facts.append("barcode/ASIN matches your files")
    if c.isrc_hits:
        facts.append(f"{c.isrc_hits} ISRC matches")
    return [f"  {n:2}) {title}{'   <- recommended' if recommended else ''}",
            f"      {' | '.join(facts)}",
            f"      https://musicbrainz.org/release/{rel['id']}"]


def ask_release(ranked: list[Candidate], tier: int, resolve, heading: str | None = None):
    """Interactive chooser for musicbrainz.identify(). Returns (release, medium) or None (skip).
    heading: shown instead of "N releases fit equally well" (when you are picking again)."""
    shown = tier
    while True:
        hidden = f" ({len(ranked) - shown} weaker ones hidden)" if shown < len(ranked) else ""
        print(f"\n{heading}{hidden}:" if heading else
              f"\n{tier} MusicBrainz releases fit these files equally well{hidden}:")
        for n, c in enumerate(ranked[:shown], 1):
            print("\n".join(_describe(n, c, n == 1)))
        more = "a = show all, " if shown < len(ranked) else ""
        pick = f"Choose 1-{shown} (Enter = 1), " if ranked else ""
        try:
            answer = input(f"\n{pick}{more}paste a release id/link, or s = skip album: ").strip()
        except EOFError:                     # input closed: as before, take the recommended one
            return (ranked[0].release, ranked[0].medium) if ranked else None
        if not answer:
            if ranked:
                return ranked[0].release, ranked[0].medium
            print("  paste a release id/link, or s to skip")
            continue
        if answer.isdigit() and 1 <= int(answer) <= shown:
            c = ranked[int(answer) - 1]
            return c.release, c.medium
        if answer.lower() == "a" and more:
            shown = len(ranked)
            continue
        if answer.lower() in ("s", "skip", "q"):
            return None
        m = _MBID.search(answer)
        if m:
            found = resolve(m.group(0).lower())
            if isinstance(found, tuple):
                return found
            print(f"  {found}")
            continue
        print("  not understood, try again")
