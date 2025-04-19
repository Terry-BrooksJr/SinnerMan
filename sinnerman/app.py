#!/usr/bin/env python3
# mypy: ignore-errors
import json
import logging
import os
import re
import sys
from tempfile import gettempdir
from typing import List

import boto3
from log_logger import DEFAULT_LOG_FILE
from loguru import logger
from PyQt5.QtCore import QSettings
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtGui import QStandardItem
from PyQt5.QtGui import QStandardItemModel
from PyQt5.QtWidgets import QAction
from PyQt5.QtWidgets import QApplication
from PyQt5.QtWidgets import QComboBox
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
from workers.mover import VideoTransferWorker
from workers.scanner import FindFilesWorker


class MainWindow(QMainWindow):
    def get_buckets(self) -> List[str]:
        """Retrieve a list of available buckets.

        Connects to Digital Ocean Spaces using boto3 and retrieves
        a list of all available buckets. Logs a warning if no buckets
        are found and an error if any exception occurs during the process.

        Returns:
            A list of bucket names (strings). Returns an empty list if
            an error occurs or no buckets are found.
        """
        try:
            self.s3_client = boto3.client(
                "s3",
                aws_access_key_id=os.environ["DO_SPACES_KEY"],
                aws_secret_access_key=os.environ["DO_SPACES_SECRET"],
                endpoint_url=f"https://{os.environ["DO_REGION"]}.digitaloceanspaces.com",
                region_name=os.environ["DO_REGION"],
            )
            response = self.s3_client.list_buckets()
            buckets = [bucket["Name"] for bucket in response["Buckets"]]
            if len(buckets) == 0:
                logger.warning("No buckets found.")
            return buckets
        except Exception as e:
            logging.error(f"Error retrieving buckets: {e}")
            return []

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Transfer Tool - Digital Ocean Spaces")
        self.setMinimumSize(800, 600)

        self.video_files: List[str] = []
        self.transfer_worker: VideoTransferWorker = None
        self.find_worker: FindFilesWorker = None

        # Load settings
        self.settings = QSettings("MyCompany", "VideoTransferTool")
        self.load_settings()

        self.init_ui()

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
        self.space_name_input = QComboBox()
        self.region_combo = QComboBox()
        self.region_combo.addItems(DO_REGIONS)
        # Set default region from saved settings or default to nyc3
        for bucket in self.get_buckets():
            self.space_name_input.addItem(bucket)
        saved_region = self.settings.value("region", "nyc3")
        self.region_combo.setCurrentText(saved_region)
        self.space_path_input = QLineEdit()

        self.threads_input = QSpinBox()
        self.threads_input.setRange(1, 32)
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
        button_layout.addStretch()
        button_layout.addWidget(self.start_btn)
        button_layout.addWidget(self.cancel_btn)

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
        # Tray Menu Status Bar
        self.status_bar = QStatusBar()
        self.status_label = QLabel("Ready to Get it Started!")
        self.setStatusBar(self.status_bar)
        self.status_bar.addPermanentWidget(self.status_label)
        self.menu.addSeparator()
        # Add actions to the menu
        # Add a Scan Directory option to the menu.
        self.scan = QAction("Scan Directory")
        # TODO: Connect this to the scan_videos method
        self.menu.addAction(self.scan)
        # Add a Upload Video Files option to the menu.
        self.upload = QAction("Upload Video Files")
        # TODO: Connect this to the start_upload method
        self.menu.addAction(self.upload)
        # Add a Quit option to the menu.
        self.tray_quit_action = QAction("Quit")
        # FIXME: self.tray_quit_action.triggered.connect(app.quit)
        self.menu.addAction(self.tray_quit_action)
        self.tray.setContextMenu(self.tray_menu)

        self.add_log_message("Application started. Ready to transfer videos to Digital Ocean Spaces.")
        self.add_log_message(f"Log file: {os.path.abspath(DEFAULT_LOG_FILE)}")

        if last_source_dir := self.settings.value("source_dir", ""):
            self.source_dir_input.setText(last_source_dir)

    def browse_source_directory(self) -> None:
        if directory := QFileDialog.getExistingDirectory(self, "Select Source Directory"):
            self.source_dir_input.setText(directory)

    def scan_videos(self) -> None:
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
        self.video_files = files
        self.file_model.removeRows(0, self.file_model.rowCount())
        for file_path in files:
            file_item = QStandardItem(os.path.basename(file_path))
            file_item.setToolTip(file_path)
            status_item = QStandardItem("Ready")
            message_item = QStandardItem("")
            self.file_model.appendRow([file_item, status_item, message_item])
        self.progress_bar.setMaximum(len(files))
        self.progress_bar.setValue(0)
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
        self.space_path_input.text().strip()
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
            files=self.video_files, space_name=space_name, region=region, max_workers=thread_count
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
        """
        Load saved settings.
        """
        # QSettings auto-loads previously stored values; additional initialization can be added here if needed.

    def save_settings(self) -> None:
        """
        Save current settings.
        """
        self.settings.setValue("source_dir", self.source_dir_input.text().strip())
        self.settings.setValue("region", self.region_combo.currentText())
        self.settings.setValue("threads", self.threads_input.value())

    def try_restore_previous_state(self):
        """Attempts to restore the application's previous state from a temporary file.

        This method checks for a state file in the temporary directory. If found,
        it prompts the user to restore the session and, if confirmed, loads the state.
        """
        state_file = os.path.join(gettempdir(), "transfer_state.json")
        if os.path.exists(state_file):
            try:
                with open(state_file, "r") as f:
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
        for file_path in file_paths:
            file_item = QStandardItem(os.path.basename(file_path))
            file_item.setToolTip(file_path)
            status_item = QStandardItem("Previously Scanned")
            message_item = QStandardItem("")
            self.file_model.appendRow([file_item, status_item, message_item])
        self.progress_bar.setMaximum(len(file_paths))
        self.progress_bar.setValue(0)
        self.start_btn.setEnabled(True)

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
        if hasattr(self, "tray"):
            if re.search(r"^Scanning directory:\s*(.+)$", message):
                self.status_bar.clearMessage()
                self.status_bar.showMessage("Scanning for videos...")
            elif re.search(r"^Scan\scanceled$", message):
                self.status_bar.clearMessage()
                self.status_bar.showMessage("Scan canceled")
            elif re.search(r"^Scan\scomplete", message):
                self.status_bar.clearMessage()
                self.status_bar.showMessage("Successfully completed scanning")
            elif re.search(r"^Starting\suploads", message):
                self.status_bar.clearMessage()
                self.status_bar.showMessage("Uploading videos...")
            elif re.search(r"^Transfer\sscomplete", message):
                self.status_bar.clearMessage()
                self.status_bar.showMessage("All Transfers completed")
            elif re.search(r"^Upload\scanceled$", message):
                self.status_bar.clearMessage()
                self.status_bar.showMessage("Transfers canceled")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon("sinnerman/assets/icon_dock.icns"))
    window = MainWindow()
    app.setApplicationName("SinnerMan - Fuking Video Transfer Tool")
    app.setApplicationVersion("1.0")

    window.show()
    sys.exit(app.exec())
