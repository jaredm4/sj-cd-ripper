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
    return drive_state(drive)[0]


def _windows_toc(drive: str) -> bytes | None:
    """Raw table of contents straight from the drive (IOCTL_CDROM_READ_TOC), or None.
    Unlike the TrackNN.cda files, this also works for Enhanced CDs, where Windows shows the
    disc's data session instead of the audio tracks."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.CreateFileW("\\\\.\\" + drive.rstrip("\\"), 0x80000000, 3, None, 3, 0, None)   # read, shared, existing
    if handle in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        buf = ctypes.create_string_buffer(804)
        got = wintypes.DWORD(0)
        if not k.DeviceIoControl(handle, 0x00024000, None, 0, buf, len(buf), ctypes.byref(got), None):
            return None
        return buf.raw[:got.value]
    finally:
        k.CloseHandle(handle)


def toc_tracks(toc: bytes) -> tuple[int, int]:
    """(audio tracks, data tracks) in a raw CDROM_TOC: 4 header bytes, then 8 bytes per track."""
    audio = data = 0
    for pos in range(4, len(toc) - 7, 8):
        control, number = toc[pos + 1] & 0x0F, toc[pos + 2]
        if number == 0xAA:                 # lead-out entry
            break
        if control & 0x04:
            data += 1
        else:
            audio += 1
    return audio, data


def drive_state(drive: str) -> tuple[str | None, str]:
    """(signature or None, plain description of what the drive currently shows)."""
    if WINDOWS:
        try:
            toc = _windows_toc(drive)
        except Exception as e:
            toc = None
            log.debug("TOC read failed on %s: %r", drive, e)
        if toc and len(toc) >= 12:
            audio, data = toc_tracks(toc)
            fp = hashlib.sha1(toc).hexdigest()
            if not audio:
                return None, f"data disc ({data} data track(s), no audio)"
            extra = f" + {data} data track(s) (Enhanced CD)" if data else ""
            return fp, f"audio CD, {audio} audio tracks{extra}, fingerprint {fp[:8]}"
        # No TOC (no disc, or the call is not allowed): fall back to the TrackNN.cda stubs
        # that Windows shows for plain audio CDs.
        root = drive.rstrip("\\") + "\\"
        try:
            entries = os.listdir(root)
            names = sorted(n for n in entries if n.lower().endswith(".cda"))
            if not names:
                return None, f"disc with {len(entries)} entries but no audio tracks, and no readable TOC"
            h = hashlib.sha1()
            for n in names:
                with open(root + n, "rb") as f:
                    h.update(f.read())
            return h.hexdigest(), f"audio CD, {len(names)} tracks, fingerprint {h.hexdigest()[:8]}"
        except OSError as e:
            return None, f"no readable disc ({e.strerror or e})"
    try:
        import fcntl
        fd = os.open(drive, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if fcntl.ioctl(fd, 0x5326, 0) != 4:                # CDROM_DRIVE_STATUS != CDS_DISC_OK
                return None, "no disc"
            audio = fcntl.ioctl(fd, 0x5327, 0) in (100, 105)   # CDS_AUDIO / CDS_MIXED
            return ("audio", "audio CD") if audio else (None, "disc without audio tracks")
        finally:
            os.close(fd)
    except (OSError, ImportError) as e:
        return None, f"no readable disc ({e})"


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
    shown: dict[str, str] = {}       # drive -> last state we logged, so only changes are logged
    try:
        while True:
            drives = cd_drives(cfg)
            if shown.get("") != str(drives):
                shown[""] = str(drives)
                log.info("optical drives: %s", ", ".join(drives) or "none found")
            for drive in drives:
                sig, state = drive_state(drive)
                # Windows: skip a disc we just did, even if the tray closed itself on it again.
                # Elsewhere the signature is generic, so require the drive to have been empty.
                same = sig is not None and done.get(drive) == sig and (WINDOWS or drive not in seen_empty)
                if same:
                    state += " - same disc as the one just ripped, waiting for a different one"
                if shown.get(drive) != state:
                    shown[drive] = state
                    log.info("%s %s", drive, state)
                if sig is None:
                    seen_empty.add(drive)
                    continue
                if same:
                    continue
                time.sleep(3)        # let the drive settle
                log.info("audio CD detected in %s", drive)
                rip_and_process(cfg, drive)
                done[drive] = sig
                seen_empty.discard(drive)
            time.sleep(cfg["rip"]["poll_seconds"])
    except KeyboardInterrupt:
        log.info("stopped")
