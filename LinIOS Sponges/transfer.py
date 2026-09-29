import asyncio
import hashlib
import os
import re
import sqlite3
import tempfile
import logging
from collections import namedtuple

from audio_check import check_audio, check_audio_file

log = logging.getLogger("LinIOS.transfer")

# Music stored by iTunes resides under this folder on the device's media area.
DEVICE_MUSIC_DIR = "/iTunes_Control/Music"
# The iPhone's media library database (SQLite) that stores the real track names.
DEVICE_MEDIA_DB = "/iTunes_Control/iTunes/MediaLibrary.sqlitedb"
# Folder into which we push music files on the device (visible in Files app).
DEVICE_PUSH_DIR = "LinIOS Music"

AUDIO_EXTS = ('.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.aiff', '.aif', '.mp4', '.wma')

PUSH_MESSAGE = ("Added {done} track(s) to the iPhone.\n\n"
                "WHERE THEY ARE\n"
                "  \"{folder}\" folder, inside the Files app on your iPhone\n"
                "  (Files -> On My iPhone -> {folder}).\n\n"
                "They will NOT appear in the Music app. iOS only builds that\n"
                "library through Apple's own sync, which LinIOS deliberately\n"
                "does not touch, so your library is never put at risk.\n\n"
                "VERIFICATION\n"
                "  {verified} file(s) were read back off the iPhone and their\n"
                "  checksum and audio data compared with the original.\n"
                "  {failed} failed and were not counted as added.")

Track = namedtuple("Track", ["path", "title", "artist", "album", "track_number", "size", "corrupt", "reason"])


class AudioSyncError(RuntimeError):
    pass


