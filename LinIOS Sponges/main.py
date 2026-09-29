from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QFileDialog,
                             QProgressBar, QListWidget, QListWidgetItem, QMessageBox,
                             QStackedWidget, QScrollArea, QFrame, QLineEdit, QButtonGroup,
                             QRadioButton, QGroupBox, QSplitter, QStyle, QDialog,
                             QTableWidget, QTableWidgetItem, QDialogButtonBox,
                             QTextEdit)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QIcon, QFont, QColor, QPalette
import sys
import os
import logging
import threading
import traceback
from datetime import datetime

from detector import iPhoneDetector, diagnose
from transfer import MusicTransfer, DEVICE_PUSH_DIR as PUSH_DIR
import medialib

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "linios.log")


def setup_logging():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[
                logging.FileHandler(LOG_FILE),
                logging.StreamHandler(sys.stderr),
            ],
        )
    except Exception:
        logging.basicConfig(level=logging.INFO)


def install_excepthooks():
    def log_crash(exc_type, exc_value, exc_tb):
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logging.critical("Unhandled exception:\n%s", msg)
        try:
            from PyQt5.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None, "LinIOS Error",
                    "LinIOS hit a problem:\n\n%s\n\nA full log was saved to:\n%s"
                    % (exc_value, LOG_FILE))
        except Exception:
            pass

    sys.excepthook = log_crash
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda args: log_crash(
            args.exc_type, args.exc_value, args.exc_traceback)


class DeviceThread(QThread):
    connected = pyqtSignal(dict)
    disconnected = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._running = True
        self._last_state = None
        self.detector = iPhoneDetector()

    def run(self):
        while self._running:
            try:
                device = self.detector.get_device()
            except Exception:
                device = None
            if device is not None and self._last_state is None:
                info = self.detector.get_device_info(device.get('serial')) or device
                self._last_state = True
                self.connected.emit(info)
            elif device is None and self._last_state is not None:
                self._last_state = None
                self.disconnected.emit()
            self.msleep(2000)

    def stop(self):
        self._running = False


class LoadMusicThread(QThread):
    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, device):
        super().__init__()
        self.device = device

    def run(self):
        try:
            t = MusicTransfer(self.device)
            self.loaded.emit(t.list_music())
        except Exception as e:
            self.failed.emit(str(e))


class TransferWorker(QThread):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)
    pushed = pyqtSignal(object)

    def __init__(self, device, mode, paths, dest_dir=None):
        super().__init__()
        self.transfer = MusicTransfer(device)
        self.mode = mode
        self.paths = paths
        self.dest_dir = dest_dir

    def run(self):
        try:
            if self.mode == 'push':
                result = self.transfer.push_music(self.paths, self.progress.emit)
                if result:
                    self.pushed.emit(result)
            elif self.mode == 'pull':
                self.transfer.pull_music(self.dest_dir, self.progress.emit)
            elif self.mode == 'pull_selected':
                self.transfer.pull_selected(self.paths, self.dest_dir, self.progress.emit)
            elif self.mode == 'delete':
                self.transfer.delete_music(self.paths, self.progress.emit)
            self.finished.emit(True, '')
        except Exception as e:
            self.finished.emit(False, str(e))


class MedialibWorker(QThread):
    """Runs a media library backup or restore off the UI thread.

    Both are slow enough to freeze the window for many seconds, and restore
    writes to the device, so it never runs on the GUI thread.
    """

    log = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, mode, backup_dir=None, keep_old=True, pre_backup=True):
        super().__init__()
        self.mode = mode
        self.backup_dir = backup_dir
        self.keep_old = keep_old
        self.pre_backup = pre_backup

    def run(self):
        import asyncio
        try:
            if self.mode == 'backup':
                manifest, dest = asyncio.run(medialib.backup(
                    note="from LinIOS", progress=self.log.emit))
                items = (manifest.get('row_counts') or {}).get('item')
                self.done.emit(True, "Backed up %s tracks.\n\nSaved to:\n%s"
                                     % (items, dest))
            elif self.mode == 'restore':
                if self.pre_backup:
                    self.log.emit("Saving the current library first...")
                    manifest, dest = asyncio.run(medialib.backup(
                        note="automatic, taken before a restore",
                        progress=self.log.emit))
                    self.log.emit("Current library saved to %s" % dest)
                result = asyncio.run(medialib.restore(
                    self.backup_dir, confirm=True, keep_old=self.keep_old))
                items = (result.get('row_counts') or {}).get('item')
                self.done.emit(True,
                               "Restored %s tracks from the backup taken %s.\n\n"
                               "Reboot the iPhone for this to take effect."
                               % (items, result.get('restored')))
            else:
                self.done.emit(False, "Unknown operation: %s" % self.mode)
        except Exception as e:
            logging.exception("medialib %s failed", self.mode)
            self.done.emit(False, str(e))


