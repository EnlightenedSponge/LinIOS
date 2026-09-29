# LinIOS

**A free, open-source iPhone music manager for Linux.**

LinIOS is a small desktop app that lets you move music between your iPhone and
your Linux computer without iTunes. Point it at your phone, and you can copy
your library off the phone, add new music to it, and remove tracks you no longer
want — with a real window, real buttons, and a progress bar. No terminal, no
Apple software, no subscription.

There is no other app that does this on Linux. Every other option either needs
iTunes through a Windows virtual machine, needs a paid vendor, or leaves you
with command-line tools and no safety net. LinIOS exists because moving music
off an iPhone on Linux should not be a project.

---

## What it does

- **Finds your iPhone** the moment you plug it in over USB, and keeps watching
  for it if you unplug it.
- **Copies music off your phone** to a folder on your computer, using the real
  song names (`Artist - Title.mp3`) instead of Apple's internal filenames like
  `NFOA.mp3`.
- **Adds music to your iPhone's real library**, so tracks show up next to your
  existing music in the stock **Music** app and play normally.
- **Removes tracks** from your iPhone, after asking you to confirm.
- **Checks the files.** Every track is verified, on the way off the phone and on
  the way onto it, so truncated or corrupt files are reported rather than
  quietly becoming a broken song later.
- **Backs up your library** before it ever changes it, and can put that backup
  back.

---

## Who it is for

You have a Linux laptop or desktop, an iPhone, and a USB cable. You want your
music off the phone and onto the computer, or you want to put a new album on the
phone and hear it in the Music app. You would rather click a button than read a
man page.

If you have ever tried to get at an iPhone's music on Linux, you have probably
met one of these:

- `idevicebackup` throws an error you cannot interpret.
- A tool says "success" and you later find half the files are 0 bytes.
- The only GUI option is a Windows-only app running in a virtual machine.
- Everything works until the tool rewrites your library and the Music app stops
  showing half your songs.

LinIOS is built around not doing those things.

---

## Requirements

- A Linux desktop (GNOME, KDE, XFCE, Cinnamon, or anything else with a normal
  window manager)
- Python 3.8 or newer
- An iPhone running iOS 12 or newer
- **A data cable, not a charge-only cable.** This is the single most common
  reason LinIOS cannot see a phone. If the cable only charges, nothing works.

`ffprobe` (from ffmpeg) is optional. If you have it, LinIOS reads real song
tags and decodes audio more deeply. If you don't, everything still works — song
names just come from filenames.

---

## Install

```sh
cd ~/Desktop/LinIOS
python3 install.py
```

The installer asks for your password once, then handles everything:

1. Installs `usbmuxd`, `libimobiledevice`, `ifuse`, `libusb` and the PyQt5
   widgets library using your distribution's package manager.
2. Installs the two Python libraries LinIOS needs (`PyQt5` and
   `pymobiledevice3`).
3. Installs a USB permission rule so your user account can talk to the phone
   without `sudo`, and adds a **LinIOS** entry to your application menu.
