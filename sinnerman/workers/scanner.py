import concurrent.futures
from pathlib import Path
from typing import List
from typing import Union

from loguru import logger
from PyQt5.QtCore import QThread
from PyQt5.QtCore import pyqtSignal
from utils import DEFAULT_VIDEO_EXTENSIONS
from utils import FILE_SCAN_LOG_UPDATE_INTERVAL


class FindFilesWorker(QThread):
    """A worker thread to find video files in a directory.

    This class emits signals to update progress and return the list of files found.
    """

    files_found = pyqtSignal(list)
    progress_update = pyqtSignal(str)

    def __init__(self, source_dir: str, video_extensions: List[str] | None = None):
        if video_extensions is None:
            video_extensions = []
        super().__init__()
        self.source_dir = source_dir
        self.video_extensions = DEFAULT_VIDEO_EXTENSIONS if video_extensions is None else video_extensions
        self.is_canceled = False
        self.is_running = False
        self.no_files_found = 0

    def run(self) -> None:
        """Scan the source directory for video files and emit signals with progress updates and results.

        This method recursively searches the source directory for video files with extensions
        specified in `self.video_extensions`. It emits progress updates every `FILE_SCAN_LOG_UPDATE_INTERVAL`
        files found and sends the final list of video files via the `files_found` signal.
        If the scan is canceled, it emits a "Scan canceled" message and an empty list of files.
        If an error occurs during the scan, it logs the exception and emits an error message with an empty list of files.
        """
        self.is_running = True
        try:
            root_path = Path(self.source_dir)
            video_files: List[str] = []

            self.progress_update.emit(f"Scanning directory: {self.source_dir}")

            def is_valid_video_file(path: Path) -> Union[str, None]:
                """Check if the path is a valid video file."""
                if self.is_canceled:
                    return None
                if not path.is_file():
                    return None
                if path.name.startswith("."):
                    return None
                return None if path.suffix.lower() not in self.video_extensions else str(path)

            paths = list(root_path.rglob("*"))
            with concurrent.futures.ThreadPoolExecutor() as executor:
                results = list(executor.map(is_valid_video_file, paths))

            video_files = [f for f in results if f is not None]

            for i, _f in enumerate(video_files):
                if self.is_canceled:
                    self.progress_update.emit("Scan canceled")
                    self.files_found.emit([])
                    self.is_running = False
                    return
                if i > 0 and i % FILE_SCAN_LOG_UPDATE_INTERVAL == 0:
                    self.progress_update.emit(f"Found {i} video files...")

            self.progress_update.emit(f"Scan complete. Found {len(video_files)} video files")
            self.files_found.emit(video_files)
            self.no_files_found = len(video_files)
        except Exception as e:
            logger.exception("Error scanning directory")
            self.progress_update.emit(f"Error scanning directory: {e!s}")
            self.files_found.emit([])
            self.is_running = False

        finally:
            self.is_running = False

    def cancel(self) -> None:
        """Cancel the ongoing file scanning process.

        Sets the is_canceled flag to True, which will stop the file scanning
        operation in the run method.
        """
        self.is_running = False
        self.is_canceled = True
