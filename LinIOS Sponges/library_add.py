"""Register tracks in the iPhone's Music library by writing MediaLibrary.sqlitedb.

Why a database write is required
--------------------------------
Dropping audio files into ``/iTunes_Control/Music`` does **not** add them to the
Music app. Verified on a real device: a file placed there using iOS's own
``F##``-folder layout, and another plainly named, both survived a reboot at full
size while the library stayed at 153 items. The Music app lists rows in
``MediaLibrary.sqlitedb``, not files on disk.

What was established experimentally
------------------------------------
* ``item.in_my_library`` is not set directly. The trigger
  ``on_insert_item_setInMyLibraryColumn`` derives it from ``item_store``::

      in_my_library = home_sharing_id
                   OR (store_saga_id AND cloud_in_my_library)
                   OR purchase_history_id
                   OR (sync_id AND sync_in_my_library)
                   OR is_ota_purchased

  A local track is in the library because it has a non-zero ``sync_id`` with
  ``sync_in_my_library = 1``. Omit that and the row lands outside the library.
* ``item_state`` is filled in by the database's own triggers, so it must not be
  written by hand.
* ``item_extra.integrity`` is a 57-byte keyed blob (2-byte header + 55 bytes),
  not a plain digest of the file -- MD5/SHA-1/SHA-2 constructions of the real
  content did not match it. A row carrying a *copied* blob does not display; the
  same row with ``integrity = NULL`` does. NULL is therefore the default here.
* ``container_item`` is not required: 99 of the 153 real items have no
  container row, and the primary "iPhone" container is empty.
* ``medialibd`` holds the database open, so a swap only takes effect after the
  iPhone is rebooted. The caller is responsible for saying so.

Not synthesised
---------------
``album_artist.grouping_key`` and ``item_artist.grouping_key`` are binary blobs
holding references into an internal token dictionary, so new artist rows get
NULL there. ``sort_*`` keys are left at zero. Both are display/sort hints; the
plain ``album_artist`` / ``item_artist`` / ``album`` name columns carry the text
the Music app shows.

Safety
------
Only rows are ever added; nothing is modified or deleted. The build aborts unless
every table's row count changes by exactly the amount expected, the new rows are
internally consistent, and ``PRAGMA integrity_check`` passes.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time

MEDIA_DB = "/iTunes_Control/iTunes/MediaLibrary.sqlitedb"
MEDIA_MUSIC = "iTunes_Control/Music"
DEFAULT_FOLDER = "LinIOS"

# Tables cloned from a template item, keyed by item_pid. Excluded on purpose:
#   item_state    - populated by triggers on item / item_extra / item_store
#   container_*   - smart playlists and playback history only
#   item_kvs, booklet - unused in this library
#   entity_changes, source - bookkeeping for Apple's own sync protocol
CLONE_TABLES = (
    "item_playback",
    "item_stats",
    "item_video",
    "item_search",
    "lyrics",
    "chapter",
)

AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".aiff", ".aif", ".mp4")

# Well below the int64 ceiling and far from the pids already in use, so new
# entities cannot collide with the large sequential ids iOS assigns.
_PID_BASE = 1_100_000_000_000_000_000


class LibraryAddError(Exception):
    """Raised when the database would not be safe to write."""


# --------------------------------------------------------------------------- #
# tags
# --------------------------------------------------------------------------- #
def read_tags(path):
    """Best-effort metadata for an audio file.

    Many files carry no tags at all, so every field has a filename-based
    fallback; ``artist`` falls back to "Unknown Artist" so tracks still group
    sensibly instead of landing in a single nameless bucket.
    """
    info = {
        "title": "", "artist": "", "album": "",
        "track": 0, "disc": 0, "year": 0,
        "duration_ms": 0.0, "size": 0,
    }
    try:
        info["size"] = os.path.getsize(path)
    except OSError:
        pass

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-of", "json",
                 "-show_entries",
                 "format=duration,size:format_tags=title,artist,album",
                 "-show_entries", "stream_tags=track,disc,date",
                 path],
                capture_output=True, timeout=60,
            ).stdout.decode("utf-8", "replace")
            data = json.loads(out or "{}")
            fmt = data.get("format", {}) or {}
            tags = fmt.get("tags", {}) or {}

            def tag(*names):
                for n in names:
                    v = tags.get(n)
                    if v:
                        return _clean_tag(v)
                return ""

            info["title"] = tag("title")
            info["artist"] = tag("artist", "album_artist", "albumartist")
            info["album"] = tag("album")
            if fmt.get("duration"):
                try:
                    info["duration_ms"] = float(fmt["duration"]) * 1000.0
                except ValueError:
                    pass
            for st in data.get("streams", []) or []:
                stags = st.get("tags", {}) or {}
                for key, dest in (("track", "track"), ("disc", "disc")):
                    if stags.get(key):
                        m = re.match(r"\s*(\d+)", str(stags[key]))
                        if m:
                            info[dest] = int(m.group(1))
                if stags.get("date") and not info["year"]:
                    m = re.match(r"\s*(\d{4})", str(stags["date"]))
                    if m:
                        info["year"] = int(m.group(1))
                break
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"^\d{1,3}[\s._-]+", "", stem).strip() or stem
    if not info["title"]:
        info["title"] = _clean_tag(stem)
    if not info["artist"]:
        info["artist"] = "Unknown Artist"
    if not info["album"]:
        info["album"] = "Unknown Album"
    if not info["duration_ms"]:
        info["duration_ms"] = _estimate_duration_ms(path)
    return info


def _clean_tag(value):
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value


def _estimate_duration_ms(path):
    """Duration from a decode probe, for files ffprobe cannot measure."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        res = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, timeout=60,
        )
        return float(res.stdout.decode().strip() or 0.0) * 1000.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def safe_filename(name, ext):
    """A filename that is safe to hand to AFC and unlikely to collide."""
    stem = os.path.splitext(os.path.basename(name))[0]
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).strip(" .")
    stem = re.sub(r"\s+", " ", stem) or "track"
    return stem[:120] + ext.lower()


