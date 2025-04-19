# Sinnerman - Video Transfer Tool
SinnerMan is a Python-based application with a graphical interface built using PyQt5. It uses multithreading for performance and provides seamless video transfer capabilities to DigitalOcean Spaces.
⸻

## 🏆 Features
	•	PyQt5 GUI: User-friendly interface to manage transfers
	•	Multithreading: High performance, concurrent processing
	•	DigitalOcean Integration: Seamless video upload to Spaces

⸻

## 🗂️ Project Structure and Key Components

### 📁 Directory Structure
```
sinnerman/
├── app.py                  # Main GUI application using PyQt5
├── workers/                # Background worker threads
│   ├── mover.py            # Handles video compression & upload to DigitalOcean
│   └── scanner.py          # Scans directories asynchronously for video files
├── .editorconfig           # Code style configuration
├── .gitattributes
├── .gitignore
├── poetry.lock             # Locked dependencies
├── pyproject.toml          # Poetry configuration
├── requirements.txt        # Production dependencies
└── requirements-dev.txt    # Development dependencies
```


⸻
### 🧠 Class Responsibilities

#### 🔄 mover.py
Handles asynchronous video transfer, compression, duplicate detection, and error handling.
```mermaid
classDiagram
class VideoTransferWorker {
  -files: List[str]
  -space_name: str
  -is_running: bool
  -region: str
  -max_workers: int
  -success_rate: float
  -is_canceled: bool
  -s3_client: boto3.client
  +__init__(...)
  +run() : None
  +boto3_transfer(...) : Tuple[bool, str, int]
  +compress_video(...) : str
  +is_duplicate_upload(...) : bool
  +get_file_size_in_mb(...) : float
  +cancel() : None
}
VideoTransferWorker --|> PyQt5.QtCore.QThread
VideoTransferWorker ..> boto3
VideoTransferWorker ..> ffmpeg
note for VideoTransferWorker "Manages the transfer of video files to a DigitalOcean Space."
```
#### 🔍 scanner.py
Asynchronously scans a directory for video files, emitting signals to keep the UI responsive.
```mermaid
classDiagram
class FindFilesWorker {
  -source_dir: str
  -video_extensions: List[str]
  -is_canceled: bool
  -is_running: bool
  -no_files_found: int
  +files_found: pyqtSignal
  +progress_update: pyqtSignal
  +__init__(...)
  +run() : None
  +cancel() : None
}
QThread <|-- FindFilesWorker
note for FindFilesWorker "Recursively scans a directory to find video files."
```
#### 🖥️ app.py
Main GUI for interacting with the app—select source dir, scan for videos, configure uploads, and start the transfer.
```mermaid
classDiagram
class MainWindow {
  -video_files: List[str]
  -transfer_worker: VideoTransferWorker
  -find_worker: FindFilesWorker
  -settings: QSettings
  +__init__()
  +init_ui()
  +browse_source_directory()
  +scan_videos()
  +on_files_found(...)
  +start_upload()
  +update_file_progress(...)
  +on_transfer_complete(...)
  +cancel_operation()
  +add_log_message(...)
  +load_settings()
  +save_settings()
  +closeEvent(event)
  +update_tray_status(...)
  +get_buckets()
  +try_restore_previous_state()
  +get_state(state)
}
MainWindow --|> QMainWindow
MainWindow *-- VideoTransferWorker
MainWindow *-- FindFilesWorker
note for MainWindow "Manages the main application window and its functionalities."
```
____
## 🚧 Roadmap
	•	Integrated Payment Support
	•	Cross-Platform Compatibility
