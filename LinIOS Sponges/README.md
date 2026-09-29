# LinIOS

A simple, fool-proof iPhone music manager for Linux — same idea as SyncIOS but
free, open source, and native on Linux.

## Features

- Auto-detects your iPhone when you plug it in over USB
- **Pull**: copy all (or selected) music from your iPhone to your computer
  (saved into `~/Music/LinIOS` by default, changeable at any time)
- **Push to the Music app**: add music to the iPhone's real library so tracks
  show up and play in the stock **Music** app (batched, verified, backed up, and
  activated with a single restart — see [How push works](#how-push-works))
- **Push to Files**: optionally copy music to a folder for the **Files** app
  instead, if that is what you want
- **Delete**: remove tracks from your iPhone
- **Real track names**: reads the iPhone's own media library so songs keep
  their proper titles (`Artist - Title.mp3`) instead of Apple's internal
  names like `NFOA.mp3`
- **File integrity validation** — every track is checked so music never skips:
  - scans all music on the phone and flags anything incomplete/corrupt (⚠)
  - every downloaded file is verified byte-for-byte against the phone and
    deep-checked with ffprobe (when installed)
  - corrupt/incomplete files are rejected before uploading to your iPhone
  - big friendly buttons, status messages, and progress bars — no terminal needed

## Requirements

- Linux with a desktop environment
- USB cable + iPhone (iOS 12 or newer)
- Python 3.8+

## Install (one command)

```sh
cd ~/Desktop/LinIOS
python3 install.py
```

The installer will:
1. Install system packages (usbmuxd, libimobiledevice, ifuse, libusb, PyQt5)
2. Install Python libraries (pymobiledevice3, pyusb)
3. Add USB permission rules for Apple devices
4. Blacklist the `apple-mfi-fastcharge` kernel module (see below)
5. Add a "LinIOS" launcher to your application menu

## Launch

Double-click **LinIOS** from your app menu, or run:

```sh
~/Desktop/LinIOS/run.sh
```

## First-time note

When you plug your iPhone in, iOS will show a dialog: tap **"Trust"** and enter
your passcode, otherwise the phone refuses access. Keep the phone unlocked.

## How push works

LinIOS has two push modes.

### Add Music to Library (default)

This puts music in the stock **Music** app, so tracks show up alongside your real
library and play normally.

It works in four steps, in this order:

1. **Back up** your current media library. Nothing is written to the iPhone
   before this backup is verified, so there is always a known-good snapshot to
   roll back to.
2. **Copy** each track to `/iTunes_Control/Music/LinIOS/` and read it back to
   confirm the bytes arrived intact.
3. **Register** the tracks by writing one row per track into Apple's
   `MediaLibrary.sqlitedb`, so the Music app knows they exist.
4. **Restart the iPhone.** Apple's media service holds the database open while
   the phone is running, so the new tracks only appear after a restart. This is
   why tracks are added as a single batch with one restart, not one per track.

If any step fails, your library is left exactly as it was.

### Add to Files Folder Instead

The older, simpler route: files are copied to a `LinIOS Music` folder and appear
in the **Files** app on the device ("On My iPhone" → "LinIOS Music"). This is
faster and does not need a restart, but the Music app will not see these files.
Use it only if you want the files in Files rather than in your library.

## Confirming a track is really on the iPhone

A successful write is not proof, so LinIOS reads the file back off the device
after every transfer. For each file it checks that the copy on the iPhone has
the same size, the same MD5 checksum as the local original, and audio that
still decodes. Anything that fails is reported as a problem instead of being
counted as added.

To check the folder at any time, use **Show Music Added to iPhone**. That list
is read from the device itself rather than from LinIOS's record of what it
sent, so it reflects what the iPhone actually has. Files are refreshed
automatically when the window is reopened.

What the check can and cannot prove:

- With the original to hand (during a transfer), it is a byte-for-byte
  comparison, so any corruption or truncation is caught.
- Without the original (when listing the folder later), it confirms the file is
  readable and valid audio, but a truncated MP3 can still pass, because the
  file header records the full length of the original track. Keep the local
  copy for a full comparison.

## Troubleshooting

- **"No iPhone detected"** → check the message in the dialog. LinIOS inspects the
  USB configuration first, because a hijacked configuration looks exactly like a
  missing phone but needs a completely different fix.
- **"Could not open a connection"** → unlock the phone, re-tap Trust, and run
  `sudo systemctl restart usbmuxd`, then unplug/replug the cable.
- **No device detected** → make sure you are using a data (not charge-only)
  cable and check the USB port.
- **Permissions** → the installer adds a udev rule. After install, unplug and
  replug the phone, or run `sudo udevadm trigger`.

### The `apple-mfi-fastcharge` module

Some systems ship an `apple-mfi-fastcharge` kernel module, meant to enable fast
charging for Apple devices. Its USB alias matches nearly *every* Apple USB
device, so it binds to your iPhone and takes the whole device over. The phone
then sits on a USB configuration that has no *Apple USB Multiplexor* interface,
which is the only way usbmuxd can talk to it. The result is an iPhone that
`lsusb` lists but no program can reach.

LinIOS blacklists the module on install and unloads it if it is currently
loaded. The trade-off is that Apple devices lose USB fast charging and charge at
standard USB-C current. To restore it you would need a version of the module
built with a narrow alias matching only genuine MFI fast-charge interfaces
(class `0xFF`, subclass `0xFE`, protocol `0x01`) — the stock one is not safe to
use alongside iPhone connectivity.

Check its state at any time:

```sh
lsmod | grep apple_mfi     # should print nothing
idevice_id -l              # should print your iPhone's UDID
```

## Media library backup

LinIOS can back up the music library stored on your iPhone and put a backup
back. This is the safety net for anything that ever needs to write to that
database, which iOS keeps open at all times.

From the app, with the iPhone connected, use the **iPhone Library Backup**
panel at the bottom of the left sidebar:

- **Back Up Library Now** — takes a verified snapshot. The sidebar shows when
  the last one was taken and how many tracks it holds.
- **Backups & Restore...** — lists every backup with its date, track count,
  size and verification status. Select one to see its details, **Verify
  Selected** to re-check it, or **Restore Selected** to put it back.

Restoring is deliberately hard to do by accident. The button is disabled for
any backup that fails verification, selecting an invalid one explains why
instead of offering it, and confirming a restore first takes a fresh backup of
whatever is currently on the phone. The old database is also left on the device
as `MediaLibrary.sqlitedb.linios-pre-restore`.

The same commands are available from a terminal:

```sh
python3 medialib.py backup --note "before doing something risky"
python3 medialib.py list
python3 medialib.py verify ~/Music/LinIOS/medialib-backups/<name>
python3 medialib.py restore ~/Music/LinIOS/medialib-backups/<name> --yes
```

Backups land in `~/Music/LinIOS/medialib-backups/<timestamp>/` and contain the
raw database with both sidecars, a checkpointed single-file copy, and a manifest
with checksums and row counts.

**Why it verifies more than it looks like it does.** The database is a
write-ahead-log SQLite file, and its `-wal` was 1.8 MB against a 1.2 MB main
file on a test device — a great deal of live data lived outside the main
database. The three files must all come from the same instant, but AFC has no
multi-file read, so they are pulled one after another and can be mixed up.

`PRAGMA integrity_check` does **not** catch that. Given a `-wal` it cannot
validate, SQLite throws the log away and checks the self-consistent main
database instead, so a torn pull still reports "ok". LinIOS therefore also
compares the WAL salt, which both the `-wal` and the `-shm` carry and which
changes whenever the log is recycled. That is what actually proves the files
belong together. A mismatch fails the snapshot and the pull is retried.

**Limits worth knowing:**

- A restore is only visible after the iPhone is **rebooted**, because
  `medialibraryd` holds the database open and may write over a swap that lands
  while it is running. The CLI says so, but it cannot reboot the phone for you.
- The old database is kept on the device as
  `MediaLibrary.sqlitedb.linios-pre-restore` so a bad swap can be undone.
- If you delete pushed audio files, restore the matching database backup too,
  otherwise the library ends up pointing at files that are gone.
- Backups cover the media library only, not the rest of the device. For a
  full-device backup use a dedicated tool.
