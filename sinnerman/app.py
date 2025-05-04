#!/usr/bin/env python3
# mypy: ignore-errors
import gc
import json
import logging
import os
import re
import select
import subprocess
import sys
import threading
import time
import tracemalloc
from contextlib import contextmanager
from functools import wraps
from typing import List

import objgraph
import psutil
from log_logger import DEBUG_LOG_FILE
from log_logger import DEFAULT_LOG_FILE
from loguru import logger
from PyQt5.QtCore import QObject
from PyQt5.QtCore import QSettings
from PyQt5.QtCore import Qt
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtGui import QStandardItem
from PyQt5.QtGui import QStandardItemModel
from PyQt5.QtWidgets import QAction
from PyQt5.QtWidgets import QApplication
from PyQt5.QtWidgets import QComboBox
from PyQt5.QtWidgets import QDialog
from PyQt5.QtWidgets import QFileDialog
from PyQt5.QtWidgets import QFormLayout
from PyQt5.QtWidgets import QGroupBox
from PyQt5.QtWidgets import QHBoxLayout
from PyQt5.QtWidgets import QHeaderView
from PyQt5.QtWidgets import QLabel
from PyQt5.QtWidgets import QLineEdit
from PyQt5.QtWidgets import QListWidget
from PyQt5.QtWidgets import QMainWindow
from PyQt5.QtWidgets import QMenu
from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtWidgets import QProgressBar
from PyQt5.QtWidgets import QPushButton
from PyQt5.QtWidgets import QSpinBox
from PyQt5.QtWidgets import QStatusBar
from PyQt5.QtWidgets import QSystemTrayIcon
from PyQt5.QtWidgets import QTableView
from PyQt5.QtWidgets import QVBoxLayout
from PyQt5.QtWidgets import QWidget
from utils import DEFAULT_VIDEO_EXTENSIONS
from utils import DO_REGIONS
from utils import STATE_FILE
from utils import SpacesClient
from workers.key_manager import KeyManager
from workers.mover import VideoTransferWorker
from workers.scanner import FindFilesWorker


PROC = psutil.Process()