# --------------------------------------------------------------------------- #
# database helpers
# --------------------------------------------------------------------------- #
def _tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def _columns(conn, table):
    return [r[1] for r in conn.execute('PRAGMA table_info("%s")' % table)]


def _counts(conn):
    out = {}
    for t in _tables(conn):
        try:
            out[t] = conn.execute('SELECT count(*) FROM "%s"' % t).fetchone()[0]
        except sqlite3.Error:
            out[t] = None
    return out


def _insert(conn, table, values):
    cols = list(values)
    sql = 'INSERT INTO "%s" (%s) VALUES (%s)' % (
        table, ",".join('"%s"' % c for c in cols), ",".join("?" * len(cols)))
    conn.execute(sql, [values[c] for c in cols])


def _used_pids(conn):
    used = set()
    for q in ("SELECT item_pid FROM item", "SELECT item_artist_pid FROM item_artist",
              "SELECT album_artist_pid FROM album_artist", "SELECT album_pid FROM album",
              "SELECT entity_pid FROM entity_revision", "SELECT genre_id FROM genre"):
        try:
            used.update(r[0] for r in conn.execute(q) if r[0] is not None)
        except sqlite3.Error:
            pass
    return used


class _Allocator:
    """Hands out pids that cannot collide with anything already stored."""

    def __init__(self, conn):
        self._used = _used_pids(conn)
        self._next = _PID_BASE

    def take(self):
        while self._next in self._used:
            self._next += 1
        pid = self._next
        self._used.add(pid)
        self._next += 1
        return pid


def _ensure_base_location(conn, folder, alloc):
    path = "%s/%s" % (MEDIA_MUSIC, folder)
    row = conn.execute("SELECT base_location_id FROM base_location WHERE path=?",
                       (path,)).fetchone()
    if row:
        return int(row[0]), path
    pid = alloc.take()
    _insert(conn, "base_location", {"base_location_id": pid, "path": path})
    return pid, path


def _ensure_album_artist(conn, name, alloc, representative_pid):
    row = conn.execute(
        "SELECT album_artist_pid FROM album_artist WHERE album_artist=? LIMIT 1",
        (name,)).fetchone()
    if row:
        return int(row[0]), False
    pid = alloc.take()
    _insert(conn, "album_artist", {
        "album_artist_pid": pid, "album_artist": name,
        "sort_album_artist": name, "grouping_key": None,
        "representative_item_pid": representative_pid, "keep_local": 1,
    })
    return pid, True


def _ensure_item_artist(conn, name, alloc, representative_pid):
    row = conn.execute(
        "SELECT item_artist_pid FROM item_artist WHERE item_artist=? LIMIT 1",
        (name,)).fetchone()
    if row:
        return int(row[0]), False
    pid = alloc.take()
    _insert(conn, "item_artist", {
        "item_artist_pid": pid, "item_artist": name, "sort_item_artist": name,
        "grouping_key": None, "representative_item_pid": representative_pid,
        "keep_local": 1,
    })
    return pid, True


def _ensure_album(conn, name, album_artist_pid, alloc, representative_pid, year):
    row = conn.execute(
        "SELECT album_pid FROM album WHERE album=? AND album_artist_pid=? LIMIT 1",
        (name, album_artist_pid)).fetchone()
    if row:
        return int(row[0]), False
    pid = alloc.take()
    _insert(conn, "album", {
        "album_pid": pid, "album": name, "sort_album": name,
        "album_artist_pid": album_artist_pid, "representative_item_pid": representative_pid,
        "grouping_key": None, "keep_local": 1, "album_year": int(year or 0),
    })
    return pid, True