class PushedFilesWorker(QThread):
    """Reads the push folder back off the iPhone to confirm what is really there."""

    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, device):
        super().__init__()
        self.device = device

    def run(self):
        try:
            self.loaded.emit(MusicTransfer(self.device).list_pushed(verify=True))
        except Exception as e:
            logging.exception("list_pushed failed")
            self.failed.emit(str(e))


class PushedFilesDialog(QDialog):
    """Shows what is in the LinIOS Music folder, as read back from the iPhone.

    Nothing here comes from LinIOS's own record of what it sent, so it is the
    honest answer to "is the track really on my phone?".
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Music Added to iPhone")
        self.setMinimumSize(680, 440)
        self.worker = None

        lay = QVBoxLayout(self)

        intro = QLabel(
            "These files are on your iPhone in the \"%s\" folder.\n"
            "Open them with the Files app (On My iPhone -> %s). They are not in "
            "the Music app, and LinIOS does not put them there."
            % (PUSH_DIR, PUSH_DIR))
        intro.setWordWrap(True)
        intro.setObjectName("outputLabel")
        lay.addWidget(intro)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["File", "Size", "On iPhone", "Notes"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 250)
        self.table.setColumnWidth(3, 240)
        lay.addWidget(self.table)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("transferStatus")
        lay.addWidget(self.summary)

        row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh from iPhone")
        self.refresh_btn.clicked.connect(self.refresh)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row.addWidget(self.refresh_btn)
        row.addStretch()
        row.addWidget(close_btn)
        lay.addLayout(row)

    @staticmethod
    def _human(n):
        if n >= 1024 * 1024:
            return "%.1f MB" % (n / (1024.0 * 1024.0))
        if n >= 1024:
            return "%.0f KB" % (n / 1024.0)
        return "%d B" % n

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def refresh(self):
        if self.worker is not None and self.worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self.summary.setText("Reading the iPhone...")
        self.worker = PushedFilesWorker(self.parent().device
                                       if hasattr(self.parent(), "device") else None)
        self.worker.loaded.connect(self._on_loaded)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_loaded(self, entries):
        self.refresh_btn.setEnabled(True)
        self.table.setRowCount(0)
        good = sum(1 for e in entries if e.get("ok"))
        bad = len(entries) - good
        for e in entries:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(e.get("name", "")))
            self.table.setItem(r, 1, QTableWidgetItem(self._human(e.get("size", 0))))
            verdict = QTableWidgetItem("verified" if e.get("ok") else "PROBLEM")
            if not e.get("ok"):
                verdict.setForeground(QColor("#dc2626"))
            self.table.setItem(r, 2, verdict)
            self.table.setItem(r, 3, QTableWidgetItem(e.get("reason", "")))
        if not entries:
            self.summary.setText(
                "Nothing in the \"%s\" folder yet. Add music with "
                "\"Add Music to iPhone\"." % PUSH_DIR)
        elif bad:
            self.summary.setText(
                "%d of %d file(s) verified, %d need attention. Everything listed "
                "was read back off the iPhone just now." % (good, len(entries), bad))
        else:
            self.summary.setText(
                "All %d file(s) read back off the iPhone successfully. "
                "Remember: Files app, not the Music app." % len(entries))

    def _on_failed(self, msg):
        self.refresh_btn.setEnabled(True)
        self.summary.setText("Could not read the iPhone: %s" % msg)


class LibraryPushWorker(QThread):
    """Adds tracks to the iPhone's Music library (not the Files folder)."""

    progress = pyqtSignal(int, int, str)
    done = pyqtSignal(bool, str, object)

    def __init__(self, paths):
        super().__init__()
        self.paths = paths

    def run(self):
        import asyncio
        import library_push
        try:
            summary = asyncio.run(
                library_push.push_to_library(self.paths, self.progress.emit))
            self.done.emit(True, "", summary)
        except Exception as e:
            logging.exception("library push failed")
            self.done.emit(False, str(e), None)


