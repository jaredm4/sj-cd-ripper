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
| `python -m ripflow process <folder> [--move]` | Runs the pipeline on existing FLACs. Copies into the library unless `--move`; an album already in its library folder is renamed in place. |
| `python -m ripflow process <folder> --release <mbid>` | Overrides the automatic release choice. |
| `python -m ripflow process <folder> --ignore-local-art` | Ignores existing `cover.jpg` / embedded art and searches online again. |
| `python -m ripflow process <folder> --only-local-art` | Uses only a `cover`/`folder`/`front` `.jpg`/`.png`/`.webp` in the album folder (newest file wins). No online lookup; embedded art is ignored and replaced with a fresh 600 px copy. |
| `python -m ripflow process <folder> --auto` | Never asks which release to use (see below). |
| `python -m ripflow covers <root> [--dry-run]` | Updates cover art across an existing library (see below). Audio files are not touched. |
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
   (Enhanced CD, HDCD and similar count as CD)
7. `prefer_countries` order, then any other or no country
8. Has a barcode
9. Has front cover art on MusicBrainz
10. Most complete metadata (label, catalogue number, full date, packaging, ...)
11. Earliest date

The tag search drops edition notes such as "(Deluxe Version)" from the album title, because
MusicBrainz keeps those out of the title; the track count picks the right edition.

When `process` runs at a terminal and several releases fit the files equally well (same
barcode/ASIN result, same ISRC matches, same status and explicit/clean fit), it lists them and
asks: Enter takes the recommended one, a number picks another, `a` shows weaker candidates
too, a pasted release id or link uses that release, and `s` skips the album. `watch`, `rip`
and `process --auto` never ask.

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

## Other files in the album folder

`process` also brings along the album's other files from the source folder (`[files] extras`,
by default `*.jpg *.png *.cue *.nfo *.pdf *.lrc *.webp`). A file named after a track, such as
its `.lrc` lyrics, is renamed along with the track so players still find it. A lyric file whose
name no longer matches its track (say the tracks were converted and renamed by another tool) is
matched by the track number in its name, if its title also resembles that track's, or else by
a near-identical title; this is decided per file, so lyric files that still match keep their
match. A lyric file that fits no track keeps its name. Files ripflow makes
itself are never brought along, since they would describe the old files: `cover.jpg`,
`cover.webp`, `cover-static.*`, `dr.txt`, `rip.log`, `discid.txt` and `ripflow.json` (with
`--no-cover`, ripflow makes no cover, so the cover files do come along). A name that already
exists in the library folder is not overwritten. `.cue` files are copied as they are, so the
track names inside them refer to the old file names.

## Tags

Typographic punctuation in MusicBrainz names is converted to ASCII in tags and file names
(`ascii_punctuation`), the same way Picard's "Convert Unicode punctuation characters to ASCII"
option does: `It’s` -> `It's`, `Battle‐Axe` -> `Battle-Axe`, `…And Justice` -> `...And Justice`.
The file-name rules then apply to the result, so a `"` becomes `_` and a leading `.` becomes `_`.

On a MusicBrainz match all existing tags are dropped except the `[tags] keep` list
(default: `ALBUM DYNAMIC RANGE`, `DYNAMIC RANGE`, `ISRC`, `LYRICS`, `UNSYNCEDLYRICS`,
`MUSICBRAINZ_DISCID`, `UPC`).

### Animated covers

When Apple Music has an animated cover for the album, it is also saved as an animated
`cover.webp` next to `cover.jpg` (`animated`, on by default). This follows the approach of
[apple-music-animated-artworks](https://github.com/m8tec/apple-music-animated-artworks): the
album found by the iTunes search is looked up through the Apple Music web player's own
interface, and its video stream is converted with ffmpeg (which needs WebP support; the usual
Windows builds have it). That interface is not a documented public API, so it may stop working;
any failure is noted in the report and the rest of the album is processed as normal.

- Size: `animated_max_size` (default 1000 px), `animated_quality` (75), `animated_fps` (0 =
  source). Animated WebP files grow quickly; the report shows each file's size.
- An existing `cover.webp` is kept; `--ignore-local-art` makes a new one.
- A still (not animated) `cover.webp` is renamed to `cover-static.webp`, so it no longer competes
  with `cover.jpg`; it remains available as a still-cover source.
- Jellyfin and Navidrome show one album image, so the still cover can't sit beside the
  animation as `cover.jpg` (the server would pick either). When an animated `cover.webp` exists, the still
  is saved as `cover-static.jpg` (`animated_still_name`), a name neither server treats as album
  art; otherwise it is `cover.jpg`. Either way it is the image the embedded 600 px art is made
  from. Re-runs rename between the two as needed, and rename the earlier name `cover orig.jpg`.
- The embedded art is always the 600 px still, so players that read the files directly (and
  per-track art in Navidrome) still get a plain image.
- The still and embedded art always come from a still image, never the animation (unless
  nothing else is available, then its first frame is used).

### Updating a whole library: `covers`

`python -m ripflow covers D:\Music` walks the library and, for every album folder (one with a
`cover.jpg`, or with music files and no `folder.jpg`, which marks an artist folder):

- adds Apple Music's animated `cover.webp` where there is none yet and Apple has one
- finds a still cover where there is none, the same way `process` does (nothing found is fine)
- moves the still to `cover-static.jpg` when an animated cover exists, or back to `cover.jpg`
  when it does not

Artist and album come from the first track's tags (FLAC directly, other formats through
`ffprobe`), or from the folder names. What was learned is remembered in the album's `ripflow.json`
file (see below), so albums Apple has no animated cover for, or with no still cover anywhere,
are skipped next time (`--recheck` looks again). `--dry-run` looks
everything up but writes nothing; `--limit N` stops after N albums. Results go to the console
and to `logs/reports/<date> covers.tsv`.

Apple allows about 20 searches a minute, so iTunes searches are spaced 3 s apart
(`itunes_interval`) and each artist's album list is fetched once per run, so later albums by the
same artist need no search at all. The Apple Music lookup for an animated cover is spaced 1 s
(`apple_music_interval`). Albums whose `ripflow.json` already has the answer make no calls, so
there is nothing to wait for. Both settings go under `[cover]`; the summary shows how many calls
were made. Expect a few seconds per album; if Apple
starts refusing requests the run waits, then stops, and a re-run continues where it left off.

### Album memory: `ripflow.json`

Each album folder can hold a small `ripflow.json` file (JSON) with facts ripflow learned and does
not need to look up again: the album's Apple Music id, whether Apple has an animated cover, and
whether any still cover exists. It lives with the album, so it survives moving the library,
another machine or a fresh copy of ripflow. `process`, `watch` and `covers` read and update it;
it never holds log output. Delete it to make ripflow forget an album. (Versions before this
kept the cover memory in `logs/covers-state.json`; `covers` still reads that file once and
moves each album's entry into its `ripflow.json`, after which the old file can be deleted.)

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

Stores list every edition of an album under nearly the same name, so the store album is chosen
on signals that don't depend on naming, strongest first: its UPC equals the release's barcode
(looked up directly), then the same track count, then the same edition text (whatever is in
brackets or after " - " in the title, plus MusicBrainz's edition note), then how closely the
names match. Only the best-matching album's image is used, never a bigger image from another
edition; the report names the album used and the ones passed over.

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
