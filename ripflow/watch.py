"""The "daemon": wait for an audio CD, rip it, hand it to the pipeline, eject, repeat."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time

from .config import attention, log
from .pipeline import process_folder
from .ripper import rip_disc

WINDOWS = sys.platform == "win32"


# ---- drive helpers (Windows now; Linux versions are a first sketch, untested) ----

def cd_drives(cfg: dict) -> list[str]:
    if cfg["rip"]["drive"]:
        return [cfg["rip"]["drive"]]
    if WINDOWS:
        import ctypes
        import string
        k = ctypes.windll.kernel32
        k.SetErrorMode(0x0001)       # never pop a "no disk in drive" dialog
        mask = k.GetLogicalDrives()
        return [f"{c}:" for i, c in enumerate(string.ascii_uppercase)
                if mask >> i & 1 and k.GetDriveTypeW(f"{c}:\\") == 5]
    return [d for d in ("/dev/sr0", "/dev/sr1") if os.path.exists(d)]


def disc_signature(drive: str) -> str | None:
    """None if the drive holds no audio CD; otherwise a string that identifies the disc."""
    if WINDOWS:
        # Windows presents audio tracks as tiny TrackNN.cda stubs holding the track
        # positions and a disc serial - hashing them fingerprints the disc.
        root = drive.rstrip("\\") + "\\"
        try:
            names = sorted(n for n in os.listdir(root) if n.lower().endswith(".cda"))
            if not names:
                return None
            h = hashlib.sha1()
            for n in names:
                with open(root + n, "rb") as f:
                    h.update(f.read())
            return h.hexdigest()
        except OSError:
            return None
    try:
        import fcntl
        fd = os.open(drive, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if fcntl.ioctl(fd, 0x5326, 0) != 4:                # CDROM_DRIVE_STATUS != CDS_DISC_OK
                return None
            return "audio" if fcntl.ioctl(fd, 0x5327, 0) in (100, 105) else None   # CDS_AUDIO / CDS_MIXED
        finally:
            os.close(fd)
    except (OSError, ImportError):
        return None


def eject(drive: str) -> None:
    try:
        if WINDOWS:
            import ctypes
            mci = ctypes.windll.winmm.mciSendStringW
            if mci(f"open {drive} type cdaudio alias ripflowcd", None, 0, None) == 0:
                err = mci("set ripflowcd door open", None, 0, None)
                mci("close ripflowcd", None, 0, None)
                if err == 0:
                    return
            ps = f"(New-Object -ComObject Shell.Application).Namespace(17).ParseName('{drive}').InvokeVerb('Eject')"
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=30)
        else:
            subprocess.run(["eject", drive], timeout=30)
    except Exception as e:      # ejecting is a convenience; never let it kill the watcher
        log.warning("could not eject %s: %s", drive, e)


def beep(ok: bool, cfg: dict) -> None:
    if not (WINDOWS and cfg["rip"]["beep"]):
        return
    try:
        import winsound
        for _ in range(1 if ok else 3):
            winsound.Beep(880 if ok else 440, 250)
            time.sleep(0.15)
    except Exception:
        pass


# ---- one disc, and the loop --------------------------------------------------

def rip_and_process(cfg: dict, drive: str) -> bool:
    """Rip the disc in `drive` and run the pipeline on the result. True = nothing needs attention."""
    ok = False
    try:
        rip = rip_disc(cfg, drive)
        if not rip.files:
            attention(f"{drive} :: rip produced no files - {rip.message} (kept {rip.folder})")
        else:
            outcomes = process_folder(cfg, rip.folder, move=True, rip=rip)
            ok = all(o.ok for o in outcomes)
            if not any(p.suffix.lower() == ".flac" for p in rip.folder.rglob("*")):
                shutil.rmtree(rip.folder, ignore_errors=True)      # everything was filed
    except Exception as e:
        log.exception("unexpected error while handling the disc in %s", drive)
        attention(f"{drive} :: unexpected error: {e!r} (see ripflow.log)")
    mode = cfg["rip"]["eject"]
    if mode == "always" or (mode == "success" and ok):
        eject(drive)
    beep(ok, cfg)
    return ok


def watch(cfg: dict) -> None:
    log.info("watching for audio CDs on %s (Ctrl+C to stop)", cfg["rip"]["drive"] or "any optical drive")
    done: dict[str, str] = {}        # drive -> signature of the disc we already handled
    seen_empty: set[str] = set()
    try:
        while True:
            for drive in cd_drives(cfg):
                sig = disc_signature(drive)
                if sig is None:
                    seen_empty.add(drive)
                    continue
                # Windows: skip a disc we just did, even if the tray closed itself on it again.
                # Elsewhere the signature is generic, so require the drive to have been empty.
                if done.get(drive) == sig and (WINDOWS or drive not in seen_empty):
                    continue
                time.sleep(3)        # let the drive settle
                log.info("audio CD detected in %s", drive)
                rip_and_process(cfg, drive)
                done[drive] = sig
                seen_empty.discard(drive)
            time.sleep(cfg["rip"]["poll_seconds"])
    except KeyboardInterrupt:
        log.info("stopped")
