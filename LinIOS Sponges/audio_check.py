"""Lightweight audio-file integrity checks (pure Python, no dependencies).

Used to make sure music is complete and not corrupt so playback does not skip.
Each checker inspects the container header / atom structure; anything that is
truncated, has a broken header, or is not actually an audio file gets flagged.
"""

import os
import shutil
import subprocess

# how many bytes of header we inspect; enough for all supported formats
HEADER_SCAN = 256 * 1024


def check_audio(data: bytes, ext: str):
    """Validate audio bytes.

    Returns (ok: bool, reason: str).
    """
    if not data:
        return False, "file is empty (0 bytes)"
    ext = (ext or "").lower()
    try:
        if ext in (".mp3",):
            return _check_mp3(data)
        if ext in (".m4a", ".mp4", ".aac", ".m4b", ".m4p"):
            return _check_mp4(data)
        if ext in (".wav",):
            return _check_wav(data)
        if ext in (".flac",):
            return _check_flac(data)
        if ext in (".ogg", ".oga"):
            return _check_ogg(data)
        if ext in (".aiff", ".aif", ".aifc"):
            return _check_aiff(data)
    except Exception:
        return False, "could not parse file structure"
    # format we do not validate; accept and let the player handle it
    return True, "ok"


def check_audio_file(path: str):
    """Validate a local audio file on disk. Returns (ok, reason).

    Runs the structural container checks, then (when ffprobe is available) a
    real decode probe to catch damage the header checks cannot see."""
    try:
        ext = os.path.splitext(path)[1]
        with open(path, "rb") as fh:
            head = fh.read(HEADER_SCAN)
            size = fh.seek(0, os.SEEK_END)
            data = head + b"\x00" * max(0, size - len(head))
        ok, reason = check_audio(data, ext)
        if ok:
            return _deep_probe(path)
        return ok, reason
    except OSError as e:
        return False, f"cannot read file ({e})"
    except Exception:
        return False, "could not read file"


def _deep_probe(path: str):
    """Deep-decode the file with ffprobe if it exists. Best-effort only."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return True, "ok"
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True, "ok"
    out = result.stdout.decode(errors="replace").strip()
    err_lines = result.stderr.decode(errors="replace").strip().splitlines()
    if result.returncode != 0:
        detail = err_lines[-1] if err_lines else "audio decoder rejected the file"
        return False, f"decoder error ({detail})"
    try:
        duration = float(out) if out else 0.0
    except ValueError:
        duration = 0.0
    if duration <= 0:
        return False, "file contains no playable audio"
    return True, "ok"


def _has_frame_sync(buf: bytes, start: int, end: int) -> bool:
    """True if a valid MPEG audio frame sync 0xFF Ex/Ex appears in buf[start:end]."""
    for i in range(start, min(end, len(buf) - 1)):
        if buf[i] == 0xFF and (buf[i + 1] & 0xE0) == 0xE0:
            # MPEG version cannot be '01' (reserved) with layer '00' (reserved)
            ver = (buf[i + 1] >> 3) & 0x03
            layer = (buf[i + 1] >> 1) & 0x03
            if ver != 0x01 and layer != 0x00:
                return True
    return False


def _check_mp3(data: bytes) -> tuple:
    if data[:3] == b"ID3":
        # ID3v2 tag; frame sync should follow the tag
        tag_size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | \
                   ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        start = 10 + tag_size
        if start >= len(data):
            return False, "ID3 header claims more data than the file contains"
        if not _has_frame_sync(data, start, min(len(data), start + HEADER_SCAN)):
            return False, "no valid MP3 frames after ID3 tag"
        return True, "ok"
    if _has_frame_sync(data, 0, min(len(data), HEADER_SCAN)):
        return True, "ok"
    return False, "not a valid MP3 (no frame sync found)"


def _check_mp4(data: bytes) -> tuple:
    # locate 'ftyp' atom (a leading 'free'/'skip' atom is allowed)
    off, ftyp = 0, None
    while off + 8 <= len(data):
        size = int.from_bytes(data[off:off + 4], "big")
        typ = data[off + 4:off + 8]
        if typ == b"ftyp":
            ftyp = off
            break
        if size < 8:
            break
        off += size
    if ftyp is None:
        return False, "missing ftyp header (not an MP4/M4A file)"

    # walk every atom; declared sizes must fit inside the file
    off, have_moov, have_mdat = 0, False, False
    while off + 8 <= len(data):
        size = int.from_bytes(data[off:off + 4], "big")
        typ = data[off + 4:off + 8]
        if size == 1:  # extended 64-bit size
            if off + 16 > len(data):
                return False, "truncated atom header"
            size = int.from_bytes(data[off + 8:off + 16], "big")
        elif size == 0:  # extends to end of file (legal for 'mdat')
            size = len(data) - off
        if size < 8:
            return False, "invalid atom size"
        if off + size > len(data):
            return False, "file is truncated (atom runs past the end)"
        if typ == b"moov":
            have_moov = True
        elif typ == b"mdat":
            have_mdat = True
        off += size
    if not (have_moov or have_mdat):
        return False, "no media data in file"
    return True, "ok"


def _check_riff(data: bytes, magic: bytes, kind: bytes, endian: str = "little") -> tuple:
    if len(data) < 12 or data[:4] != magic:
        return False, "missing container header"
    declared = int.from_bytes(data[4:8], endian) + 8
    if data[8:12] != kind:
        return False, "file type mismatch (not WAV/AIFF)"
    if declared > len(data):
        return False, "file is truncated (declared size exceeds file)"
    # walk chunks
    off = 12
    while off + 8 <= len(data):
        size = int.from_bytes(data[off + 4:off + 8], endian)
        if off + 8 + size > len(data):
            return False, "file is truncated (chunk runs past the end)"
        off += 8 + size + (size & 1)  # chunks are word-aligned
    return True, "ok"


def _check_wav(data: bytes) -> tuple:
    if len(data) < 12:
        return False, "file too small to be a WAV"
    r = _check_riff(data, b"RIFF", b"WAVE", endian="little")
    if not r[0]:
        return r
    if b"fmt " not in data[: min(len(data), HEADER_SCAN)]:
        return False, "missing fmt chunk"
    if b"data" not in data[: min(len(data), HEADER_SCAN)]:
        return False, "missing audio data chunk"
    return True, "ok"


def _check_flac(data: bytes) -> tuple:
    if data[:4] != b"fLaC":
        return False, "missing fLaC marker (not a FLAC file)"
    if len(data) < 42:
        return False, "FLAC file is truncated (no STREAMINFO block)"
    return True, "ok"


def _check_ogg(data: bytes) -> tuple:
    if len(data) < 28 or data[:4] != b"OggS":
        return False, "missing OggS capture pattern (not an OGG file)"
    if data[4] != 0:
        return False, "unknown Ogg version"
    return True, "ok"


def _check_aiff(data: bytes) -> tuple:
    if len(data) < 12:
        return False, "file too small to be an AIFF"
    kind = data[8:12]
    if kind not in (b"AIFF", b"AIFC"):
        return False, "not an AIFF/AIFC file"
    r = _check_riff(data, b"FORM", kind, endian="big")
    if not r[0]:
        return r
    if b"SSND" not in data[: min(len(data), HEADER_SCAN)]:
        return False, "missing sound data chunk"
    return True, "ok"