4. Restarts the USB service and unloads a kernel module that can hide your
   iPhone (see [A note about fast charging](#a-note-about-usb-fast-charging)).
5. Starts LinIOS for you.

You can run it more than once. It checks before it changes anything.

**After installing, log out and back in.** The installer adds your user to the
`plugdev` group, and group membership only applies to new sessions. Skipping this
is the other common reason the phone stays invisible.

---

## Launching LinIOS

Double-click **LinIOS** in your application menu, or run:

```sh
~/Desktop/LinIOS/run.sh
```

---

## First run, step by step

1. **Unlock the iPhone and plug it in.** Keep it unlocked for the first minute.
2. **Tap "Trust" on the phone** and enter your passcode when iOS asks. If you
   miss this prompt, unplug and replug — it will appear again. Until you trust
   the computer, no program on Linux can read the phone.
3. **Wait for the green banner.** It will say the phone is connected. You still
   have to click **Open Music Manager** yourself; LinIOS will not change screens
   on its own.
4. **Click "Refresh Music List".** This is the button that reads your library.
   If it is empty, the phone has no synced music on it, or it has not been
   trusted.

That is the setup. Everything after this is a button.

---

## Using it

### Get music off your iPhone

Click **Save All Music to Computer** to copy everything, or select tracks in
the list and click **Save Selected Music**. Files land in `~/Music/LinIOS` and
are named `Artist - Title.mp3`.

Click **Change Save Folder** to put them somewhere else. Note that this choice is
for the current session only — LinIOS does not remember it between launches, so
it goes back to `~/Music/LinIOS` next time.

If a file with the same name already exists, LinIOS does not overwrite it. It
skips the file and tells you it did, or saves a second copy as
`Title (1).mp3`.

### Read the music list

The list shows every track LinIOS can find, as `Artist - Title`. If a track is
flagged with an orange **⚠**, the file on the phone is empty or is a different
size than the library says it should be — it is incomplete. Click
**Show detailed view** to see everything, or **Show icon only** to see just
names.

Files are still copied even when they are flagged, because you may want the
bytes regardless. They are listed separately in the result so you know which
ones to check. Hovering over a ⚠ track tells you what is wrong with it.

The **View Mode** options at the top of the sidebar are placeholders and do not
change anything yet. The list is always shown in full, as `Artist - Title`.

### Add music to your iPhone

Click **Add Music to Library...** and pick your files. This is the one you want
most of the time: the tracks end up in the real **Music** app.

It takes a moment longer than the alternative, and afterwards it will tell you
to restart your iPhone. That is not a bug and you cannot skip it — see
[Why restarting the iPhone is required](#why-restarting-the-iphone-is-required).

**Add to Files Folder Instead...** is the other option. It drops the files into
a folder called `LinIOS Music` that you can browse in the **Files** app under
*On My iPhone*. It is faster and needs no restart, but the **Music** app will not
see them and you cannot play them from your library. Only use this if you
specifically want the files in the Files app.

### Confirm what is really on the iPhone

Click **Show Music Added to iPhone...**. This reads the folder back off the
device rather than showing you LinIOS's own notes, so it tells you what the
iPhone actually has. The list refreshes every time you open it, and each file is
marked **verified** or **PROBLEM**.

While a file is being transferred, LinIOS holds the original, so verification is
byte-for-byte: same size, same MD5 checksum, and audio that still decodes. When
you list the folder later, the original is gone, so it can only confirm the file
is readable and valid audio. A truncated MP3 can still pass that weaker check,
because the file header records the length the track *should* have been. Keep
your local copy if you want the strong check.

### Remove music

Select tracks, click **Delete Selected Music**, and confirm. LinIOS defaults to
**No** so a stray Enter cannot empty a folder.

This removes the audio file only. The entry stays in the iPhone's library
database, so the Music app can be left pointing at songs that are no longer
there. If you delete files, restore the matching library backup afterwards to
keep the two in step.

### Back up and restore your library

In the sidebar at the bottom, under **iPhone Library Backup**:

- **Back Up Library Now** takes a verified snapshot of the media library and
  tells you when it was last taken and how many tracks it holds.
- **Backups & Restore...** lists every backup with its date, track count, size
  and verification status. Select one to **Verify Selected** or
  **Restore Selected**.

Restoring is deliberately awkward to do by accident. The button is disabled for
any backup that fails verification, selecting a bad one explains why instead of
offering it, and confirming takes a fresh backup of whatever is currently on the
phone first. Your old database is also left on the device as
`MediaLibrary.sqlitedb.linios-pre-restore` so a bad swap can be undone.

Restoring is only visible after you **reboot the iPhone**. There is no way
around this, from the app or the command line.

---

## Why restarting the iPhone is required

When LinIOS adds music to your library, it has to tell iOS about the new files,
because the Music app reads its list from a database that iOS owns
(`MediaLibrary.sqlitedb`). LinIOS writes a row per track into that database.

iOS keeps that database open at all times. While the phone is running, the
system's media service can write over what LinIOS just wrote, and the Music app
is reading from a cache that will not be refreshed. So the new tracks are only
really there once the phone boots again.

This is also why LinIOS adds music **as a batch with one restart** rather than
one file at a time. Twenty tracks means one restart, not twenty.

The same applies in reverse: a restore is not complete until the phone has been
rebooted.

---

## How LinIOS is careful with your library

Adding music to the library is the only thing LinIOS does that writes to
Apple's own files, so it is fenced in as tightly as it can be.

**Nothing is written before a verified backup exists.** Every library add
starts by taking a snapshot of the current media library and checking it. If
that fails, LinIOS stops before touching the phone. You are never one click away
from a change you cannot undo.

**Files are read back after they are written.** After copying each track to the
phone, LinIOS reads it back off the device and compares it byte-for-byte with
what it sent. A file that does not read back intact is a failure, not a success.

**The database it builds is checked before it is installed.** LinIOS will only
replace your library if the new database adds exactly the number of rows it
expected, marks every new track as in-library, and passes SQLite's own
`integrity_check`. If any of that is off, the build is thrown away and your
library is left exactly as it was. This is deliberate: a future iOS update that
changes the schema will cause LinIOS to refuse to work rather than to damage
your library.

**The old database is kept on the device** as
`MediaLibrary.sqlitedb.linios-pre-restore` when a new one is installed.

### Why the verification goes as far as it does

The media library is a write-ahead-log SQLite database: the main file plus a
`-wal` log and a `-shm` index. On a test device the `-wal` was 1.8 MB against a
1.2 MB main file, so a great deal of live data lived outside the main database
itself. The three files must all come from the same instant.

LinIOS can read one file at a time, so a pull can come back torn — files from
slightly different moments. Here is the trap: `PRAGMA integrity_check` does
**not** catch this. Given a `-wal` it cannot validate, SQLite discards the log
and checks the self-consistent main file instead, so a torn pull cheerfully
reports "ok". LinIOS therefore also compares the **WAL salt**, a value the `-wal`
and `-shm` both carry that changes whenever the log is recycled. That is what
actually proves the three files belong together. A mismatch fails the backup and
the pull is retried.

This is also why a backup is not just one file. Each backup contains the raw
database with both sidecars, a checkpointed single-file copy, and a manifest
listing checksums, row counts, the device it came from, the iOS version and the
WAL cross-check result.

---

## What lives where

| Where | What |
|---|---|
| `~/Music/LinIOS` | Music you saved off the phone (default save folder) |
| `~/Music/LinIOS/medialib-backups/` | Library backups, one timestamped folder each |
| `LinIOS/logs/linios.log` | Full log, including anything that went wrong |
| On the phone: `Music/LinIOS` | Tracks added to the library, in the Music app |
| On the phone: `LinIOS Music` | Tracks added for the Files app |

The two push destinations have similar names and are genuinely different. Tracks
in **Music/LinIOS** are in the Music app. Tracks in **LinIOS Music** are in the
Files app and nowhere else.

---

## When something goes wrong

LinIOS checks your USB configuration before it gives up, because a phone that
has been hijacked by a kernel module looks exactly like a missing phone but
needs a completely different fix. The error dialog will tell you which case you
are in.

**"No iPhone detected"** — connect it, unlock it, tap **Trust**. If a dialog
appears explaining that your phone is on a USB configuration with no Apple USB
Multiplexor, that is a driver problem, not a cable problem, and the dialog will
give you the exact command to run.

**"Could not open a connection"** — unlock the phone, tap **Trust** again, then:

```sh
sudo systemctl restart usbmuxd
```

and unplug and replug the cable.

**Nothing happens when I plug in** — try another USB port, and try a different
cable. Then confirm the phone is visible to the system at all:

```sh
idevice_id -l
```

That should print your iPhone's UDID. If it prints nothing, the problem is
below LinIOS: permissions, cable, or the fast-charge module below.

**Permissions after installing** — unplug and replug the phone, or:

```sh
sudo udevadm trigger
```

And remember: after installing, log out and back in so the `plugdev` group
addition takes effect.

**A restore does not show up** — reboot the iPhone. This is expected, not a
failure.

**A file is flagged ⚠** — it is empty or truncated on the phone. Copy it anyway
if you want the bytes, but do not push it back: LinIOS refuses to upload files
it has flagged as corrupt, which is intentional.

---

## A note about USB fast charging

Some systems ship a kernel module called `apple-mfi-fastcharge` that is meant to
make Apple devices charge faster over USB. Its USB alias matches nearly every
Apple USB device, so it grabs your iPhone and takes the whole device over. The
phone then sits on a USB configuration with no *Apple USB Multiplexor*
interface, and the multiplexor is the only way to talk to an iPhone at all. The
result is a phone that `lsusb` lists but that no program can reach.

LinIOS blacklists this module on install and unloads it if it is currently
loaded. **The trade-off is real: Apple devices lose USB fast charging and charge
at standard USB-C current.** Restoring it would require a version of the module
built with a narrow alias that matches only genuine fast-charge interfaces, and
the stock one is not safe to use alongside iPhone connectivity. For a desktop
machine that charges overnight, standard speed is a reasonable price for being
able to reach your phone at all.

Check its state at any time:

```sh
lsmod | grep apple_mfi   # should print nothing
idevice_id -l            # should print your iPhone's UDID
```

---

## Doing it from a terminal

Everything LinIOS does to your library is also available as commands, if you
prefer to script it or want a backup on a schedule.

```sh
# Take a verified snapshot of the media library
python3 medialib.py backup --note "before doing something risky"

# List every backup, with its track count and verification status
python3 medialib.py list

# Re-check a backup
python3 medialib.py verify ~/Music/LinIOS/medialib-backups/<name>

# Put a backup back. --yes is required on purpose.
python3 medialib.py restore ~/Music/LinIOS/medialib-backups/<name> --yes
```

The `restore` command refuses to run without `--yes`, and reminds you to reboot
the iPhone afterwards.

---

## What LinIOS cannot do

Being clear about this early is better than finding out later.

- **It cannot reach a locked phone.** The iPhone must be unlocked and trusted.
- **It cannot reach a charge-only cable.**
- **It cannot reboot your iPhone for you.** The reboot after a library add or a
  restore is yours to do.
- **It cannot un-delete a file you deleted.** That is what backups are for.
- **It cannot see a fully streamed library.** Adding to the library needs at
  least one existing synced local track to use as a template. If your library
  has nothing but streamed or purchased content, LinIOS will say so and stop.
- **It only ever uses the first iPhone it finds.** Two phones plugged in is not
  a supported configuration.
- **It is not a full-device backup.** Backups cover the media library only. For
  a full-device backup, use a dedicated tool.
- **It is tied to the iOS version it was tested on.** Adding to the library
  depends on undocumented database behaviour. If Apple changes it, LinIOS
  refuses to proceed rather than risk your library. This is a feature, but it
  does mean it may stop working after an iOS update until it is updated to
  match.

---

## Honest notes on the implementation

- It is coupled to iOS internals. The library-add path is the honest way to do
  this on Linux, and it is guarded, but it is a reverse-engineered schema rather
  than a supported API.
- The library-add path was developed against one device. Findings about
  database behaviour come from real experiments on that phone and may not hold
  on other hardware or other iOS versions.
- The installer writes a USB permission rule that makes Apple USB device nodes
  world-writable. That is normal for a desktop but it is a real loosening of
  permissions, and you should know it is happening.
- `ffprobe` is optional but genuinely useful. Without it, song tags come from
  filenames and LinIOS strips a leading number — treating `01 - Song.mp3` as
  `Song` — which is right most of the time and wrong when the number is part of
  the title.

---

## License

LinIOS is free and open source.
