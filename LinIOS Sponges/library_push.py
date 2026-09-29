"""Add music to the iPhone's Music *library* (not the Files folder).

This orchestrates the whole operation, in the order that keeps it recoverable:

1. Take a verified backup of the current media library. Nothing is written to
   the device before this succeeds, so there is always a known-good snapshot to
   restore from.
2. Copy each file to ``/iTunes_Control/Music/LinIOS/`` and read it back to
   confirm the bytes arrived intact.
3. Build a new database from that backup with a row registered for each track,
   and install it using :func:`medialib.install_db`, which stages the file,
   proves it read back intact, keeps the previous database aside on the device,
   and drops the stale WAL/SHM sidecars.
4. Report that a reboot is required, because ``medialibd`` holds the database
   open and will not see the new rows until then.

If any step fails, the library is left exactly as it was: the database is only
replaced in the final step, and the copy it replaces is preserved on the device.
"""

import asyncio
import os
import shutil
import tempfile

import library_add
import medialib
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService

FOLDER = library_add.DEFAULT_FOLDER


async def _connect():
    lockdown = await create_using_usbmux(connection_type="USB")
    return lockdown, AfcService(lockdown)


def _emit(cb, done, total, label):
    if cb:
        cb(done, total, label)


async def push_to_library(local_paths, progress=None, folder=FOLDER,
                           backup_root=None, keep_old=True):
    """Register ``local_paths`` in the iPhone's Music library.

    Returns a summary dict. Raises :class:`medialib.MedialibError` or
    :class:`library_add.LibraryAddError` on failure, in which case the device
    library is unmodified.
    """
    local_paths = [p for p in local_paths if p]
    if not local_paths:
        raise library_add.LibraryAddError("no files selected")
    total = len(local_paths)

    # --- 1. verified backup first, always ------------------------------- #
    _emit(progress, 0, total, "Backing up your music library before changing it...")
    manifest, backup_dir = await medialib.backup(
        note="auto: before LinIOS library push", progress=None)
    clean_db = os.path.join(backup_dir, medialib.CLEAN_NAME)
    if not os.path.isfile(clean_db):
        raise medialib.MedialibError("backup did not produce a clean database")

    # --- 2. metadata + copy, verifying each file ------------------------ #
    _emit(progress, 0, total, "Reading music tags...")
    tracks = []
    for i, path in enumerate(local_paths, 1):
        name = os.path.basename(path)
        try:
            info = library_add.read_tags(path)
        except Exception:
            info = {"title": os.path.splitext(name)[0], "artist": "Unknown Artist",
                    "album": "Unknown Album", "track": 0, "disc": 0,
                    "year": 0, "duration_ms": 0.0,
                    "size": os.path.getsize(path)}
        info["path"] = path
        info.setdefault("size", os.path.getsize(path))
        tracks.append(info)
        _emit(progress, i, total, "Read tags: %s" % info["title"])

    lockdown, afc = await _connect()
    try:
        base = "/iTunes_Control/Music/%s" % folder
        for d in _ancestors(base):
            try:
                await afc.makedirs(d)
            except Exception:
                pass

        taken = set()
        try:
            taken = {n for n in await afc.listdir(base) if n not in (".", "..")}
        except Exception:
            pass

        written = []
        for i, t in enumerate(tracks, 1):
            ext = os.path.splitext(t["path"])[1] or ".mp3"
            remote_name = library_add.safe_filename(t["title"], ext)
            if remote_name in taken:
                stem, e = os.path.splitext(remote_name)
                n = 2
                while "%s (%d)%s" % (stem, n, e) in taken:
                    n += 1
                remote_name = "%s (%d)%s" % (stem, n, e)
            taken.add(remote_name)
            t["remote_name"] = remote_name

            _emit(progress, i, total, "Copying to iPhone: %s" % t["title"])
            with open(t["path"], "rb") as fh:
                data = fh.read()
            await afc.set_file_contents("%s/%s" % (base, remote_name), data)
            back = await afc.get_file_contents("%s/%s" % (base, remote_name))
            if back != data:
                raise library_add.LibraryAddError(
                    "%s did not read back intact from the iPhone" % t["title"])
            t["size"] = len(data)
            t["file_size"] = len(data)
            written.append(remote_name)

        # --- 3. build + install the database ---------------------------- #
        _emit(progress, total, total, "Registering tracks in the music library...")
        work = tempfile.mkdtemp(prefix="linios-lib-")
        try:
            staged = os.path.join(work, "work.db")
            final = os.path.join(work, "final.db")
            info = library_add.register_batch(
                clean_db, staged, tracks, folder=folder, integrity_mode="null")
            library_add.finalize(staged, final)
            report = medialib.validate_db_file(final)
            if not report.get("ok"):
                raise library_add.LibraryAddError(
                    "built database failed validation: %s" % report.get("reason"))
            install = await medialib.install_db(final, afc=afc, keep_old=keep_old)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    finally:
        try:
            await lockdown.close()
        except Exception:
            pass

    summary = {
        "added": len(written),
        "folder": folder,
        "device_folder": "/iTunes_Control/Music/%s" % folder,
        "files": written,
        "items": info["items"],
        "artists_created": info["artists_created"],
        "albums_created": info["albums_created"],
        "backup": backup_dir,
        "reboot_required": True,
        "restorable_from": os.path.join(backup_dir, medialib.MANIFEST_NAME),
        "previous_db_kept": install.get("kept_old"),
    }
    for t, r in zip(tracks, info["registered"]):
        t["pid"] = r["pid"]
        t["device_path"] = r["device_path"]
    summary["tracks"] = tracks
    return summary


def _ancestors(path):
    """Every parent directory of ``path``, outermost first."""
    parts = [p for p in path.split("/") if p]
    out = []
    for i in range(len(parts)):
        out.append("/" + "/".join(parts[:i + 1]))
    return out