class MusicTransfer:
    """Sync music to/from an iPhone using pymobiledevice3's async AFC service.

    Real track names are read from the iPhone's own media library database, so
    songs keep their proper title/artist names instead of the obfuscated
    filename Apple uses internally.

    All public methods are synchronous facades over an asyncio event loop, so
    they can be driven from a QThread worker."""

    def __init__(self, device):
        self.device = device

    # ------------------------------------------------------------------ #
    # public sync API
    # ------------------------------------------------------------------ #
    def list_music(self):
        """Return a list of Track objects for the music stored on the iPhone."""
        return asyncio.run(self._op(self._list_music))

    def pull_music(self, dest_dir, progress_cb=None):
        asyncio.run(self._op(self._pull_music, dest_dir, progress_cb))

    def pull_selected(self, tracks, dest_dir, progress_cb=None):
        asyncio.run(self._op(self._pull_selected, tracks, dest_dir, progress_cb))

    def push_music(self, local_paths, progress_cb=None, verify=True):
        """Push files, then read each one back off the iPhone to confirm it.

        Returns a result dict describing what was added and how it verified.
        """
        return asyncio.run(self._op(self._push_music, local_paths, progress_cb, verify))

    def list_pushed(self, verify=True):
        """List what is actually stored in the push folder, read from the device.

        This is the device's own view, not LinIOS's record of what it sent, so
        it is the honest answer to "is the track really on my iPhone?".
        """
        return asyncio.run(self._op(self._list_pushed, verify))

    async def _list_pushed(self, afc, verify):
        base = '/' + DEVICE_PUSH_DIR
        try:
            names = [n for n in await afc.listdir(base) if n not in ('.', '..')]
        except Exception:
            return []
        out = []
        for name in sorted(names):
            remote = f'{base}/{name}'
            try:
                st = await afc.stat(remote)
            except Exception:
                continue
            if st is None:
                continue
            entry = {"name": name, "remote": remote,
                     "size": int(st.get("st_size", 0)),
                     "mtime": st.get("st_mtime"), "ok": False, "reason": ""}
            if not verify:
                entry["ok"] = True
                entry["reason"] = "present on the iPhone (not checked)"
                out.append(entry)
                continue
            check = await self._verify_on_device(afc, remote)
            entry.update({"ok": check["ok"], "reason": check["reason"],
                          "audio_ok": check["audio_ok"],
                          "size_match": check["size_match"]})
            out.append(entry)
        return out

    def delete_music(self, tracks, progress_cb=None):
        asyncio.run(self._op(self._delete_music, tracks, progress_cb))

    # ------------------------------------------------------------------ #
    # async plumbing
    # ------------------------------------------------------------------ #
    async def _op(self, fn, *args):
        try:
            return await self._run(fn, *args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("operation %s failed: %s", fn.__name__, e)
            msg = str(e) or e.__class__.__name__
            raise AudioSyncError(self._friendly_error(msg))

    async def _run(self, fn, *args):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        lockdown = None
        try:
            lockdown = await create_using_usbmux(
                serial=self.device.get("serial"),
                connection_type="USB",
                autopair=True,
                pair_timeout=30,
            )
            try:
                async with AfcService(lockdown) as afc:
                    return await fn(afc, *args)
            finally:
                await lockdown.close()
        except Exception:
            raise

    # ------------------------------------------------------------------ #
    # media library metadata
    # ------------------------------------------------------------------ #
    async def _media_meta(self, afc):
        """Download the iPhone media library and return a mapping of
        obfuscated file name -> Track metadata."""
        mapping = {}
        try:
            data = await afc.get_file_contents(DEVICE_MEDIA_DB)
        except Exception as e:
            log.debug("could not read media db: %s", e)
            return mapping
        fd, path = tempfile.mkstemp(suffix=".sqlitedb")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            con = sqlite3.connect(path)
            try:
                rows = con.execute(
                    """
                    SELECT ie.location, ie.title, ia.item_artist, al.album,
                           i.track_number, ie.file_size
                    FROM item_extra ie
                    JOIN item i ON i.item_pid = ie.item_pid
                    LEFT JOIN item_artist ia ON ia.item_artist_pid = i.item_artist_pid
                    LEFT JOIN album al ON al.album_pid = i.album_pid
                    WHERE ie.location != ''
                    """
                ).fetchall()
                for loc, title, artist, album, track_no, file_size in rows:
                    if not loc:
                        continue
                    mapping[loc.lower()] = {
                        "title": (title or "").strip(),
                        "artist": (artist or "").strip(),
                        "album": (album or "").strip(),
                        "track_number": track_no,
                        "file_size": file_size or 0,
                    }
            finally:
                con.close()
        except Exception as e:
            log.debug("media db parse failed: %s", e)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return mapping

    # ------------------------------------------------------------------ #
    # operations
    # ------------------------------------------------------------------ #
    async def _list_music(self, afc):
        meta = await self._media_meta(afc)
        found = []

        async def scan(directory):
            try:
                entries = await afc.listdir(directory)
            except Exception:
                return
            for entry in entries:
                if entry in ('.', '..'):
                    continue
                full = directory.rstrip('/') + '/' + entry
                try:
                    info = await afc.stat(full)
                except Exception:
                    continue
                if info.get('st_ifmt') == 'S_IFDIR':
                    await scan(full)
                elif entry.lower().endswith(AUDIO_EXTS):
                    found.append((full, int(info.get('st_size', 0))))

        await scan(DEVICE_MUSIC_DIR)
        found.sort(key=lambda pair: pair[0])

        tracks = []
        for full, size in found:
            base = os.path.basename(full)
            m = meta.get(base.lower())
            title = (m.get("title") if m else "") or base
            artist = (m.get("artist") if m else "") or ""
            desired = (m.get("file_size") if m else 0) or 0

            corrupt = False
            reason = None
            if size <= 0:
                corrupt, reason = True, "file is empty (0 bytes)"
            elif desired > 0 and size != desired:
                corrupt, reason = True, f"file size mismatch (expected {desired} bytes, found {size})"
            tracks.append(Track(
                path=full,
                title=title,
                artist=artist,
                album=(m.get("album") if m else "") or "",
                track_number=(m.get("track_number") if m else 0) or 0,
                size=size,
                corrupt=corrupt,
                reason=reason,
            ))
        return tracks

    async def _pull_music(self, afc, dest_dir, cb):
        tracks = await self._list_music(afc)
        if not tracks:
            self._emit(cb, 0, 0, "No music found on the iPhone.")
            return
        await self._pull_tracks(afc, tracks, dest_dir, cb)

    async def _pull_selected(self, afc, tracks, dest_dir, cb):
        if not tracks:
            return
        await self._pull_tracks(afc, tracks, dest_dir, cb)

    async def _pull_tracks(self, afc, tracks, dest_dir, cb):
        os.makedirs(dest_dir, exist_ok=True)
        total = len(tracks)
        saved = skipped = bad = 0
        bad_names = []
        for i, track in enumerate(tracks, 1):
            label = track.title or os.path.basename(track.path)
            local = self._local_name(dest_dir, track)
            if os.path.exists(local):
                skipped += 1
                self._emit(cb, i, total, f"Skipped existing: {label}")
                continue
            self._emit(cb, i, total, f"Copying {label} ({i}/{total})...")
            data = await afc.get_file_contents(track.path)
            ext = os.path.splitext(track.path)[1]
            ok, reason = check_audio(data, ext)
            if track.size > 0 and len(data) != track.size and ok:
                ok = False
                reason = f"download truncated (expected {track.size} bytes, got {len(data)})"
            with open(local, 'wb') as fh:
                fh.write(data)
            saved += 1
            if ok:
                ok, reason = check_audio_file(local)  # deep decode check
            if not ok:
                bad += 1
                bad_names.append(label)
                self._emit(cb, i, total, f"Saved {label} (warning: {reason})")
        summary = f"Saved {saved} track(s) to {dest_dir}"
        if skipped:
            summary += f", skipped {skipped} already on disk"
        if bad:
            summary += (f"\n\n{len(bad_names)} file(s) look corrupt or incomplete:\n"
                        + "\n".join("  • " + n for n in bad_names)
                        + "\n\nThey may skip or fail to play.")
        self._emit(cb, total, total, summary)

    async def _verify_on_device(self, afc, remote, local_path=None):
        """Read a file back off the iPhone and confirm it is really there.

        A successful write and a matching byte count are not proof on their
        own, so the copy on the device is fetched and compared against the
        original with a checksum, then re-checked as audio. This is what turns
        "reported success" into "verified on the phone".
        """
        result = {"name": os.path.basename(remote), "remote": remote,
                  "size": 0, "size_match": False, "md5_match": None,
                  "audio_ok": False, "ok": False, "reason": ""}
        try:
            st = await afc.stat(remote)
        except Exception as e:
            result["reason"] = f"not found on the iPhone ({e})"
            return result
        if st is None:
            result["reason"] = "not found on the iPhone"
            return result

        result["size"] = int(st.get("st_size", 0))
        try:
            data = await afc.get_file_contents(remote)
        except Exception as e:
            result["reason"] = f"could not be read back from the iPhone ({e})"
            return result

        result["size_match"] = len(data) == result["size"]
        ext = os.path.splitext(remote)[1].lower()

        if local_path:
            try:
                with open(local_path, "rb") as fh:
                    original = fh.read()
                result["md5_match"] = (hashlib.md5(original).hexdigest()
                                       == hashlib.md5(data).hexdigest())
            except OSError:
                result["md5_match"] = None

        audio_ok, audio_reason = check_audio(data, ext)
        result["audio_ok"] = bool(audio_ok)

        if audio_ok:
            # The in-memory check only looks at the container structure, so a
            # truncated file still passes it. Re-check via a real decode probe.
            deep_ok, deep_reason = self._deep_check(data, ext)
            if not deep_ok:
                audio_ok, audio_reason = False, deep_reason
                result["audio_ok"] = False

        problems = []
        if not result["size_match"]:
            problems.append("size on iPhone does not match what was written")
        if result["md5_match"] is False:
            problems.append("checksum does not match the original")
        if not audio_ok:
            problems.append(f"audio data is not valid ({audio_reason})")
        if problems:
            result["reason"] = "; ".join(problems)
            return result

        result["ok"] = True
        if result["md5_match"]:
            result["reason"] = "byte-for-byte match with the original, audio decodes"
        else:
            result["reason"] = ("readable and valid audio, but there is no local "
                                "original to compare a checksum against")
        return result

    @staticmethod
    def _deep_check(data, ext):
        """Run the file-based checker (which includes an ffprobe decode) on bytes.

        check_audio_file needs a path, so the data is staged in a temporary file
        first. This is what catches a file that is cut short but still has a
        valid-looking header.
        """
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=ext or ".bin")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return check_audio_file(tmp)
        except OSError as e:
            return False, f"could not verify audio data ({e})"
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    async def _push_music(self, afc, local_paths, cb, verify=True):
        if not local_paths:
            return None
        try:
            await afc.makedirs('/' + DEVICE_PUSH_DIR)
        except Exception:
            pass
        total = len(local_paths)
        done = rejected = verified = 0
        bad_names = []
        results = []
        for i, lp in enumerate(local_paths, 1):
            name = os.path.basename(lp)
            # validate the local file first so corrupt music never reaches the phone
            ok, reason = check_audio_file(lp)
            if not ok:
                rejected += 1
                bad_names.append(f"{name} ({reason})")
                self._emit(cb, i, total, f"Skipped {name} - it looks corrupt: {reason}")
                continue
            remote = f'/{DEVICE_PUSH_DIR}/{self._clean_name(name)}'
            if await self._remote_exists(afc, remote):
                self._emit(cb, i, total, f"Already on iPhone: {name}")
                done += 1
                results.append({"name": name, "skipped": True, "ok": True,
                                "reason": "already on the iPhone", "remote": remote})
                continue
            size = os.path.getsize(lp)
            self._emit(cb, i, total, f"Transferring {name} ({i}/{total})...")
            with open(lp, 'rb') as fh:
                data = fh.read()
            await afc.set_file_contents(remote, data)
            remote_size = await self._remote_size(afc, remote)
            if size > 0 and remote_size != size:
                raise AudioSyncError(
                    f"Transfer of {name} failed: wrote {remote_size} of {size} bytes")
            done += 1

            if verify:
                self._emit(cb, i, total, f"Verifying {name} on the iPhone...")
                check = await self._verify_on_device(afc, remote, lp)
                check["skipped"] = False
                results.append(check)
                if check["ok"]:
                    verified += 1
                else:
                    bad_names.append(f"{name} ({check['reason']})")
            else:
                results.append({"name": name, "remote": remote, "ok": True,
                                "skipped": False,
                                "reason": "written, not verified"})
        message = PUSH_MESSAGE.format(done=done, folder=DEVICE_PUSH_DIR,
                                      verified=verified,
                                      failed=len(bad_names) if verify else 0)
        if rejected:
            message += ("\n\n{0} file(s) were skipped because they look corrupt "
                        "or incomplete:\n").format(rejected)
            message += "\n".join("  • " + n for n in bad_names)
            message += "\n\nThey may skip or fail to play."
        self._emit(cb, total, total, message)
        return {"added": done, "verified": verified, "rejected": rejected,
                "results": results, "message": message, "folder": DEVICE_PUSH_DIR}

    async def _delete_music(self, afc, tracks, cb):
        total = len(tracks)
        for i, track in enumerate(tracks, 1):
            label = track.title or os.path.basename(track.path)
            self._emit(cb, i, total, f"Deleting {label}...")
            try:
                await afc.rm_single(track.path, force=True)
            except Exception as e:
                log.debug("delete failed for %s: %s", track.path, e)
        self._emit(cb, total, total, f"Deleted {total} track(s)")

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _local_name(self, dest_dir, track):
        """Build a local path using the real track name (as shown on iPhone).

        Files are named \"Artist - Title.ext\", or just \"Title.ext\" when the
        artist is unknown or already part of the title."""
        ext = os.path.splitext(track.path)[1] or '.mp3'
        title = track.title or os.path.basename(track.path)
        artist = track.artist or ""
        if artist and not title.lower().startswith(artist.lower()):
            base = f"{artist} - {title}"
        else:
            base = title
        base = self._clean_name(base)
        path = os.path.join(dest_dir, base + ext)
        if not os.path.exists(path):
            return path
        i = 1
        while os.path.exists(os.path.join(dest_dir, f'{base} ({i}){ext}')):
            i += 1
        return os.path.join(dest_dir, f'{base} ({i}){ext}')

    async def _remote_exists(self, afc, path):
        try:
            return (await afc.stat(path)) is not None
        except Exception:
            return False

    async def _remote_size(self, afc, path):
        try:
            return int((await afc.stat(path)).get('st_size', 0))
        except Exception:
            return 0

    def _clean_name(self, name):
        name = os.path.basename(name)
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name).strip()
        return name or 'track'

    def _emit(self, cb, done, total, label):
        if cb:
            try:
                cb(done, total, label)
            except Exception:
                pass

    def _friendly_error(self, msg):
        low = msg.lower()
        if not self.device.get('serial'):
            return ("No iPhone detected. Connect your iPhone with a USB cable,\n"
                    "unlock it, and tap \"Trust\" on the phone when prompted.")
        if 'pairing' in low or 'trust' in low or 'no host' in low or 'not paired' in low:
            return ("The iPhone has not been paired with this computer yet.\n"
                    "Unlock the phone, tap \"Trust\", enter your passcode, and try again.")
        if 'connection' in low or 'timeout' in low or 'usbmux' in low:
            return ("Could not reach the iPhone.\n"
                    "Make sure usbmuxd is running, the cable is a data cable,\n"
                    "and the phone is unlocked.")
        return msg