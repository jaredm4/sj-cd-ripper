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

1. **Python 3.11+**, then `pip install pillow numpy` (cover art, dynamic range).
2. **cyanrip**: download `cyanrip-win64.exe` from https://github.com/cyanreg/cyanrip/releases
3. **flac**: https://xiph.org/flac/download.html (put `flac.exe` on PATH or set its path in the config).
   If flac is missing, ffmpeg is used for the integrity check instead.
4. **rsgain**: https://github.com/complexlogic/rsgain (on PATH, or set its path in the config).
5. Copy `ripflow.example.toml` to `ripflow.toml`, set `library`, the cyanrip path, and
   `drive_offset` (the read offset EAC already uses for your drive; without it AccurateRip
   will not match).

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

1. **Rip**: cyanrip reads with full cd-paranoia checking and verifies each track against
   AccurateRip v1 and v2. Results are read from its log:
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

1. Barcode equal to a `UPC`/`BARCODE` tag already on the files
2. Official before promo/bootleg
3. Not in `avoid_countries`
4. Format: CD first for ripped discs; Digital Media, then CD, for existing FLAC files
5. `prefer_countries` order, then any other country
6. Has a barcode
7. Most complete metadata (label, catalogue number, full date, packaging, cover on file, ...)
8. Earliest date

Releases not matched by disc ID must also have matching track lengths. A folder in
`Unknown/` that came from a ripflow rip keeps being treated as a CD rip.

No match: files go to `<library>/Unknown/<date time>/` as `Track 01...`, with a `discid.txt`
containing the disc ID and a link that adds the disc to MusicBrainz. Once it exists there:
`python -m ripflow process "<that folder>" --move`.

## Tags

On a MusicBrainz match all existing tags are dropped except the `[tags] keep` list
(default: `ALBUM DYNAMIC RANGE`, `DYNAMIC RANGE`, `ISRC`, `MUSICBRAINZ_DISCID`, `UPC`).

## Dynamic range and ReplayGain

- Dynamic range uses the DR14 method (the one behind the foobar2000 DR Meter). It writes
  `DYNAMIC RANGE` and `ALBUM DYNAMIC RANGE` (plain numbers) and a `dr.txt` in the album folder.
  The log layout is similar to, not identical to, foobar's.
- ReplayGain runs last: `rsgain easy --output <album folder>` (arguments configurable).

## Cover art

Sources in order: an existing `cover/folder/front.jpg|png`, Apple (iTunes search API, up to
3000 px), Cover Art Archive (original-size front image), Deezer (1000 px). A result is only used when artist and album
names match closely. Output: `cover.jpg` (original bytes kept when it is already a JPEG no
larger than 3000 px, otherwise re-encoded) plus a 600 px copy embedded in each track.

## Not tested yet

Written and tested without a CD drive or internet access. Tested: tagging, integrity check,
disc ID maths, release ranking, file placement, cover processing (with simulated API replies).
Needs a first real run: the cyanrip call and log parsing (based on a 0.9.3 log sample), live
MusicBrainz / Apple / Deezer responses, Windows disc detection and eject, the real rsgain,
and DR values against foobar2000 (compare on a few albums you have already measured). Linux disc
detection is a sketch.