class BackupsDialog(QDialog):
    """Lists media library backups and offers to restore one."""

    restoreRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("iPhone Library Backups")
        self.setMinimumSize(660, 430)

        lay = QVBoxLayout(self)

        intro = QLabel(
            "Backups of the music library stored on your iPhone.\n"
            "A restore replaces the library currently on the device with the "
            "one you pick.")
        intro.setWordWrap(True)
        intro.setObjectName("outputLabel")
        lay.addWidget(intro)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Taken", "Tracks", "Size", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        lay.addWidget(self.table)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("transferStatus")
        lay.addWidget(self.summary)

        row = QHBoxLayout()
        self.verify_btn = QPushButton("Verify Selected")
        self.verify_btn.clicked.connect(self._verify_selected)
        self.restore_btn = QPushButton("Restore Selected...")
        self.restore_btn.setObjectName("dangerBtn")
        self.restore_btn.clicked.connect(self._restore_selected)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row.addWidget(self.verify_btn)
        row.addWidget(self.restore_btn)
        row.addStretch()
        row.addWidget(close_btn)
        lay.addLayout(row)

        self._refresh()

    def _selected_path(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    @staticmethod
    def _dir_size(path):
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        if total >= 1024 * 1024:
            return "%.1f MB" % (total / (1024.0 * 1024.0))
        if total >= 1024:
            return "%.0f KB" % (total / 1024.0)
        return "%d B" % total

    def _refresh(self):
        self.table.setRowCount(0)
        rows = medialib.list_backups()
        for name, path, created, items, ok in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            when = self.table.item(r, 0)
            if when is None:
                when = QTableWidgetItem()
                self.table.setItem(r, 0, when)
            when.setText(created or name)
            when.setData(Qt.UserRole, path)
            for col, text in ((1, "-" if items is None else str(items)),
                              (2, self._dir_size(path)),
                              (3, "verified" if ok else "INVALID")):
                cell = QTableWidgetItem(text)
                if not ok:
                    cell.setForeground(QColor("#dc2626"))
                self.table.setItem(r, col, cell)
        if self.table.rowCount():
            self.table.selectRow(0)
        self._sync_buttons()

    def _sync_buttons(self):
        path = self._selected_path()
        has = path is not None
        self.verify_btn.setEnabled(has)
        if not has:
            self.restore_btn.setEnabled(False)
            self.summary.setText("No backup selected.")
            return
        ok, detail = medialib.verify(path)
        if not ok:
            # The summary says why, so the button only invites a dead end.
            self.restore_btn.setEnabled(False)
            self.summary.setText(
                "This backup cannot be restored:\n%s\n\nTake a new backup "
                "instead." % detail)
            return
        self.restore_btn.setEnabled(True)
        counts = detail.get('row_counts') or {}
        self.summary.setText(
            "Selected: %s\n%s tracks, %s albums. Integrity check passed (%s).\n"
            "Device: %s on iOS %s"
            % (os.path.basename(path), counts.get('item'), counts.get('album'),
               detail.get('wal_cross_check', 'n/a'),
               detail.get('device_udid') or 'unknown',
               detail.get('ios_version') or 'unknown'))

    def _verify_selected(self):
        path = self._selected_path()
        if not path:
            return
        ok, detail = medialib.verify(path)
        if ok:
            QMessageBox.information(
                self, "Backup Verified",
                "This backup is intact and can be restored.\n\n%s"
                % detail.get('created'))
        else:
            QMessageBox.warning(self, "Backup Invalid",
                                "This backup cannot be restored:\n\n%s" % detail)
        self._refresh()

    def _restore_selected(self):
        path = self._selected_path()
        if not path:
            return
        ok, detail = medialib.verify(path)
        if not ok:
            QMessageBox.warning(self, "Cannot Restore",
                                "This backup failed verification, so restoring it "
                                "could leave the library unusable:\n\n%s\n\n"
                                "Nothing has been changed." % detail)
            return
        counts = detail.get('row_counts') or {}
        answer = QMessageBox.warning(
            self, "Replace the iPhone Library?",
            "This replaces the music library on your iPhone with the backup "
            "from %s.\n\n"
            "  Tracks in the backup: %s\n"
            "  Tracks on the iPhone now: see the music list\n\n"
            "Before doing this LinIOS will save the current library, so it can "
            "be put back.\n\n"
            "You must reboot the iPhone afterwards for the change to take "
            "effect.\n\n"
            "Continue?"
            % (detail.get('created'), counts.get('item')),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.restoreRequested.emit(path)

    def showEvent(self, event):
        self._refresh()
        super().showEvent(event)


class LinIOSApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LinIOS - iPhone Music Manager")
        self.resize(900, 650)
        self.setMinimumSize(800, 550)

        self.device = None
        self.device_thread = DeviceThread()
        self.device_thread.connected.connect(self.on_device_connected)
        self.device_thread.disconnected.connect(self.on_device_disconnected)
        self.device_thread.start()

        self.worker = None
        self.medialib_worker = None
        self.remote_files = []
        self.output_dir = os.path.expanduser("~/Music/LinIOS")
        os.makedirs(self.output_dir, exist_ok=True)

        self._build_ui()
        self._apply_style()
        self._refresh_backup_status()
        self._set_medialib_enabled(False)

        self.statusBar().showMessage("Waiting for iPhone...", 5000)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.top_bar = QFrame()
        self.top_bar.setObjectName("topBar")
        top_layout = QHBoxLayout(self.top_bar)

        self.header_icon = QLabel()
        self.header_icon.setPixmap(self.style().standardIcon(QStyle.SP_ComputerIcon).pixmap(32, 32))
        self.header_title = QLabel("LinIOS")
        self.header_title.setObjectName("headerTitle")
        self.device_status = QLabel("No iPhone detected")
        self.device_status.setObjectName("deviceStatus")
        self.device_status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        top_layout.addWidget(self.header_icon)
        top_layout.addWidget(self.header_title)
        top_layout.addStretch()
        top_layout.addWidget(self.device_status)
        layout.addWidget(self.top_bar)

        self.conn_banner = QFrame()
        self.conn_banner.setObjectName("connBanner")
        self.conn_banner.hide()
        self.conn_banner_layout = QHBoxLayout(self.conn_banner)
        self.conn_banner_label = QLabel("iPhone connected!")
        self.conn_banner_label.setObjectName("connLabel")
        self.conn_banner_layout.addWidget(self.conn_banner_label)
        self.conn_banner_layout.addStretch()
        layout.addWidget(self.conn_banner)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        self._build_welcome_page()
        self._build_home_page()

        self.stack.addWidget(self.welcome_page)
        self.stack.addWidget(self.home_page)

        self.stack.setCurrentWidget(self.welcome_page)

    def _build_welcome_page(self):
        self.welcome_page = QWidget()
        wl = QVBoxLayout(self.welcome_page)
        wl.addStretch()

        icon_label = QLabel()
        icon_label.setPixmap(self.style().standardIcon(QStyle.SP_ComputerIcon).pixmap(96, 96))
        icon_label.setAlignment(Qt.AlignCenter)

        title = QLabel("LinIOS")
        title.setObjectName("bigTitle")
        title.setAlignment(Qt.AlignCenter)

        subtitle = QLabel("Easily sync music between your Linux computer and iPhone")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignCenter)

        status = QLabel("Connect your iPhone using a USB cable. When prompted on the\nphone, tap \"Trust\" to allow access.")
        status.setObjectName("statusHint")
        status.setAlignment(Qt.AlignCenter)

        wl.addWidget(icon_label)
        wl.addWidget(title)
        wl.addWidget(subtitle)
        wl.addSpacing(20)
        wl.addWidget(status)
        wl.addStretch()

        self.welcome_btn = QPushButton("Open Music Manager")
        self.welcome_btn.setEnabled(False)
        self.welcome_btn.setObjectName("primaryBtn")
        self.welcome_btn.clicked.connect(lambda: self.stack.setCurrentWidget(self.home_page))
        wl.addWidget(self.welcome_btn, alignment=Qt.AlignHCenter)
        self.welcome_btn.setFixedWidth(220)
        self.welcome_btn.setFixedHeight(44)

    def _build_home_page(self):
        self.home_page = QWidget()
        hl = QVBoxLayout(self.home_page)

        splitter = QSplitter(Qt.Horizontal)
        hl.addWidget(splitter)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)

        self.icon_only_radio = QRadioButton("Show icon only (simplest)")
        self.detailed_radio = QRadioButton("Show detailed view")
        self.detailed_radio.setChecked(True)
        self.icon_mode_group = QButtonGroup(self)
        self.icon_mode_group.addButton(self.icon_only_radio)
        self.icon_mode_group.addButton(self.detailed_radio)

        view_group = QGroupBox("View Mode")
        vg = QVBoxLayout(view_group)
        vg.addWidget(self.icon_only_radio)
        vg.addWidget(self.detailed_radio)
        left_layout.addWidget(view_group)

        self.refresh_btn = QPushButton("Refresh Music List")
        self.refresh_btn.clicked.connect(self.load_music)
        left_layout.addWidget(self.refresh_btn)

        self.pull_btn = QPushButton("Save All Music to Computer")
        self.pull_btn.setObjectName("primaryBtn")
        self.pull_btn.clicked.connect(self.pull_all)
        left_layout.addWidget(self.pull_btn)

        self.pull_sel_btn = QPushButton("Save Selected Music")
        self.pull_sel_btn.clicked.connect(self.pull_selected)
        left_layout.addWidget(self.pull_sel_btn)

        self.del_sel_btn = QPushButton("Delete Selected Music")
        self.del_sel_btn.setObjectName("dangerBtn")
        self.del_sel_btn.clicked.connect(self.delete_selected)
        left_layout.addWidget(self.del_sel_btn)

        left_layout.addWidget(QLabel(""))
        self.output_label = QLabel(f"Save folder:\n{self.output_dir}")
        self.output_label.setWordWrap(True)
        self.output_label.setObjectName("outputLabel")
        left_layout.addWidget(self.output_label)

        self.change_dir_btn = QPushButton("Change Save Folder")
        self.change_dir_btn.clicked.connect(self.choose_output_dir)
        left_layout.addWidget(self.change_dir_btn)

        left_layout.addStretch()

        backup_group = QGroupBox("iPhone Library Backup")
        bg = QVBoxLayout(backup_group)
        self.backup_status_label = QLabel("")
        self.backup_status_label.setWordWrap(True)
        self.backup_status_label.setObjectName("outputLabel")
        bg.addWidget(self.backup_status_label)
        self.backup_now_btn = QPushButton("Back Up Library Now")
        self.backup_now_btn.clicked.connect(self.backup_library)
        bg.addWidget(self.backup_now_btn)
        self.backups_btn = QPushButton("Backups & Restore...")
        self.backups_btn.clicked.connect(self.show_backups)
        bg.addWidget(self.backups_btn)
        left_layout.addWidget(backup_group)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        right_header = QHBoxLayout()
        self.music_count_label = QLabel("0 tracks")
        self.music_count_label.setObjectName("countLabel")
        right_header.addWidget(self.music_count_label)
        right_header.addStretch()
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setVisible(False)
        right_header.addWidget(self.pause_btn)
        right_layout.addLayout(right_header)

        self.music_list = QListWidget()
        self.music_list.setSelectionMode(QListWidget.ExtendedSelection)
        right_layout.addWidget(self.music_list)

        self.drag_drop_label = QLabel("or drag & drop music files here to add them to iPhone")
        self.drag_drop_label.setObjectName("dragDrop")
        self.drag_drop_label.setAlignment(Qt.AlignCenter)
        right_layout.addWidget(self.drag_drop_label)

        self.add_files_btn = QPushButton("Add Music to Library...")
        self.add_files_btn.setObjectName("primaryBtn")
        self.add_files_btn.setFixedHeight(48)
        self.add_files_btn.clicked.connect(self.push_files)
        right_layout.addWidget(self.add_files_btn)

        self.files_btn = QPushButton("Add to Files Folder Instead...")
        self.files_btn.clicked.connect(self.push_files_folder)
        right_layout.addWidget(self.files_btn)

        self.pushed_btn = QPushButton("Show Music Added to iPhone...")
        self.pushed_btn.clicked.connect(self.show_pushed)
        right_layout.addWidget(self.pushed_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.hide()
        right_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("transferStatus")
        right_layout.addWidget(self.status_label)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([280, 620])

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #f5f6f8; }
            QWidget { font-size: 14px; color: #2c3e50; }
            #topBar {
                background: #1c3a6b;
                border-bottom: 3px solid #3b82f6;
                padding: 12px;
            }
            #headerTitle {
                color: white;
                font-size: 20px;
                font-weight: bold;
            }
            #deviceStatus {
                color: #8fb3ff;
                font-size: 13px;
            }
            #connBanner {
                background: #dcfce7;
                border: 1px solid #16a34a;
                border-radius: 6px;
                margin: 6px 0;
                padding: 8px;
            }
            #connLabel {
                color: #166534;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 8px 14px;
                font-weight: bold;
            }
            QPushButton:hover { background: #f1f5f9; border-color: #94a3b8; }
            QPushButton:disabled { color: #94a3b8; background: #e2e8f0; }
            #primaryBtn {
                background: #3b82f6;
                color: white;
                border: 1px solid #2563eb;
            }
            #primaryBtn:hover { background: #2563eb; }
            #dangerBtn { color: #dc2626; border-color: #fca5a5; }
            #dangerBtn:hover { background: #fee2e2; }
            QGroupBox {
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 10px;
                margin-top: 8px;
                font-weight: bold;
            }
            #bigTitle {
                font-size: 34px;
                font-weight: bold;
                color: #1c3a6b;
                padding-top: 12px;
            }
            #subtitle { color: #64748b; font-size: 15px; }
            #statusHint { color: #64748b; font-size: 13px; }
            QListWidget {
                background: white;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 4px;
            }
            QListWidget::item { padding: 6px; border-bottom: 1px solid #f1f5f9; }
            QListWidget::item:selected { background: #e0edff; color: #1c3a6b; }
            #outputLabel { font-size: 12px; color: #475569; background: #f8fafc;
                            border: 1px dashed #cbd5e1; padding: 8px; }
            #dragDrop { color: #94a3b8; font-size: 12px; padding: 4px; }
            #transferStatus { color: #475569; font-size: 12px; }
            QProgressBar {
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                text-align: center;
                height: 22px;
            }
            QProgressBar::chunk { background: #3b82f6; border-radius: 6px; }
        """)

    def on_device_connected(self, device):
        self.device = device
        name = device.get('name', 'iPhone')
        self.device_status.setText(f"Connected: {name}")
        self.conn_banner_label.setText(f"{name} connected! Click \"Add Music\" to transfer.")
        self.conn_banner.show()
        self.welcome_btn.setEnabled(True)
        self._set_medialib_enabled(True)
        self.statusBar().showMessage(f"{name} connected", 5000)
        QMessageBox.information(self, "iPhone Connected",
                                f"Your {name} is detected and ready.\nTap \"Trust\" on the phone if prompted.")
        self.load_music()

    def on_device_disconnected(self):
        self.device = None
        try:
            reason = diagnose()
        except Exception:
            reason = None
        self.device_status.setText(reason.splitlines()[0] if reason
                                   else "No iPhone detected")
        self.conn_banner.hide()
        self.music_list.clear()
        self.welcome_btn.setEnabled(False)
        if self.medialib_worker is None or not self.medialib_worker.isRunning():
            self._set_medialib_enabled(False)
        self.music_count_label.setText("0 tracks")
        self.statusBar().showMessage("iPhone disconnected", 5000)

    def _refresh_backup_status(self):
        """Summarise the newest backup in the left panel."""
        rows = medialib.list_backups()
        if not rows:
            self.backup_status_label.setText("No library backups yet.")
            return
        name, path, created, items, ok = rows[-1]
        if ok:
            self.backup_status_label.setText(
                "Last backup: %s\n%s tracks" % (created or name, items))
        else:
            self.backup_status_label.setText(
                "Last backup (%s) FAILED verification.\nTake a new one."
                % (created or name))

    def _set_medialib_enabled(self, enabled):
        self.backup_now_btn.setEnabled(enabled)
        self.backups_btn.setEnabled(enabled)

    def _start_medialib(self, mode, backup_dir=None):
        if self.medialib_worker is not None and self.medialib_worker.isRunning():
            QMessageBox.information(
                self, "Busy",
                "A library backup or restore is already running. Please wait for "
                "it to finish.")
            return
        self._set_medialib_enabled(False)
        self.status_label.setText("Working on the iPhone library...")
        self.medialib_worker = MedialibWorker(mode, backup_dir=backup_dir)
        self.medialib_worker.log.connect(self.status_label.setText)
        self.medialib_worker.done.connect(self._on_medialib_done)
        self.medialib_worker.start()

    def backup_library(self):
        if not self._require_device():
            return
        self._start_medialib('backup')

    def show_backups(self):
        if not self._require_device():
            return
        dialog = BackupsDialog(self)
        dialog.restoreRequested.connect(
            lambda path: self._start_medialib('restore', backup_dir=path))
        dialog.exec_()

    def _on_medialib_done(self, ok, message):
        self._set_medialib_enabled(self.device is not None)
        if ok:
            self.statusBar().showMessage("Library backup finished", 8000)
            QMessageBox.information(self, "Library Backup", message)
        else:
            self.status_label.setText("Ready")
            QMessageBox.critical(
                self, "Library Backup Failed",
                "%s\n\nYour library was not changed." % message)
        self._refresh_backup_status()

    def _no_device_message(self):
        """Specific reason the iPhone is unusable, falling back to generic help.

        Cable/Trust advice is wrong for a hijacked USB configuration, so check
        for that first and only then give the usual suggestions.
        """
        try:
            reason = diagnose()
        except Exception:
            reason = None
        if reason:
            return reason
        return ("No iPhone detected. Please connect your iPhone with a USB cable, "
                "unlock it, and tap \"Trust\" to allow access.")

    def _require_device(self):
        if self.device is None:
            QMessageBox.warning(self, "No iPhone", self._no_device_message())
            return False
        return True

    def load_music(self):
        if not self._require_device():
            return
        self.status_label.setText("Loading music list...")
        self.load_thread = LoadMusicThread(self.device)
        self.load_thread.loaded.connect(self.on_music_loaded)
        self.load_thread.failed.connect(self.on_music_load_failed)
        self.load_thread.start()

    def track_label(self, track):
        label = track.title
        if track.artist and not track.title.lower().startswith(track.artist.lower()):
            label = f"{track.artist} - {track.title}"
        if track.corrupt:
            label = f"⚠ {label}"
        return label

    def on_music_loaded(self, files):
        self.remote_files = files
        self.music_list.clear()
        corrupt_count = 0
        for t in files:
            item = QListWidgetItem(self.track_label(t))
            if t.corrupt:
                corrupt_count += 1
                item.setForeground(QColor("#c2410c"))
                item.setToolTip(
                    "May be corrupt or incomplete: " + (t.reason or "unknown problem"))
            self.music_list.addItem(item)
        count_text = f"{len(files)} tracks"
        if corrupt_count:
            count_text += f"  ({corrupt_count} may be corrupt)"
        self.music_count_label.setText(count_text)
        if corrupt_count:
            self.status_label.setText(
                f"Loaded {len(files)} tracks - {corrupt_count} may be corrupt/incomplete\n"
                f"(saving them may cause skipping). Their names are marked with ⚠.")
        else:
            self.status_label.setText(f"Loaded {len(files)} tracks - all healthy")

    def on_music_load_failed(self, msg):
        self.status_label.setText(f"Failed to load tracks: {msg}")
        QMessageBox.warning(self, "Error", msg)

    def _run_worker(self, mode, paths=None):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "Busy", "A transfer is already in progress.")
            return
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.status_label.setText("Working...")
        if mode == 'library_push':
            self.worker = LibraryPushWorker(paths or [])
            self.worker.progress.connect(self.on_progress)
            self.worker.done.connect(self.on_library_push_done)
        else:
            self.worker = TransferWorker(self.device, mode, paths or [], self.output_dir)
            self.worker.progress.connect(self.on_progress)
            self.worker.finished.connect(self.on_transfer_finished)
            self.worker.pushed.connect(self.on_pushed)
        self.worker.start()

    def on_library_push_done(self, ok, msg, summary):
        self.progress_bar.hide()
        if self.worker is not None:
            self.worker = None
        if not ok:
            self.status_label.setText("Error: %s" % msg)
            QMessageBox.critical(
                self, "Could Not Add to Library",
                "%s\n\nYour iPhone library was not changed." % (msg or "Unknown error"))
            if self.device is not None:
                self.load_music()
            return
        self.status_label.setText("Added! Restart your iPhone to see the new tracks.")
        self._show_library_push_summary(summary)

    def _show_library_push_summary(self, s):
        dlg = QDialog(self)
        dlg.setWindowTitle("Added to iPhone Library")
        dlg.setMinimumWidth(560)
        v = QVBoxLayout(dlg)
        head = QLabel("Added %d track(s) to your iPhone Music library." % s["added"])
        head.setStyleSheet("font-size: 15px; font-weight: 600;")
        v.addWidget(head)
        v.addWidget(QLabel("Each file was copied and read back to confirm it arrived "
                           "intact."))
        listing = QTextEdit()
        listing.setReadOnly(True)
        listing.setPlainText("\n".join(
            "- %s  ->  %s" % (t["title"], t["device_path"]) for t in s["tracks"]))
        listing.setMinimumHeight(min(220, 28 * len(s["tracks"]) + 20))
        v.addWidget(listing)

        warn = QLabel()
        warn.setWordWrap(True)
        warn.setStyleSheet("font-weight: 600;")
        warn.setText("One more step: restart your iPhone.\n"
                     "Apple's media service keeps the library open while the phone is "
                     "running, so the new tracks only appear in the Music app after a "
                     "restart. Everything is already saved and verified.")
        v.addWidget(warn)
        v.addWidget(QLabel("Backup taken before these changes: %s" % s["backup"]))

        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Got it")
        close.clicked.connect(dlg.accept)
        row.addWidget(close)
        v.addLayout(row)
        dlg.exec_()
        if self.device is not None:
            self.load_music()

    def on_progress(self, done, total, label):
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(done)
        self.status_label.setText(label)

    def on_transfer_finished(self, ok, msg):
        self.progress_bar.hide()
        # on_pushed() already reports the verified result for a push, so the
        # generic "Complete" box would just be a second, vaguer dialog.
        is_push = self.worker is not None and self.worker.mode == 'push'
        if ok:
            self.status_label.setText("Done!")
            if not is_push:
                QMessageBox.information(self, "Complete",
                                        msg or "Transfer completed successfully.")
            if self.device is not None:
                self.load_music()
        else:
            self.status_label.setText(f"Error: {msg}")
            QMessageBox.critical(self, "Error", msg or "Transfer failed.")

    def on_pushed(self, result):
        """Report what actually landed on the iPhone, verified by reading it back."""
        added = result.get("added", 0)
        verified = result.get("verified", 0)
        folder = result.get("folder", PUSH_DIR)
        noun = "track" if added == 1 else "tracks"

        lines = [
            f"{added} {noun} {'is' if added == 1 else 'are'} now in the "
            f"\"{folder}\" folder on your iPhone.",
            "",
            "To play them: open the Files app, go to On My iPhone, and tap "
            f"\"{folder}\".",
            "They will not show up in the Music app, because LinIOS does not "
            "modify your music library.",
            "",
        ]
        if result.get("results"):
            lines.append("Verified on the iPhone:")
            for r in result["results"]:
                mark = "ok" if r.get("ok") else "PROBLEM"
                name = r.get("name", "?")
                if r.get("skipped"):
                    mark = "already there"
                lines.append(f"  [{mark}] {name} - {r.get('reason', '')}")
            lines.append("")
        if verified < added:
            lines.append(f"Only {verified} of {added} could be confirmed. "
                         "The rest are listed in the log with the reason.")
        else:
            lines.append("Each file was read back off the iPhone and compared "
                         "with the original, so these are confirmed on the phone.")

        QMessageBox.information(self, "Added to iPhone", "\n".join(lines))

    def show_pushed(self):
        if not self._require_device():
            return
        dlg = PushedFilesDialog(self)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.show()

    def push_files(self):
        if not self._require_device():
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select music to add to your iPhone library", os.path.expanduser("~/Music"),
            "Music Files (*.mp3 *.m4a *.aac *.wav *.flac *.ogg *.aiff *.mp4)")
        if not paths:
            return
        names = [os.path.basename(p) for p in paths]
        preview = "\n".join("  - %s" % n for n in names[:8])
        if len(names) > 8:
            preview += "\n  ... and %d more" % (len(names) - 8)
        box = QMessageBox(self)
        box.setWindowTitle("Add to iPhone Library")
        box.setIcon(QMessageBox.Question)
        box.setText("Add %d track(s) to your iPhone Music library?" % len(paths))
        box.setInformativeText(
            "%s\n\nA backup of your library is taken first, and every file is "
            "verified after copying.\n\nYou will need to restart your iPhone "
            "once at the end for the Music app to see the new tracks."
            % preview)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        if box.exec_() != QMessageBox.Yes:
            return
        self._run_worker('library_push', paths)

    def push_files_folder(self):
        if not self._require_device():
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select music files to copy into the Files app", os.path.expanduser("~/Music"),
            "Music Files (*.mp3 *.m4a *.aac *.wav *.flac *.ogg *.aiff *.mp4)")
        if paths:
            self._run_worker('push', paths)

    def pull_all(self):
        if not self._require_device():
            return
        if self.remote_files:
            self._run_worker('pull')

    def pull_selected(self):
        if not self._require_device():
            return
        rows = [i.row() for i in self.music_list.selectedIndexes()]
        if not rows:
            QMessageBox.information(self, "Nothing Selected",
                                    "Please select one or more tracks first.")
            return
        paths = [self.remote_files[r] for r in rows]
        self._run_worker('pull_selected', paths)

    def delete_selected(self):
        if not self._require_device():
            return
        rows = [i.row() for i in self.music_list.selectedIndexes()]
        if not rows:
            QMessageBox.information(self, "Nothing Selected",
                                    "Please select one or more tracks first.")
            return
        tracks = [self.remote_files[r] for r in rows]
        names = "\n".join(self.track_label(t) for t in tracks)
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete these {(len(tracks))} track(s) from the iPhone?\n\n{names}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self._run_worker('delete', tracks)

    def choose_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Choose save folder", self.output_dir)
        if d:
            self.output_dir = d
            self.output_label.setText(f"Save folder:\n{d}")

    def closeEvent(self, event):
        if self.medialib_worker is not None and self.medialib_worker.isRunning():
            answer = QMessageBox.question(
                self, "Library operation running",
                "A library backup or restore is still running. Closing LinIOS "
                "now may leave it incomplete.\n\nClose anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.device_thread.stop()
        self.device_thread.wait(4000)
        if self.worker is not None and self.worker.isRunning():
            self.worker.wait(4000)
        if self.medialib_worker is not None and self.medialib_worker.isRunning():
            self.medialib_worker.wait(4000)
        event.accept()


def main():
    setup_logging()
    install_excepthooks()
    logging.info("LinIOS started at %s", datetime.now())
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setApplicationName("LinIOS")
    try:
        win = LinIOSApp()
        win.show()
        rc = app.exec_()
    except Exception:
        logging.critical("startup failed:\n%s", traceback.format_exc())
        try:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.critical(None, "LinIOS Error",
                                 "LinIOS failed to start.\nSee the log for details:\n" + LOG_FILE)
        except Exception:
            pass
        rc = 1
    logging.info("LinIOS exited with code %s", rc)
    sys.exit(rc)


if __name__ == "__main__":
    main()