class ApplicationDoctor(QObject):
    """Provides diagnostic tools and monitoring for the application.

    This class encapsulates various methods for monitoring memory usage,
    garbage collection activity, thread count, and object growth,
    primarily for debugging purposes. It offers tools for taking memory
    snapshots, logging statistics, and performance monitoring.
    """

    debugging_enabled = pyqtSignal(bool)
    active_monitoring = pyqtSignal(bool)

    def __init__(
        self,
        app,
        key_manager: KeyManager | None = None,
        transfer_worker: VideoTransferWorker | None = None,
        scan_worker: FindFilesWorker | None = None,
    ):
        super().__init__()
        self.app = app
        self.key_manager = key_manager
        self.transfer_worker = transfer_worker
        self.scan_worker = scan_worker
        # self.debugging_enabled.connect(self.app.on_debugging_enabled)
        # self.active_monitoring.connect(self.app.on_active_monitoring)

    def get_debug_state(self) -> bool:
        return bool(self.app.is_in_diagnostic_mode or os.environ.get("DEBUG_MODE"))

    def enable_debug_state(self) -> None:
        os.environ["DEBUG_MODE"] = "True"
        logger.add(DEBUG_LOG_FILE, rotation="1 MB", level="DEBUG")
        logger.info("Diagnostics mode enabled")
        tracemalloc.start()
        gc.set_debug(gc.DEBUG_STATS)

    @contextmanager
    def memory_snapshot_context(self):
        yield tracemalloc.take_snapshot()

    def log_memory_snapshot(self) -> None:
        with self.memory_snapshot_context() as snapshot:
            top_stats = snapshot.statistics("lineno")
            for stat in top_stats[:5]:
                logger.debug(stat)

    def log_gc_activity(self):
        unreachable = gc.collect()
        logger.debug(f"GC collected {unreachable} unreachable objects")

    def log_thread_count(self):
        logger.debug(f"Thread count: {len(threading.enumerate())}")

    def log_memory_rss(self):
        rss = PROC.memory_info().rss / (1024**2)
        logger.debug(f"Memory RSS: {rss:.2f} MB")

    def log_object_growth(self):
        growth = objgraph.growth()
        logger.debug(f"Object growth: {growth[:5]}")

    # === Function Timing Decorator ===
    def monitor_performance(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            """Wraps the function to monitor its performance.

            Args:
                *args: Variable positional arguments passed to the wrapped function.
                **kwargs: Variable keyword arguments passed to the wrapped function.

            Returns:
                The result of the wrapped function.
            """
            if not self.get_debug_state():
                return func(*args, **kwargs)
            start = time.perf_counter()
            result = func(*args, **kwargs)
            duration = time.perf_counter() - start
            logger.debug(f"{func.__name__} executed in {duration:.4f}s")
            return result

        return wrapper

    # === Snapshot Utility ===
    def log_objgraph_snapshot(self):
        """Logs a memory snapshot using objgraph.

        Shows object growth, common types, and saves a reference graph.
        """
        logger.debug("Taking objgraph snapshot...")
        objgraph.show_growth(limit=10)
        objgraph.show_most_common_types(limit=10)
        objgraph.show_refs([objgraph.by_type("dict")[0]], filename="refs.png")
        logger.debug("Saved reference graph to refs.png")

    # === Periodic Reporter ===
    def memory_watcher(self, interval=30):
        """Periodically monitors and logs memory statistics.

        This function runs in a loop, checking memory usage, RSS, thread count,
        object growth, and garbage collection activity at the specified interval.

        Args:
            interval: The time interval (in seconds) between checks.
        """

        try:
            while self.get_debug_state():
                self.active_monitoring.emit(True)
                try:
                    logger.debug("===== Memory Report Start =====")
                    self.log_memory_snapshot()
                    self.log_memory_rss()
                    self.log_thread_count()
                    self.log_object_growth()
                    self.log_gc_activity()
                except Exception as e:
                    logger.exception(f"Memory watcher error: {e}")
                time.sleep(interval)
        finally:
            self.active_monitoring.emit(False)

        # === Start Monitoring ===
        def start_monitoring(self):
            """Starts memory monitoring in a separate thread if DEBUG_MODE is enabled.

            Initializes and starts the memory_watcher in a daemon thread.
            """
            if self.get_debug_state():
                logger.debug("Starting memory diagnostics...")
                t = threading.Thread(target=self.memory_watcher, daemon=True)
                t.start()

    # === Optional: Manual Hotkey Trigger for Snapshot ===
    def listen_for_snapshot_key(self, hotkey="s"):
        """Listens for a specified hotkey to trigger a memory snapshot.

        Starts a daemon thread that waits for the hotkey to be pressed and then calls log_objgraph_snapshot.
        """
        if not self.get_debug_state():
            return

        def watcher():
            print("Press 's' to save snapshot")
            while True:
                try:
                    if select.select([sys.stdin], [], [], 1.0)[0]:
                        user_input = sys.stdin.readline().strip()
                        if user_input.lower() == hotkey:
                            self.log_objgraph_snapshot()
                except Exception:
                    break

        threading.Thread(target=watcher, daemon=True).start()

    def toggle_monitoring(self):
        """Toggles memory monitoring on or off.

        Starts a new daemon thread for memory_watcher if monitoring is enabled,
        otherwise logs a message indicating that monitoring is disabled.
        """
        self.app.is_in_diagnostic_mode = not self.app.is_in_diagnostic_mode
        if self.app.is_in_diagnostic_mode:
            self.debugging_enabled.emit(True)
            logger.info("Memory monitoring ENABLED via GUI")
            os.environ["DEBUG_MODE"] = "True"
            threading.Thread(target=self.memory_watcher, daemon=True).start()
        else:
            logger.info("Memory monitoring DISABLED via GUI")

    def gui_snapshot_trigger(self):
        """Triggers a memory snapshot if DEBUG_MODE is enabled.

        Logs a warning message if called outside of DEBUG mode.
        """
        if not self.get_debug_state():
            logger.warning("Snapshot trigger ignored; not in DEBUG mode")
            return
        self.log_objgraph_snapshot()

    def enable(self):
        """Enables the application's debugging mode."""
        try:
            assert self.get_debug_state() is True
            self.enable_debug_state()
            threading.Thread(target=self.memory_watcher, daemon=True).start()
            self.listen_for_snapshot_key()
        except AssertionError:
            QMessageBox().warning(
                self.app, title="Error Starting Debugger", text="Application is not in diagnostic mode."
            )
            logger.warning("Application is not in diagnostic mode.")


class MainWindow(QMainWindow):
    """Main window for the video transfer tool.

    Provides the user interface for selecting a source directory, configuring Digital Ocean Spaces settings,
    scanning for video files, and initiating the transfer process.
    """

    def __init__(self):
        super().__init__()
        KeyManager.ensure_credentials(self)  # <-- prompt if needed

        self.setWindowTitle("Video Transfer Tool - Digital Ocean Spaces")
        self.setMinimumSize(800, 600)

        self.video_files: List[str] = []
        self.transfer_worker: VideoTransferWorker = None
        self.find_worker: FindFilesWorker = None
        self.application_doctor: ApplicationDoctor = None
        self.is_in_diagnostic_mode = None
        # Load settings
        self.settings = QSettings("MyCompany", "VideoTransferTool")
        self.load_settings()

        self.init_ui()
        self.init_diagnostic_settings_ui()

    def init_ui(self) -> None:
        """Initialize the user interface.

        Creates and arranges the widgets, loads settings
        """
        central_widget = QWidget()
        main_layout = QVBoxLayout(central_widget)

        # Source directory section
        source_group = QGroupBox("Source Directory")
        source_layout = QHBoxLayout()
        self.source_dir_input = QLineEdit()
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_source_directory)
        scan_btn = QPushButton("Scan for Videos")
        scan_btn.clicked.connect(self.scan_videos)
        source_layout.addWidget(self.source_dir_input)
        source_layout.addWidget(browse_btn)
        source_layout.addWidget(scan_btn)
        source_group.setLayout(source_layout)

        # Digital Ocean settings
        do_group = QGroupBox("Digital Ocean Spaces Settings")
        do_layout = QFormLayout()
        self.region_combo = QComboBox()
        self.region_combo.addItems(DO_REGIONS)
        # Set default region from saved settings or default to nyc3
        if spaces := self.get_spaces():
            self.space_name_input = QComboBox()
            self.space_name_input.addItems(spaces)
        else:
            self.space_name_input = QLineEdit()
        saved_region = self.settings.value("region", "nyc3")
        self.region_combo.setCurrentText(saved_region)
        self.space_path_input = QLineEdit()

        self.threads_input = QSpinBox()
        self.threads_input.setRange(1, 20)
        self.threads_input.setValue(int(self.settings.value("threads", 4)))
        do_layout.addRow("Space Name:", self.space_name_input)
        do_layout.addRow("Region:", self.region_combo)
        do_layout.addRow("Space Path (optional):", self.space_path_input)
        do_layout.addRow("Concurrent Uploads:", self.threads_input)
        do_group.setLayout(do_layout)

        # File list section
        self.file_table_view = QTableView()
        self.file_model = QStandardItemModel(0, 3)
        self.file_model.setHorizontalHeaderLabels(["File", "Status", "Message"])
        self.file_table_view.setModel(self.file_model)
        self.file_table_view.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.file_table_view.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.file_table_view.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)

        # Overall progress
        progress_layout = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_label = QLabel("Ready")
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.progress_label)

        # Action buttons
        button_layout = QHBoxLayout()
        self.start_btn = QPushButton("Start Upload")
        self.start_btn.clicked.connect(self.start_upload)
        self.start_btn.setEnabled(False)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_operation)
        self.cancel_btn.setEnabled(False)
        self.open_log_btn = QPushButton("Open Debug Settings")
        self.open_log_btn.clicked.connect(self.show_diagnostic_settings)
        self.open_log_btn.setEnabled(True)
        self.open_general_log_btn = QPushButton("Open General Logs")
        self.open_general_log_btn.clicked.connect(self.open_logs)
        self.open_general_log_btn.setEnabled(True)
        button_layout.addStretch()
        button_layout.addWidget(self.start_btn)
        button_layout.addWidget(self.open_log_btn)
        button_layout.addWidget(self.cancel_btn)
        button_layout.addWidget(self.open_general_log_btn)

        # Log area using QListWidget for simplicity
        self.log_list = QListWidget()
        self.log_list.setMaximumHeight(150)

        # Assemble layout
        main_layout.addWidget(source_group)
        main_layout.addWidget(do_group)
        main_layout.addWidget(self.file_table_view)
        main_layout.addLayout(progress_layout)
        main_layout.addLayout(button_layout)
        main_layout.addWidget(QLabel("Log:"))
        main_layout.addWidget(self.log_list)
        self.setCentralWidget(central_widget)

        # System Tray
        self.tray = QSystemTrayIcon(self)
        self.tray_icon = QIcon("sinnerman/assets/icon_tray.png")
        self.tray.setIcon(self.tray_icon)
        self.tray.setVisible(True)
        self.tray.setToolTip("SinnerMan - Video Transfer Tool")
        # Tray Context Menu
        self.tray_menu = QMenu(self)
        #  Status Bar
        self.status_bar = QStatusBar()
        self.status_label = QLabel("Ready to Get it Started!")
        self.setStatusBar(self.status_bar)
        self.status_bar.addPermanentWidget(self.status_label)
        self.tray_menu.addSeparator()
        # Add actions to the menu
        # Add a Scan Directory option to the menu.
        self.scan = QAction("Scan Directory")
        # TODO: Connect this to the scan_videos method
        self.tray_menu.addAction(self.scan)
        # Add a Upload Video Files option to the menu.
        self.upload = QAction("Upload Video Files")
        # TODO: Connect this to the start_upload method
        self.tray_menu.addAction(self.upload)
        # Add a Quit option to the menu.
        self.tray_quit_action = QAction("Quit")
        # FIXME: self.tray_quit_action.triggered.connect(app.quit)
        self.tray_menu.addAction(self.tray_quit_action)
        self.tray.setContextMenu(self.tray_menu)

        self.add_log_message("Application started. Ready to transfer videos to Digital Ocean Spaces.")
        self.add_log_message(f"Log file: {os.path.abspath(DEFAULT_LOG_FILE)}")

        # Load Last Used Settings
        if last_source_dir := self.settings.value("source_dir", ""):
            self.source_dir_input.setText(last_source_dir)
        if last_space_name := self.settings.value("space_name", ""):
            self.space_name_input.setCurrentText(last_space_name)

    def browse_source_directory(self) -> None:
        """Opens a file dialog to select a source directory.

        If a directory is selected, updates the source directory input field.
        """
        if directory := QFileDialog.getExistingDirectory(self, "Select Source Directory"):
            self.source_dir_input.setText(directory)

    def get_spaces(self) -> List[str]:
        return SpacesClient.list_spaces(self.region_combo.currentText())

    def open_debug_log(self) -> None:
        """Open the debug log file in the default text editor.

        Checks if the log file exists and opens it using the appropriate
        system command based on the platform (Windows, macOS, or Linux).
        Displays a warning message if the log file is not found.
        """
        if os.path.exists(DEBUG_LOG_FILE):
            if sys.platform == "win32":
                os.startfile(DEBUG_LOG_FILE)
            else:
                subprocess.call(["open" if sys.platform == "darwin" else "xdg-open", DEBUG_LOG_FILE])
        else:
            QMessageBox.warning(self, "Error", "Log file not found")

    def open_logs(self) -> None:
        """Open the debug log file in the default text editor.

        Checks if the log file exists and opens it using the appropriate
        system command based on the platform (Windows, macOS, or Linux).
        Displays a warning message if the log file is not found.
        """
        if os.path.exists(DEFAULT_LOG_FILE):
            if sys.platform == "win32":
                os.startfile(DEFAULT_LOG_FILE)
            else:
                subprocess.call(["open" if sys.platform == "darwin" else "xdg-open", DEFAULT_LOG_FILE])
        else:
            QMessageBox.warning(self, "Error", "Log file not found")

    def create_doctor_diagnostics(self) -> None:
        """Create a new instance of the ApplicationDoctor class"""
        doc = self.application_doctor = ApplicationDoctor(
            app=self, transfer_worker=self.transfer_worker or None, scan_worker=self.find_worker or None
        )
        self.application_doctor = doc
        return None

    def trigger_debug_mode(self, enabled: bool) -> None:
        if enabled:
            dialog = QMessageBox()
            dialog.information(self, "Diagnostics", "Preparing To Enable Debugger...!")
            dialog.information(self, "Diagnostics", "Checking For Application Doctor Diagnostics Agent...")
            if not self.application_doctor:
                dialog.information(
                    self, "Diagnostics", "Application Doctor Diagnostics Agent Not Found...Creating New Instance..."
                )
                self.create_doctor_diagnostics()
            else:
                dialog.information(
                    self, "Diagnostics", "Application Doctor Diagnostics Agent Found...Enabling Debugger..."
                )
            self.application_doctor.enable_debug_state()
            self.application_doctor.enable()
            dialog.information(
                self, "Diagnostics", "Debugger Enabled...Please Check Log File For Debugging Information..."
            )

    def scan_videos(self) -> None:
        """Scans the source directory for video files.
        s
                Retrieves the source directory path, validates it, and initiates
                the file scanning worker to find video files. Updates UI elements
                and logs messages accordingly.
        """
        source_dir = self.source_dir_input.text().strip()
        if not source_dir:
            QMessageBox.warning(self, "Error", "Please select a source directory.")
            return
        if not os.path.isdir(source_dir):
            QMessageBox.warning(self, "Error", "The specified source directory does not exist.")
            return

        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.add_log_message(f"Scanning for video files in: {source_dir}")
        # Save the source directory in settings
        self.settings.setValue("source_dir", source_dir)

        # Clear previous file list
        self.video_files = []
        self.file_model.removeRows(0, self.file_model.rowCount())

        # Start file scanning worker
        self.find_worker = FindFilesWorker(source_dir, DEFAULT_VIDEO_EXTENSIONS)
        self.find_worker.files_found.connect(self.on_files_found)
        self.find_worker.progress_update.connect(self.add_log_message)
        self.find_worker.progress_update.connect(self.update_tray_status)
        self.find_worker.start()

    def on_files_found(self, files: List[str]) -> None:
        """Handles the completion of the file scanning process.

        Updates the file list model with the found files, resets the progress bar,
        enables the start button, and logs a message.
        """
        self.video_files = files
        self.file_model.removeRows(0, self.file_model.rowCount())
        self._populate_file_model(files, "Ready")
        if files:
            self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.add_log_message(f"Found {len(files)} video files.")

    def start_upload(self) -> None:
        """Starts the video upload process.

        This method initiates the upload of video files to Digital Ocean Spaces.
        It retrieves settings, validates input, updates file statuses, and starts the transfer worker.
        """
        if not self.video_files:
            QMessageBox.warning(self, "Error", "No video files to upload.")
            return
        space_name = self.space_name_input.currentText().strip()
        if not space_name:
            QMessageBox.warning(self, "Error", "Please enter a Space name.")
            return

        region = self.region_combo.currentText()
        path = self.space_path_input.text().strip()
        thread_count = self.threads_input.value()

        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        # Update file statuses
        for row in range(self.file_model.rowCount()):
            self.file_model.item(row, 1).setText("Queued")
            self.file_model.item(row, 2).setText("")

        # Start the transfer worker
        self.transfer_worker = VideoTransferWorker(
            files=self.video_files, space_name=space_name, region=region, max_workers=thread_count, path=path
        )
        self.transfer_worker.progress_updated.connect(self.update_file_progress)
        self.transfer_worker.transfer_complete.connect(self.on_transfer_complete)
        self.transfer_worker.log_message.connect(self.add_log_message)
        self.transfer_worker.log_message.connect(self.update_tray_status)
        self.transfer_worker.start()

    def update_file_progress(self, file_index: int, status: str, message: str) -> None:
        """
        Update file row in the model with new status and message.
        """
        if 0 <= file_index < self.file_model.rowCount():
            self.file_model.item(file_index, 1).setText(status)
            self.file_model.item(file_index, 2).setText(message)
            # Update background color based on status
            if status == "Completed":
                self.file_model.item(file_index, 1).setBackground(Qt.green)
            elif status == "Failed":
                self.file_model.item(file_index, 1).setBackground(Qt.red)
            elif status == "In Progress":
                self.file_model.item(file_index, 1).setBackground(Qt.yellow)
            elif status == "Canceled":
                self.file_model.item(file_index, 1).setBackground(Qt.lightGray)
            # Update overall progress bar
            completed_count = 0
            for row in range(self.file_model.rowCount()):
                curr_status = self.file_model.item(row, 1).text()
                if curr_status in ["Completed", "Failed", "Canceled"]:
                    completed_count += 1
            self.progress_bar.setValue(completed_count)
            self.progress_label.setText(f"{completed_count}/{self.file_model.rowCount()} completed")

    def on_transfer_complete(self, success_count: int, fail_count: int) -> None:
        """Handle the completion of the video transfer.

        This method is called when the video transfer worker finishes.
        It re-enables the start button, disables the cancel button,
        displays a completion message, and shows a message box with the results.

        Args:
            success_count: The number of videos successfully transferred.
            fail_count: The number of videos that failed to transfer.
        """
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        message = f"Transfer completed: {success_count} successful, {fail_count} failed"
        self.progress_label.setText(message)
        QMessageBox.information(self, "Transfer Complete", message)

    def cancel_operation(self) -> None:
        if self.find_worker and self.find_worker.isRunning():
            self.find_worker.cancel()
            # Mark all entries as canceled if not already completed
            for row in range(self.file_model.rowCount()):
                if self.file_model.item(row, 1).text() == "Queued":  # type: ignore
                    self.file_model.item(row, 1).setText("Canceled")  # type: ignore
                    self.file_model.item(row, 1).setBackground(Qt.lightGray)  # type: ignore
        if self.transfer_worker and self.transfer_worker.isRunning():
            self.transfer_worker.cancel()
        self.cancel_btn.setEnabled(False)
        self.add_log_message("Cancel operation initiated.")

    def add_log_message(self, message: str) -> None:
        """
        Append a message to the log UI and log file.
        """
        self.log_list.addItem(message)
        self.log_list.scrollToBottom()
        logging.info(message)

    def load_settings(self) -> None:
        """Load saved settings.

        Loads settings using QSettings. Any additional
        initialization can be added here.
        """
        # QSettings auto-loads previously stored values; additional initialization can be added here if needed.

    def save_settings(self) -> None:
        """
        Save current settings.
        """
        self.settings.setValue("source_dir", self.source_dir_input.text().strip())
        self.settings.setValue("region", self.region_combo.currentText())
        self.settings.setValue("threads", self.threads_input.value())
        self.settings.setValue("space_name", self.space_name_input.currentText().strip())

    def try_restore_previous_state(self):
        """Attempts to restore the application's previous state from a temporary file.

        This method checks for a state file in the temporary directory. If found,
        it prompts the user to restore the session and, if confirmed, loads the state.
        """
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                if (
                    state.get("total", 0) > 0
                    and QMessageBox.question(self, "Restore Session", "Restore files from last session?")
                    == QMessageBox.Yes
                ):
                    self.get_state(state)
            except Exception as e:
                self.add_log_message(f"Error restoring session: {e}")

    def get_state(self, state):
        """
        Restores the previous state of the application by populating the file model
        and resetting the progress bar and start button.

        Args:
            state (dict): A dictionary containing the previous state of the application.
                          It should include "successful" and "failed" keys, each mapping
                          to a list of file paths.

        Behavior:
            - Combines the "successful" and "failed" file paths from the state.
            - Updates the `video_files` attribute with the combined file paths.
            - Adds each file path to the file model with its corresponding status and tooltip.
            - Resets the progress bar's maximum value to the number of file paths and sets its value to 0.
            - Enables the start button.
        """
        file_paths = state.get("successful", []) + state.get("failed", [])
        self.video_files = file_paths
        self._populate_file_model(file_paths, "Previously Scanned")
        self.start_btn.setEnabled(True)

    def _populate_file_model(self, file_paths: List[str], initial_status: str) -> None:
        """Populates the file model with the given file paths and initial status.

        Args:
            file_paths: A list of file paths to add to the model.
            initial_status: The initial status to display for each file.
        """
        for file_path in file_paths:
            file_item = QStandardItem(os.path.basename(file_path))
            file_item.setToolTip(file_path)
            status_item = QStandardItem(initial_status)
            message_item = QStandardItem("")
            self.file_model.appendRow([file_item, status_item, message_item])
        self.progress_bar.setMaximum(len(file_paths))
        self.progress_bar.setValue(0)

    def closeEvent(self, event) -> None:  # noqa: N802
        """Handle the close event of the main window.
        This method is called when the user attempts to close the window.
        It saves the current settings and closes the application.
        Args:
            event: The close event object.
        """
        # Save settings before closing
        self.save_settings()
        event.accept()

    def update_tray_status(self, message: str) -> None:
        """Update the system tray tooltip and message area."""
        if not hasattr(self, "tray"):
            return
        diagnositic_prefix = "Diagnostic mode ENABLED: "
        if re.search(r"^Scanning directory:\s*(.+)$", message):
            self.update_tray_and_status(
                (
                    f"{diagnositic_prefix}Scanning for videos..."
                    if self.is_in_diagnostic_mode
                    else "Scanning for videos..."
                ),
                "sinnerman/assets/icon_tray_scanning.png",
            )
        elif re.search(r"^Scan\scanceled$", message):
            self.update_tray_and_status(
                (f"{diagnositic_prefix}Scan canceled" if self.is_in_diagnostic_mode else "Scan canceled"),
                "sinnerman/assets/icon_tray.png",
            )
        elif re.search(r"^Scan\scomplete", message):
            self.update_tray_and_status(
                (
                    f"{diagnositic_prefix}Successfully completed scanning"
                    if self.is_in_diagnostic_mode
                    else "Successfully completed scanning"
                ),
                "sinnerman/assets/icon_tray.png",
            )
        elif re.search(r"^Starting\suploads", message):
            self.update_tray_and_status(
                (f"{diagnositic_prefix}Uploading videos..." if self.is_in_diagnostic_mode else "Uploading videos..."),
                "sinnerman/assets/icon_tray_uploading.png",
            )
        elif re.search(r"^Transfer\sscomplete", message):
            self.update_tray_and_status(
                (
                    f"{diagnositic_prefix}Successfully completed All Transfers"
                    if self.is_in_diagnostic_mode
                    else "Successfully completed All Transfers"
                ),
                "sinnerman/assets/icon_tray.png",
            )
        elif re.search(r"^Upload\scanceled$", message):
            self.update_tray_and_status(
                "Transfers canceled",
                "sinnerman/assets/icon_tray.png",
            )
            self.update_tray_and_status(
                "Transfers canceled",
                "sinnerman/assets/icon_tray.png",
            )

    # TODO Rename this here and in `update_tray_status`
    def update_tray_and_status(self, status_msg: str, icon_path: str) -> None:
        """Updates the tray icon and status bar message.

        Args:
            status: The new status message to display.
            icon_path: The path to the icon to display in the tray.
        """
        self.status_bar.clearMessage()
        self.status_bar.showMessage(status_msg)
        self.tray.setIcon(QIcon(icon_path))
        self.status_label.setText(status_msg)

    def init_diagnostic_settings_ui(self):
        self.diagnostic_window = QDialog(self)
        self.diagnostic_window.setModal(True)  # <-- Makes it a modal popup
        self.diagnostic_window.setWindowTitle("Diagnostic Settings")
        self.diagnostic_window.setWindowIcon(QIcon("sinnerman/assets/icon_dock.icns"))
        self.diagnostic_window.setFixedSize(400, 200)

        # Create the main layout
        self.diagnostic_layout = QVBoxLayout()

        # Current settings display
        self.current_diagnostic_settings_layout = QVBoxLayout()
        self.current_diagnostic_settings_label = QLabel("Current Diagnostic Settings:")
        # FIXME: self.active_diagnostic_settings_icon = QIcon('sinnerman/assets/active.png')
        # FIXME: self.inactive_diagnostic_settings_iconactive_diagnostic_settings_icon = QIcon("sinnerman/assets/inactive.png")

        # FIXME:self.active_icon = self.active_diagnostic_settings_icon.pixmap(
        # FIXME: self.inactive_icon = self.inactive_diagnostic_settings_icon.pixmap(h=20, w=20)
        # FIXME: self.current_diagnostic_settings_icon = QLabel()
        # FIXME: self.current_diagnostic_settings_icon.setFixedSize(20, 20)
        # FIXME: self.current_diagnostic_settings_icon.setPixmap(self.inactive_icon)
        self.current_diagnostic_settings = QLabel(
            f"Diagnostic Mode: {'Enabled' if self.is_in_diagnostic_mode else 'Disabled'}"
        )
        self.current_diagnostic_settings_layout.addWidget(self.current_diagnostic_settings_label)
        self.current_diagnostic_settings_layout.addWidget(self.current_diagnostic_settings)
        self.diagnostic_layout.addLayout(self.current_diagnostic_settings_layout)

        # FIXME: self.current_diagnostic_settings_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Buttons
        self.diagnostic_enablement_layout = QVBoxLayout()
        self.enable_monitor_button = QPushButton(
            "Disable Diagnostic Mode" if self.is_in_diagnostic_mode else "Enable Diagnostic Mode"
        )
        self.snapshot_button = QPushButton("Take Memory Snapshot")
        self.view_log_button = QPushButton("View Diagnostics Log")
        self.view_log_button.setToolTip("View the diagnostic log file")

        self.diagnostic_enablement_layout.addWidget(self.enable_monitor_button)
        self.diagnostic_enablement_layout.addWidget(self.snapshot_button)
        self.diagnostic_enablement_layout.addWidget(self.view_log_button)
        self.diagnostic_layout.addLayout(self.diagnostic_enablement_layout)

        # Close button
        self.close_diagnostic_settings_btn = QPushButton("Close Settings")
        self.close_diagnostic_settings_btn.clicked.connect(self.diagnostic_window.close)
        self.diagnostic_layout.addWidget(self.close_diagnostic_settings_btn)

        # Connect actions
        self.view_log_button.clicked.connect(self.open_debug_log)
        self.snapshot_button.clicked.connect(self.trigger_manual_snapshot)
        self.enable_monitor_button.clicked.connect(self.toggle_debugging)

        # Set layout
        self.diagnostic_window.setLayout(self.diagnostic_layout)

    def trigger_manual_snapshot(self):
        if not self.application_doctor:
            self.create_doctor_diagnostics()
            if not self.application_doctor.get_debug_state():
                return
            self.application_doctor.gui_snapshot_trigger()
            self.update_tray_and_status(
                "Memory Snapshot Taken",
                "sinnerman/assets/icon_tray.png",
            )

    def show_diagnostic_settings(self):
        self.diagnostic_window.exec_()  # <-- This shows it as a modal dialog

    def close_diagnostic_settings(self):
        self.diagnostic_window.close()

    def toggle_debugging(self):
        debug_state = self.is_in_diagnostic_mode
        debug_state = not debug_state
        self.is_in_diagnostic_mode = debug_state
        self.trigger_debug_mode(debug_state)

    def on_active_monitoring(self, active: bool):
        logger.info(f"Active monitoring state: {active}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon("sinnerman/assets/icon_dock.icns"))
    window = MainWindow()
    app.setApplicationName("SinnerMan - Fuking Video Transfer Tool")
    app.setApplicationVersion("1.0")

    window.show()
    sys.exit(app.exec())
