# ripflow

Insert a CD, get verified, tagged, Jellyfin-ready FLACs. The same pipeline also works on
folders of FLACs you already have.

```
watch (daemon)        process <folder>
   | rip (cyanrip)         |
   +----------+------------+
              v
   integrity check -> MusicBrainz match -> cover art -> dynamic range -> tag + file
   -> ReplayGain (rsgain) -> <library>/<artist>/<album>/
```

## Setup (Windows)

1. **Python 3.11+**, then `pip install -r requirements.txt` (Pillow for cover art, numpy for dynamic range).
2. **cyanrip**: download `cyanrip-win64.exe` from https://github.com/cyanreg/cyanrip/releases
3. **flac**: https://xiph.org/flac/download.html (put `flac.exe` on PATH or set its path in the config).
   If flac is missing, ffmpeg is used for the integrity check instead.
4. **rsgain**: https://github.com/complexlogic/rsgain (on PATH, or set its path in the config).
5. Copy `ripflow.example.toml` to `ripflow.toml` and set `library`. Everything else is commented
   out and shows the default; uncomment what you need, at least the cyanrip path (if it is
   not on PATH) and `drive_offset` (the read offset EAC uses for your drive; without it
   AccurateRip will not match).

## Commands

Run from the folder that contains `ripflow.toml`:

| Command | What it does |
|---|---|
| `python -m ripflow watch` | Waits for audio CDs. Rips, processes, ejects, waits for the next one. |
| `python -m ripflow rip` | Same, once, for the disc that is in the drive now. |
| `python -m ripflow process <folder> --dry-run` | Shows the MusicBrainz candidates, the pick, and the planned file names. Changes nothing. |
| `python -m ripflow process <folder> [--move]` | Runs the pipeline on existing FLACs. Copies into the library unless `--move`. |
| `python -m ripflow process <folder> --release <mbid>` | Overrides the automatic release choice. |
| `python -m ripflow verify <root>` | Whole-library integrity scan (replaces `check_flac.sh`, same TSV idea). |

## Running headless

- Tray opens = finished, swap discs. With `eject = "success"` the tray stays shut when
  something needs a look.
- One beep = clean, three low beeps = an entry was added to `logs/ATTENTION.log`.
- `logs/ATTENTION.log` has one line per problem; `logs/reports/` has a full report per album
  (integrity, AccurateRip per track, every MusicBrainz candidate and which one won, cover source).
- If the tray closes itself on the same disc, it is recognised and not ripped again.

## What gets checked

1. **Rip**: cyanrip reads with full cd-paranoia checking and computes AccurateRip v1/v2
   checksums. If cyanrip itself reports the disc as not found, ripflow looks the disc up in the
   AccurateRip database directly and compares cyanrip's checksums.
   VERIFIED (all tracks match), UNVERIFIED (disc not in the database), SUSPECT (a track mismatched).
2. **Files** (fresh rips and existing FLACs): every FLAC is fully decoded; decoded sample count is
   compared with the header (catches truncation), decoder errors are captured, and the MD5 of
   the decoded audio is compared with the MD5 stored by the encoder.
3. **After tagging**: header length and MD5 are re-read to confirm the audio was not touched.

## How a release is chosen

A disc ripped by ripflow is looked up by disc ID (text search only as a fallback). For
existing FLAC files, disc ID / track-length matches and a search on the existing artist and
album tags are pooled and ranked together. An existing `MUSICBRAINZ_ALBUMID` tag or
`--release` is used directly.

Ranking, most important first:

1. Barcode (`UPC`/`BARCODE` tag) or Amazon ASIN (from a `WWW`/`ASIN` tag) equal to the release's
2. Most tracks whose `ISRC` tag matches the release's recordings
3. Official before promo/bootleg
4. Not in `avoid_countries`
5. Explicit edition over clean (reversed when the files are tagged `Rating=Clean`)
6. Format: CD first for ripped discs; Digital Media, then CD, for existing FLAC files
7. `prefer_countries` order, then any other or no country
8. Has a barcode
9. Has front cover art on MusicBrainz
10. Most complete metadata (label, catalogue number, full date, packaging, ...)
11. Earliest date

The tag search drops edition notes such as "(Deluxe Version)" from the album title, because
MusicBrainz keeps those out of the title; the track count picks the right edition.

Releases not matched by disc ID must also have matching track lengths. A folder in
`Unknown/` that came from a ripflow rip keeps being treated as a CD rip.

No match: files go to `<library>/Unknown/<date time>/` as `Track 01...`, with a `discid.txt`
containing the disc ID and a link that adds the disc to MusicBrainz. Once it exists there:
`python -m ripflow process "<that folder>" --move`.

## File names

Defaults mirror the Picard naming preset:
`Artist/Album/Artist - Album - 01 Title.flac`, or `... - 1-01 Title.flac` when the release
has more than one disc. As in Picard, each of `* : < > ? | "` becomes `_`, and a leading
period becomes `_` (`_..And Justice for All`). Trailing periods stay on file names; on a
folder name a trailing period also becomes `_` (`P.O.D_`), because Windows cannot store it.

## Tags

On a MusicBrainz match all existing tags are dropped except the `[tags] keep` list
(default: `ALBUM DYNAMIC RANGE`, `DYNAMIC RANGE`, `ISRC`, `LYRICS`, `UNSYNCEDLYRICS`,
`MUSICBRAINZ_DISCID`, `UPC`).

## Dynamic range and ReplayGain

- Dynamic range uses the DR14 method (the one behind the foobar2000 DR Meter). It writes
  `DYNAMIC RANGE` and `ALBUM DYNAMIC RANGE` (plain numbers) and a `dr.txt` in the album folder.
  The log layout is similar to, not identical to, foobar's.
- ReplayGain runs last: `rsgain easy --output <album folder>` (arguments configurable).

## Cover art

Primary sources (`sources`): an existing `cover/folder/front.jpg|png`, the art embedded in the
first track, and Apple (iTunes search API, the Apple Music catalogue). The largest image among
them wins. Only when none of them has a usable image are the `fallback_sources` tried: Cover
Art Archive (original-size front image, often a scan) and Deezer (1000 px).

An image must be at least `min_size` (600) and is scaled down to `max_size` (3000). Images
that are slightly off square are centre-cropped, never stretched; anything beyond
`max_aspect` is skipped. Apple and Deezer results are only considered when artist and album
names match closely. Output: `cover.jpg` plus a 600 px copy embedded in each track, which
replaces any art that was embedded before.

## Not tested yet

Written and tested without a CD drive or internet access. Tested: tagging, integrity check,
disc ID maths, release ranking, file placement, cover processing (with simulated API replies).
Needs a first real run: the cyanrip call and log parsing (based on a 0.9.3 log sample), live
MusicBrainz / Apple / Deezer responses, Windows disc detection and eject, the real rsgain,
and DR values against foobar2000 (compare on a few albums you have already measured). Linux disc
detection is a sketch.