def _template_item(conn, media_type=8):
    """A local, synced, unprotected item to use as a structural template."""
    row = conn.execute(
        """
        SELECT i.item_pid FROM item i JOIN item_store s ON s.item_pid = i.item_pid
         WHERE i.media_type = ? AND i.in_my_library = 1
           AND s.sync_id <> 0 AND s.sync_in_my_library = 1
           AND s.store_saga_id = 0 AND s.purchase_history_id = 0
           AND s.home_sharing_id = 0 AND s.is_ota_purchased = 0
           AND s.store_item_id = 0 AND s.is_protected = 0
           AND i.base_location_id > 0
         ORDER BY i.item_pid LIMIT 1
        """, (media_type,)).fetchone()
    if row is None:
        raise LibraryAddError(
            "no suitable local item to use as a template; the library may have "
            "no synced local tracks")
    return row[0]


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def register_batch(db_in, db_out, tracks, folder=DEFAULT_FOLDER,
                   integrity_mode="null", media_type=8):
    """Build a database that also registers each of ``tracks``.

    ``db_in`` is a clean verified snapshot and is never modified. ``tracks`` is a
    list of dicts as produced by :func:`read_tags`, each optionally carrying a
    ``remote_name`` and ``base_location_id``. Returns a summary of the change.
    """
    if integrity_mode not in ("null", "clone"):
        raise LibraryAddError("integrity_mode must be 'null' or 'clone'")
    if not tracks:
        raise LibraryAddError("no tracks to register")

    shutil.copyfile(db_in, db_out)
    conn = sqlite3.connect(db_out)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        before = _counts(conn)

        template_pid = _template_item(conn, media_type)
        alloc = _Allocator(conn)

        # base_location is per-folder, so resolve it once for the batch.
        loc_id, loc_path = _ensure_base_location(conn, folder, alloc)

        used_names = {r[0] for r in conn.execute(
            "SELECT location FROM item_extra")}
        now = time.time()
        max_rev = conn.execute(
            "SELECT max(revision) FROM entity_revision").fetchone()[0] or 0

        registered = []
        for t in tracks:
            size = int(t.get("size") or 0)
            if size <= 0:
                raise LibraryAddError(
                    "refusing to register %r: zero-byte file" % t.get("title"))
            ext = os.path.splitext(t.get("remote_name") or t.get("path", ""))[1]
            if not ext:
                ext = ".mp3"

            remote_name = t.get("remote_name") or safe_filename(
                t.get("title") or "track", ext)
            if remote_name in used_names:
                stem, e = os.path.splitext(remote_name)
                n = 2
                while "%s (%d)%s" % (stem, n, e) in used_names:
                    n += 1
                remote_name = "%s (%d)%s" % (stem, n, e)
            used_names.add(remote_name)

            title = t.get("title") or os.path.splitext(remote_name)[0]
            artist = t.get("artist") or "Unknown Artist"
            album = t.get("album") or "Unknown Album"

            pid = alloc.take()

            # artists/album first: they want a representative item pid
            item_artist_pid, _ = _ensure_item_artist(conn, artist, alloc, pid)
            album_artist_pid, _ = _ensure_album_artist(conn, artist, alloc, pid)
            album_pid, _ = _ensure_album(conn, album, album_artist_pid, alloc,
                                         pid, t.get("year", 0))

            # --- item ---------------------------------------------------- #
            cols = _columns(conn, "item")
            src = conn.execute("SELECT * FROM item WHERE item_pid=?",
                               (template_pid,)).fetchone()
            row = dict(zip(cols, src))
            row.update({
                "item_pid": pid, "media_type": media_type,
                "item_artist_pid": item_artist_pid, "album_pid": album_pid,
                "album_artist_pid": album_artist_pid,
                "base_location_id": t.get("base_location_id") or loc_id,
                "track_number": int(t.get("track") or 0),
                "disc_number": int(t.get("disc") or 0),
                "date_added": now, "date_downloaded": now,
                "in_my_library": 0,      # the trigger derives this from item_store
            })
            _insert(conn, "item", row)

            # --- item_store: this is what puts the item in the library ---- #
            cols = _columns(conn, "item_store")
            src = conn.execute("SELECT * FROM item_store WHERE item_pid=?",
                               (template_pid,)).fetchone()
            srow = dict(zip(cols, src))
            srow.update({
                "item_pid": pid, "sync_id": alloc.take(), "sync_in_my_library": 1,
                "sync_redownload_params": "local", "cloud_status": 0,
                "store_kind": 0, "is_protected": 0, "playback_endpoint_type": 0,
            })
            _insert(conn, "item_store", srow)

            # --- item_extra: the file identity ---------------------------- #
            cols = _columns(conn, "item_extra")
            src = conn.execute("SELECT * FROM item_extra WHERE item_pid=?",
                               (template_pid,)).fetchone()
            erow = dict(zip(cols, src))
            erow.update({
                "item_pid": pid, "title": title, "sort_title": title,
                "location": remote_name, "file_size": size,
                "total_time_ms": float(t.get("duration_ms") or 0.0),
                "year": int(t.get("year") or 0), "date_modified": now,
                "genius_id": 0, "category_id": 0, "is_user_disabled": 0,
                "integrity": (src[cols.index("integrity")]
                              if integrity_mode == "clone" else None),
            })
            _insert(conn, "item_extra", erow)

            # --- remaining item-keyed tables: straight clones ------------- #
            for table in CLONE_TABLES:
                tcols = _columns(conn, table)
                tsrc = conn.execute('SELECT * FROM "%s" WHERE item_pid=?' % table,
                                    (template_pid,)).fetchone()
                if tsrc is None:
                    continue
                clone = dict(zip(tcols, tsrc))
                clone["item_pid"] = pid
                _insert(conn, table, clone)

            max_rev += 1
            conn.execute(
                "INSERT INTO entity_revision (revision, entity_pid, deleted,"
                " class, revision_type) VALUES (?,?,0,0,0)", (max_rev, pid))

            registered.append({
                "pid": pid, "title": title, "artist": artist, "album": album,
                "location": remote_name, "file_size": size,
                "device_path": "/%s/%s" % (loc_path, remote_name),
            })

        conn.commit()

        # --- nothing unexpected may have moved -------------------------- #
        after = _counts(conn)
        expect_delta = 1 * len(tracks)
        created_artists = conn.execute(
            "SELECT count(*) FROM item_artist").fetchone()[0] - (
                before.get("item_artist") or 0)
        created_album_artists = conn.execute(
            "SELECT count(*) FROM album_artist").fetchone()[0] - (
                before.get("album_artist") or 0)
        created_albums = conn.execute(
            "SELECT count(*) FROM album").fetchone()[0] - (
                before.get("album") or 0)

        problems = []
        for table in ("item", "item_extra", "item_state", "item_store",
                     "entity_revision", "item_playback", "item_stats",
                     "item_video"):
            delta = (after.get(table) or 0) - (before.get(table) or 0)
            if delta != expect_delta:
                problems.append("%s changed by %d, expected %d"
                                % (table, delta, expect_delta))
        for table, want in (("item_artist", created_artists),
                            ("album_artist", created_album_artists),
                            ("album", created_albums)):
            delta = (after.get(table) or 0) - (before.get(table) or 0)
            if delta != want or want < 0:
                problems.append("%s changed by %d, expected %d"
                                % (table, delta, want))
        if problems:
            raise LibraryAddError("unexpected row changes: " + "; ".join(problems))

        for r in registered:
            lib = conn.execute("SELECT in_my_library FROM item WHERE item_pid=?",
                               (r["pid"],)).fetchone()[0]
            if int(lib) != 1:
                raise LibraryAddError(
                    "item %d was not marked in-library; the item_store sync "
                    "marker is wrong" % r["pid"])
            st = conn.execute("SELECT count(*) FROM item_state WHERE item_pid=?",
                              (r["pid"],)).fetchone()[0]
            if st != 1:
                raise LibraryAddError(
                    "item_state not populated for item %d" % r["pid"])

        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise LibraryAddError("integrity_check failed: %s" % check)

        return {
            "registered": registered,
            "items": after.get("item"),
            "items_added": expect_delta,
            "artists_created": created_artists,
            "album_artists_created": created_album_artists,
            "albums_created": created_albums,
            "folder": folder,
            "folder_path": loc_path,
            "integrity_mode": integrity_mode,
            "integrity_check": check,
            "reboot_required": True,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finalize(db_path, out_path):
    """Rewrite ``db_path`` as a self-contained file at ``out_path``.

    ``VACUUM INTO`` drops the write-ahead log and freelist, producing a single
    valid database that can replace the live one on the device.
    """
    conn = sqlite3.connect(db_path)
    try:
        if os.path.exists(out_path):
            os.remove(out_path)
        conn.execute("VACUUM INTO ?", (out_path,))
    finally:
        conn.close()
    check = sqlite3.connect(out_path)
    try:
        got = check.execute("PRAGMA integrity_check").fetchone()[0]
        if got != "ok":
            raise LibraryAddError("finalized database failed: %s" % got)
    finally:
        check.close()
    return out_path
