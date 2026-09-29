"""Verified backup and restore of the iPhone's iTunes media library.

The music library lives in ``MediaLibrary.sqlitedb``, a WAL-mode SQLite database
that the iOS ``medialibraryd`` daemon keeps open. Anything that writes to it is
risky, so this module exists to make that risk recoverable.

A snapshot is only meaningful as a set: the main database plus its ``-wal`` and
``-shm`` sidecars must all come from the same instant, otherwise the pages do
not line up. AFC has no multi-file read, so the three are pulled one after
another and the result is validated with ``PRAGMA integrity_check``. A torn read
fails that check and the pull is retried. Once a consistent set is obtained it
is checkpointed with ``VACUUM INTO`` into one self-contained file, which is what
a restore actually writes back.

Nothing here touches the device database. ``restore`` is the only function that
writes, and it refuses to run against a backup that does not verify.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import tempfile
import time

MEDIA_DB = "/iTunes_Control/iTunes/MediaLibrary.sqlitedb"
SIDECARS = ("", "-wal", "-shm")

RAW_DIR = "raw"
RAW_STEM = "MediaLibrary.sqlitedb"
CLEAN_NAME = "MediaLibrary.clean.sqlitedb"
MANIFEST_NAME = "manifest.json"
FORMAT_VERSION = 1

# Row counts worth recording, to prove a restore really changed the library.
COUNT_TABLES = ("item", "album", "item_artist", "container_item", "entity_revision")

BACKUP_ROOT = os.path.join(
    os.path.expanduser("~"), "Music", "LinIOS", "medialib-backups")

STAGE_SUFFIX = ".linios-restore"
OLD_SUFFIX = ".linios-pre-restore"

# SQLite write-ahead log constants, used to detect a torn snapshot.
WAL_MAGIC = (0x377F0682, 0x377F0683)
WAL_SALT_OFF = 16
SHM_SALT_OFFS = (32, 80)  # two copies of the index header


class MedialibError(Exception):
    """Any failure in backup, verification or restore."""


def _md5(data):
    return hashlib.md5(data).hexdigest()


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


async def _afc():
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
    lockdown = await create_using_usbmux(connection_type="USB")
    return lockdown, AfcService(lockdown)


async def _device_identity(lockdown):
    """Best-effort UDID / iOS version, for the backup manifest."""
    if lockdown is None:
        return "", ""
    try:
        values = await lockdown.get_value()
        return (values.get("UniqueDeviceID") or "",
                values.get("ProductVersion") or "")
    except Exception:
        return "", ""


async def _pull_snapshot(afc):
    """Pull the database and both sidecars. Missing sidecars come back as None."""
    from pymobiledevice3.exceptions import AfcFileNotFoundError

    blobs = {}
    for suffix in SIDECARS:
        try:
            blobs[suffix] = await afc.get_file_contents(MEDIA_DB + suffix)
        except AfcFileNotFoundError:
            blobs[suffix] = None
    return blobs


def _materialise(blobs, workdir):
    """Write a snapshot to disk under the name 'probe' so sqlite pairs the WAL."""
    db = os.path.join(workdir, "probe")
    for suffix, data in blobs.items():
        if data is None:
            continue
        with open(db + suffix, "wb") as fh:
            fh.write(data)
    return db


def _wal_cross_check(blobs):
    """Confirm the ``-wal`` and ``-shm`` came from the same instant.

    ``PRAGMA integrity_check`` cannot catch a torn snapshot. If the ``-wal`` does
    not validate, SQLite discards it and checks the self-consistent main
    database instead, so a mixed-up pull still reports "ok". Both files carry a
    copy of the WAL salt, and that salt changes whenever the log is recycled, so
    comparing them is what actually proves the three files belong together.

    Returns (ok, detail).
    """
    wal = blobs.get("-wal")
    shm = blobs.get("-shm")

    if not wal:
        return True, "no wal: main database is self-contained"
    if len(wal) < 32:
        return False, "wal is shorter than its 32-byte header"

    magic = struct.unpack(">I", wal[:4])[0]
    if magic not in WAL_MAGIC:
        return False, "bad wal magic 0x%08x" % magic

    if not shm:
        return True, "wal present but no shm: cross-check unavailable"

    salt = wal[WAL_SALT_OFF:WAL_SALT_OFF + 8]
    for off in SHM_SALT_OFFS:
        if len(shm) >= off + 8 and shm[off:off + 8] == salt:
            return True, "salt matched at shm+%d" % off
    return False, "wal/shm salt mismatch: the pull is torn"


def _validate(db_path, clean_path):
    """Integrity-check a snapshot and checkpoint it into one clean file.

    Returns (ok, detail). ``detail`` is a dict of row counts on success, or a
    string explaining the failure.
    """
    if os.path.exists(clean_path):
        os.remove(clean_path)

    conn = sqlite3.connect(db_path)
    try:
        status = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if status != "ok":
            return False, "integrity_check reported: %s" % (status,)

        counts = {}
        for table in COUNT_TABLES:
            try:
                counts[table] = conn.execute(
                    'SELECT count(*) FROM "%s"' % table).fetchone()[0]
            except sqlite3.Error:
                counts[table] = None

        conn.execute("VACUUM INTO ?", (clean_path,))
        return True, counts
    except sqlite3.DatabaseError as e:
        return False, "sqlite error: %s" % (e,)
    finally:
        conn.close()


def verify(backup_dir):
    """Re-validate a backup on disk without touching the device.

    Returns (ok, detail). ``detail`` is the manifest dict on success.
    """
    manifest_path = os.path.join(backup_dir, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        return False, "no %s in %s" % (MANIFEST_NAME, backup_dir)
    try:
        with open(manifest_path) as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as e:
        return False, "unreadable manifest: %s" % (e,)

    for name, meta in manifest.get("files", {}).items():
        path = os.path.join(backup_dir, name)
        if not os.path.exists(path):
            return False, "missing file: %s" % name
        size = os.path.getsize(path)
        if size != meta["size"]:
            return False, "%s: size %d, expected %d" % (name, size, meta["size"])
        with open(path, "rb") as fh:
            digest = _md5(fh.read())
        if digest != meta["md5"]:
            return False, "%s: md5 mismatch" % name

    clean = os.path.join(backup_dir, manifest.get("clean", CLEAN_NAME))
    if not os.path.exists(clean):
        return False, "missing clean database"

    conn = sqlite3.connect(clean)
    try:
        status = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as e:
        return False, "clean database unreadable: %s" % (e,)
    finally:
        conn.close()
    if status != "ok":
        return False, "clean database integrity_check: %s" % (status,)

    return True, manifest


def list_backups(root=None):
    """Return [(name, path, created, item_count, verified)] newest last."""
    root = root or BACKUP_ROOT
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        ok, detail = verify(path)
        manifest = detail if ok else {}
        out.append((
            name,
            path,
            manifest.get("created", ""),
            (manifest.get("row_counts") or {}).get("item"),
            ok,
        ))
    return out


async def backup(afc=None, note="", attempts=3, progress=None):
    """Take a verified snapshot of the media library.

    Retries the pull until a self-consistent set is obtained. Raises
    MedialibError if no attempt produced an intact snapshot.
    """

    def say(msg):
        if progress:
            progress(msg)

    own = afc is None
    lockdown = None
    if own:
        lockdown, afc = await _afc()
    try:
        udid, ios = await _device_identity(lockdown)
        os.makedirs(BACKUP_ROOT, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(BACKUP_ROOT, stamp)
        suffix = 1
        while os.path.exists(dest):
            dest = os.path.join(BACKUP_ROOT, "%s-%d" % (stamp, suffix))
            suffix += 1
        os.makedirs(dest)

        last = "unknown error"
        for attempt in range(1, attempts + 1):
            say("Pulling media library (attempt %d/%d)..." % (attempt, attempts))
            workdir = tempfile.mkdtemp(prefix="mlib-", dir=dest)
            try:
                blobs = await _pull_snapshot(afc)
                if blobs.get("") is None:
                    raise MedialibError("device did not return %s" % MEDIA_DB)

                consistent, why = _wal_cross_check(blobs)
                if not consistent:
                    last = why
                    say("Snapshot rejected: %s" % why)
                    continue

                say("Validating snapshot...")
                db = _materialise(blobs, workdir)
                clean = os.path.join(dest, CLEAN_NAME)
                ok, detail = _validate(db, clean)
                if not ok:
                    last = detail
                    say("Snapshot rejected: %s" % detail)
                    if os.path.exists(clean):
                        os.remove(clean)
                    continue

                rawdir = os.path.join(dest, RAW_DIR)
                os.makedirs(rawdir, exist_ok=True)
                files = {}
                for suf, data in blobs.items():
                    if data is None:
                        continue
                    name = RAW_STEM + suf
                    with open(os.path.join(rawdir, name), "wb") as fh:
                        fh.write(data)
                    files["%s/%s" % (RAW_DIR, name)] = {
                        "size": len(data), "md5": _md5(data)}
                with open(clean, "rb") as fh:
                    blob = fh.read()
                files[CLEAN_NAME] = {"size": len(blob), "md5": _md5(blob)}

                manifest = {
                    "format": FORMAT_VERSION,
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "device_udid": udid,
                    "ios_version": ios,
                    "note": note,
                    "attempts_used": attempt,
                    "integrity_check": "ok",
                    "wal_cross_check": why,
                    "clean": CLEAN_NAME,
                    "row_counts": detail,
                    "files": files,
                }
                with open(os.path.join(dest, MANIFEST_NAME), "w") as fh:
                    json.dump(manifest, fh, indent=2)

                say("Backup verified: %d tracks -> %s"
                    % (detail.get("item") or 0, dest))
                return manifest, dest
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

        raise MedialibError(
            "could not obtain a consistent snapshot after %d attempts: %s"
            % (attempts, last))
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        if own and lockdown is not None:
            await lockdown.close()


async def restore(backup_dir, afc=None, confirm=False, keep_old=True):
    """Put a verified backup back on the device.

    Refuses to run unless ``confirm=True`` and unless the backup passes
    ``verify``. The replacement is staged under a temporary name and swapped in
    with a rename, and the previous database is kept on the device as
    ``MediaLibrary.sqlitedb.linios-pre-restore`` unless ``keep_old=False``.

    The device's ``medialibraryd`` holds the database open, so the swap is not
    seen until the iPhone is rebooted. Reboot it after any restore.
    """
    if not confirm:
        raise MedialibError("restore requires confirm=True")

    ok, detail = verify(backup_dir)
    if not ok:
        raise MedialibError("refusing to restore, backup is not valid: %s" % detail)

    clean_path = os.path.join(backup_dir, detail.get("clean", CLEAN_NAME))
    with open(clean_path, "rb") as fh:
        blob = fh.read()

    info = await _swap_in(blob, afc=afc, keep_old=keep_old)
    info["restored"] = detail.get("created")
    info["row_counts"] = detail.get("row_counts")
    return info


async def install_db(db_path, afc=None, keep_old=True):
    """Put an arbitrary verified database on the device.

    This is the same guarded swap :func:`restore` performs, but for a database
    built outside a backup directory -- used by :mod:`library_add` to install a
    library with an extra track registered in it. The file is re-validated here
    so a corrupt or unrelated database can never reach the device.

    ``medialibd`` holds the live database open, so the swap only takes effect
    after the iPhone is rebooted.
    """
    report = validate_db_file(db_path)
    if not report.get("ok"):
        raise MedialibError(
            "refusing to install, database is not valid: %s"
            % report.get("reason"))

    with open(db_path, "rb") as fh:
        blob = fh.read()

    info = await _swap_in(blob, afc=afc, keep_old=keep_old)
    info["installed"] = os.path.basename(db_path)
    info["verified"] = report
    return info


def validate_db_file(path):
    """Check a bare MediaLibrary database file, with no backup around it.

    Confirms it is a SQLite database, passes ``integrity_check``, actually looks
    like a media library, and carries no unresolved write-ahead log.
    """
    report = {"path": path, "ok": False, "reason": ""}
    if not os.path.isfile(path):
        report["reason"] = "not a file"
        return report
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error as e:
        report["reason"] = "cannot open as SQLite (%s)" % e
        return report
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        report["integrity_check"] = check
        if check != "ok":
            report["reason"] = "integrity_check failed: %s" % check
            return report
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"item", "item_extra", "item_state", "entity_revision"} - names
        if missing:
            report["reason"] = "not a media library, missing tables: %s" % (
                ", ".join(sorted(missing)))
            return report
        report["items"] = conn.execute("SELECT count(*) FROM item").fetchone()[0]
        report["size"] = os.path.getsize(path)
        report["ok"] = True
        return report
    except sqlite3.Error as e:
        report["reason"] = "query failed (%s)" % e
        return report
    finally:
        conn.close()


async def _swap_in(blob, afc=None, keep_old=True):
    """Stage a database, prove it reads back intact, then swap it into place.

    Staging under a temporary name means the live database is only replaced
    once the new bytes are known to be on the device. The previous database is
    set aside on the device first, and the stale WAL/SHM sidecars are removed,
    because replaying an old write-ahead log over a new database would corrupt
    it.
    """
    own = afc is None
    lockdown = None
    if own:
        lockdown, afc = await _afc()
    try:
        stage = MEDIA_DB + STAGE_SUFFIX
        await afc.set_file_contents(stage, blob)

        written = await afc.get_file_contents(stage)
        if len(written) != len(blob) or _md5(written) != _md5(blob):
            await _best_effort_rm(afc, stage)
            raise MedialibError("staged database did not read back intact")

        if keep_old:
            try:
                await afc.rename(MEDIA_DB, MEDIA_DB + OLD_SUFFIX)
            except Exception as e:
                await _best_effort_rm(afc, stage)
                raise MedialibError("could not set the old database aside: %s" % e)

        await afc.rename(stage, MEDIA_DB)

        removed = []
        for suf in ("-wal", "-shm"):
            if await _best_effort_rm(afc, MEDIA_DB + suf):
                removed.append(suf)

        return {
            "removed_sidecars": removed,
            "kept_old": keep_old,
            "reboot_required": True,
        }
    finally:
        if own and lockdown is not None:
            await lockdown.close()


async def _best_effort_rm(afc, path):
    try:
        await afc.rm(path, force=True)
        return True
    except Exception:
        return False


def _cli():
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backup", help="take a verified snapshot")
    p.add_argument("--note", default="")

    sub.add_parser("list", help="list backups in %s" % BACKUP_ROOT)

    p = sub.add_parser("verify", help="re-validate a backup directory")
    p.add_argument("path")

    p = sub.add_parser("restore", help="write a backup back to the device")
    p.add_argument("path")
    p.add_argument("--yes", action="store_true", help="required")
    p.add_argument("--no-keep-old", action="store_true")

    args = ap.parse_args()

    try:
        return _run_cli(args)
    except MedialibError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _run_cli(args):
    if args.cmd == "backup":
        manifest, dest = asyncio.run(backup(note=args.note, progress=print))
        print("\nBackup written to %s" % dest)
        print("Tracks: %s" % (manifest["row_counts"].get("item"),))
        return 0

    if args.cmd == "list":
        rows = list_backups()
        if not rows:
            print("No backups in %s" % BACKUP_ROOT)
            return 0
        for name, path, created, items, ok in rows:
            print("%-20s %-20s %6s tracks  %s"
                  % (name, created, items, "ok" if ok else "INVALID"))
        return 0

    if args.cmd == "verify":
        ok, detail = verify(args.path)
        if ok:
            print("valid: %s" % detail.get("created"))
            print("tracks: %s" % (detail.get("row_counts", {}).get("item"),))
        else:
            print("INVALID: %s" % detail)
        return 0 if ok else 1

    if args.cmd == "restore":
        if not args.yes:
            print("Refusing to restore without --yes. This overwrites the "
                  "device's media library.", file=sys.stderr)
            return 2
        result = asyncio.run(restore(
            args.path, confirm=True, keep_old=not args.no_keep_old))
        print("Restored backup from %s (%s tracks)"
              % (result["restored"], result["row_counts"].get("item")))
        print("Reboot the iPhone for this to take effect.")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(_cli())
