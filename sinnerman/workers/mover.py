"""Handles asynchronous transfer of video files to DigitalOcean Spaces.

This module provides the `VideoTransferWorker` class, which uses multithreading
to upload multiple video files concurrently to a specified DigitalOcean Space.
It includes features for progress tracking, error handling, retries, and cancellation.
"""

import concurrent.futures
import json
import os
import time
from typing import Any
from typing import Dict
from typing import List
from typing import Tuple

import boto3
from boto3.exceptions import S3UploadFailedError
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ConnectionClosedError
from botocore.exceptions import EndpointConnectionError
from botocore.exceptions import NoCredentialsError
from botocore.exceptions import PartialCredentialsError
from ffmpeg import FFmpeg
from ffmpeg import FFmpegError
from loguru import logger
from PyQt5.QtCore import QThread
from PyQt5.QtCore import pyqtSignal
from tenacity import retry
from utils import FILE_SCAN_LOG_UPDATE_INTERVAL
from utils import MAX_ATTEMPTS
from utils import STATE_FILE
from utils import WAIT_STRATEGY
from utils import CompressionException
from utils import get_ffmpeg_binary_path


class VideoTransferWorker(QThread):
    """Manages the transfer of video files to a DigitalOcean Space.

    This worker utilizes multithreading to upload multiple files concurrently,
    tracks progress, handles errors, and supports cancellation. It also logs
    messages and emits signals to update the UI.
    """

    # Signals for progress updates and completion
    progress_updated = pyqtSignal(int, str, str)  # (file_index, status, message)
    transfer_complete = pyqtSignal(int, int)  # (success_count, fail_count)
    log_message = pyqtSignal(str)

    def __init__(
        self,
        files: List[str],
        path: str,
        space_name: str,
        max_workers: int,
        region: str = os.environ.get("DO_REGION", "nyc3"),
    ):
        """Initialize the VideoTransferWorker.
        Args:
            files (List[str]): List of video file paths to be transferred.
            space_name (str): Name of the DigitalOcean Space.
            max_workers (int): Maximum number of concurrent threads for file transfer.
            region (str): Region of the DigitalOcean Space.
        """
        super().__init__()
        self.files = files
        self.space_name = space_name
        self.is_running = False
        self.path = None if path is None or not path else path
        self.region = region
        self.max_workers = max_workers
        self._compression_suffix = os.environ.get("COMPRESSION_SUFFIX", "copmressed")
        self.success_rate = 0
        self.is_canceled = False
        self.total_files = len(self.files)
        self.ffmpeg = FFmpeg(executable=get_ffmpeg_binary_path())

        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=os.environ["DO_SPACES_KEY"],
            aws_secret_access_key=os.environ["DO_SPACES_SECRET"],
            endpoint_url=f"https://{self.region}.digitaloceanspaces.com",
            region_name=self.region,
        )

    def run(self) -> None:
        """Execute the file transfer process.

        This method orchestrates the overall transfer process by delegating to
        specialized helper methods for initialization, execution, and cleanup.
        """
        self.is_running = True

        # Initialize the transfer process
        if not self._initialize_transfer():
            self.is_running = False
            return

        # Execute the concurrent transfers
        successful, failed = self._execute_concurrent_transfers()

        # Handle completion and cleanup
        self._handle_completion(successful, failed)

        # Reset state and emit completion signal
        self._finalize_transfer(successful, failed)

    def _initialize_transfer(self) -> bool:
        """Initialize the transfer process and check for preconditions.

        Returns:
            bool: True if initialization succeeded and transfer should proceed, False otherwise.
        """
        self.restore_state()

        # Log the appropriate startup message based on state
        if self.is_canceled:
            self.log_message.emit("Transfer was canceled in a previous session.")
        elif self.total_files > 0:
            self.log_message.emit(
                f"Resuming transfer of {self.total_files} files with {self.max_workers} concurrent threads."
            )
        else:
            self.log_message.emit(f"Starting transfer with {self.max_workers} concurrent threads.")

        # Check if there are files to transfer
        if not self.files:
            self.log_message.emit("No files to transfer.")
            return False

        return True

    def _execute_concurrent_transfers(self) -> Tuple[int, int]:
        """Execute the concurrent file transfers using ThreadPoolExecutor.

        Returns:
            Tuple[int, int]: Count of successful and failed transfers.
        """
        total_files = len(self.files)
        successful = 0
        failed = 0

        # Using ThreadPoolExecutor for concurrent uploads
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all transfer tasks
            future_to_index = self._submit_transfer_tasks(executor)

            if not future_to_index and self.is_canceled:
                return 0, 0

            # Process results as they complete
            successful, failed = self._process_transfer_results(executor, future_to_index, total_files)

        return successful, failed

    def _submit_transfer_tasks(self, executor) -> Dict[concurrent.futures.Future, int]:
        """Submit transfer tasks to the executor.

        Args:
            executor: The ThreadPoolExecutor to submit tasks to.

        Returns:
            Dict[concurrent.futures.Future, int]: Dictionary mapping futures to file indices.
        """
        future_to_index = {}

        for i, file in enumerate(self.files):
            if self.is_canceled:
                self.log_message.emit("Transfer canceled before starting remaining tasks.")
                self.progress_updated.emit(i, "Canceled", "Transfer was canceled.")
                break

            key = os.path.basename(file)
            future = executor.submit(self.boto3_transfer, file, key, i)
            future_to_index[future] = i
            self.progress_updated.emit(i, "Queued", "Waiting to start...")

        return future_to_index

    def _process_transfer_results(self, executor, future_to_index, total_files) -> Tuple[int, int]:
        """Process the results of transfer tasks as they complete.

        Args:
            executor: The ThreadPoolExecutor running the tasks.
            future_to_index: Dictionary mapping futures to file indices.
            total_files: Total number of files being transferred.

        Return/                                    s:
            Tuple[int, int]: Count of successful and failed transfers.
        """
        successful = 0
        failed = 0

        for future in concurrent.futures.as_completed(future_to_index):
            if self.is_canceled:
                executor.shutdown(wait=False, cancel_futures=True)
                self.is_running = False
                self.log_message.emit("Transfer canceled. Waiting for tasks to finish...")
                break

            try:
                result, message, file_index = future.result()
            except Exception as e:
                file_index = future_to_index.get(future, -1)
                message = f"Exception: {e!s}"
                result = False
                logger.exception("Exception during file transfer")

            # Update counters and progress
            if result:
                successful += 1
                logger.debug(f"File transfer for {file_index} SUCCESS : {message}")
                self.progress_updated.emit(file_index, "Completed", message)
            else:
                failed += 1
                logger.error(f"File transfer of {self.files[file_index]} FAILED : {message}")
                self.progress_updated.emit(file_index, "Failed", message)

            # Log progress at intervals
            logger.info(f"Processed log: Successful: {successful} Failed: {failed}")
            self._log_progress(successful, failed, total_files)

        return successful, failed

    def _remove_file(self, file_index, file_path: str) -> bool:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"File {file_path} removed successfully.")
                self.progress_updated.emit(file_index, "Removed", "File Uploaded to S3 and removed successfully.")
                return True
            except Exception as e:
                logger.error(f"Error removing file {file_path}: {e}")
                self.progress_updated.emit(file_index, "Needs Manual Removal", f"Error removing file: {e}")
                return False
        else:
            logger.warning(f"File {file_path} does not exist.")
            return False

    def _log_progress(self, successful, failed, total_files):
        """Log the current transfer progress at specified intervals.

        Args:
            successful: Number of successful transfers.
            failed: Number of failed transfers.
            total_files: Total number of files being transferred.
        """
        total_processed = successful + failed
        if total_processed % FILE_SCAN_LOG_UPDATE_INTERVAL == 0 or total_processed == total_files:
            logger.info(f"Total processed: {total_processed}/{total_files}")
            self.log_message.emit(
                f"Progress: {total_processed}/{total_files} " f"({successful} successful, {failed} failed)"
            )

    def _handle_completion(self, successful, failed):
        """Handle the completion of all transfers and log results.

        Args:
            successful: Number of successful transfers.
            failed: Number of failed transfers.
        """
        total_files = len(self.files)

        if self.is_canceled:
            self.log_message.emit("Transfer canceled.")
        else:
            self.log_message.emit(
                f"Transfer completed. {successful}/{total_files} " f"files transferred successfully, {failed} failed."
            )

        # Save state to file
        self._save_transfer_state(successful, failed, total_files)

    def _save_transfer_state(self, successful, failed, total_files):
        """Save the transfer state to a file.

        Args:
            successful: Number of successful transfers.
            failed: Number of failed transfers.
            total_files: Total number of files.
        """
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {"successful": successful, "failed": failed, "total": total_files, "canceled": self.is_canceled}, f
                )
        except Exception as e:
            self.log_message.emit(f"Failed to save state: {e!s}")

    def _finalize_transfer(self, successful, failed):
        """Finalize the transfer by updating state and emitting completion signal.

        Args:
            successful: Number of successful transfers.
            failed: Number of failed transfers.
        """
        self.is_running = False
        self.transfer_complete.emit(successful, failed)
        self.log_message.emit("Transfer complete.")

    def restore_state(self):
        """Restores the transfer state from the state file."""
        if state := self._load_state_from_file():
            self.total_files = state.get("total_files", 0)
            try:
                self.success_rate = (state.get("successful", 0) / state.get("total_files", 0)) * 100
            except ZeroDivisionError:
                self.success_rate = 0
            self.is_canceled = state.get("canceled", False)
        else:
            self.total_files = 0
            self.success_rate = 0
            self.is_canceled = False

    def _load_state_from_file(self) -> Dict[str, Any] | None:
        """Loads the state from the state file. Handles file errors and returns None if the state cannot be loaded."""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                    if transfer_worker := state.get("transfer_worker"):
                        self.log_message.emit("Found previous transfer state.")
                        return {
                            "total_files": transfer_worker.get("total_files", 0),
                            "success_rate": transfer_worker.get("success_rate", 0),
                            "canceled": transfer_worker.get("canceled", False),
                        }
                    else:
                        return None  # Invalid state format
            except (json.JSONDecodeError, OSError) as e:
                self.log_message.emit(f"Error loading state file: {e}. Starting fresh.")
                return None
            except Exception as e:
                self.log_message.emit(f"Unexpected error loading state: {e}. Starting fresh.")
                return None
        else:
            self.log_message.emit("State file not found. Starting fresh.")
            return None

    @retry(stop=MAX_ATTEMPTS, wait=WAIT_STRATEGY)
    def boto3_transfer(
        self, file_path: str, key: str, file_index: int, bucket: str | None = None
    ) -> Tuple[bool, str, int]:
        """Transfer a single file to the DigitalOcean Space using boto3.

        This method coordinates the upload process by calling specialized helper functions
        and handles the overall flow and return values.
        """
        bucket = self.space_name if bucket is None else bucket

        # Check for cancellation
        if self._is_transfer_canceled(file_index):
            return False, "Transfer canceled", file_index

        # Check for duplicate files
        if self.is_duplicate_upload(key, bucket):
            self.progress_updated.emit(file_index, "Skipped", f"File already exists: {key}")
            return True, "File already exists", file_index

        # Prepare the file (compression and path handling)
        file_path, file_index = self._prepare_file(file_path, file_index)

        # Perform the actual upload
        success, message, file_index = self._perform_upload(file_path, bucket, key, file_index)
        if success:
            self._remove_file(file_index, file_path)

        return success, message, file_index

    def _is_transfer_canceled(self, file_index) -> bool:
        """Check if the transfer has been canceled and update progress if needed."""
        if self.is_canceled:
            self.progress_updated.emit(file_index, "Canceled", "Transfer was canceled.")
            return True
        return False

    def _prepare_file(self, file_path: str, file_index: int) -> Tuple[str, int]:
        """Prepare the file for upload, including compression and path resolution."""
        # Compress the video file if needed
        file_path = self.compress_video(file_path)
        self.progress_updated.emit(file_index, "In Progress", f"Starting upload: {file_path}")

        # Handle path resolution
        if self.path:
            file_path = os.path.join(self.path, file_path)

        return file_path, file_index

    def _perform_upload(self, file_path: str, bucket: str, key: str, file_index) -> Tuple[bool, str, int]:
        """Execute the actual file upload to S3 and handle any exceptions."""
        try:
            return self._perform_upload_to_s3(file_path, bucket, key, file_index)
        except S3UploadFailedError as e:
            logger.error(f"Upload failed: {e}")
            return False, f"Upload failed: {e!s}", file_index
        except NoCredentialsError:
            logger.error("No credentials provided.")
            return False, "No credentials provided.", file_index
        except PartialCredentialsError:
            logger.error("Incomplete credentials provided.")
            return False, "Incomplete credentials provided.", file_index
        except (EndpointConnectionError, ConnectionClosedError) as e:
            logger.error(f"Network error: {e}")
            return False, f"Network error: {e!s}", file_index
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return False, f"Unexpected error: {e!s}", file_index

    def _perform_upload_to_s3(self, file_path: str, bucket: str, key: str, file_index: int) -> Tuple[bool, str, int]:
        """Uploads the file to the specified S3 bucket.

        Logs the upload start and end times, and returns a success status and message.
        """
        self.log_message.emit(f"Uploading: {file_path} -> {bucket}/{key}")

        # Create multipart upload configuration
        config = self._create_transfer_config()

        # Record start time for performance tracking
        start = time.time()

        # Perform the upload
        self.s3_client.upload_file(file_path, bucket, key, Config=config)

        # Record and log elapsed time
        elapsed = time.time() - start
        self.log_message.emit(f"Uploaded {file_path} in {elapsed:.2f} seconds")

        return True, "Upload successful", file_index

    def _create_transfer_config(self) -> TransferConfig:
        """Create and return the S3 transfer configuration for multipart uploads."""
        return TransferConfig(
            multipart_threshold=50 * 1024 * 1024,  # 50MB
            max_concurrency=self.max_workers,
            multipart_chunksize=10 * 1024 * 1024,  # 10MB
            use_threads=True,
        )

    def compress_video(self, input_path: str) -> str:
        """Coordinates the video compression process.

        Attempts to compress the video at the given input path and returns the path to the compressed file. If compression fails, returns the original input path.
        """
        output_path = self._generate_compressed_filename(input_path)

        try:
            starting_size = self._get_and_log_starting_size(input_path)
            elapsed_time = self._execute_compression(input_path, output_path)
            logger.success(f"Compression of {input_path} completed in {elapsed_time:.2f} seconds")
            self._log_compression_results(input_path, output_path, starting_size, elapsed_time)
            return output_path
        except CompressionException as ce:
            self._log_compression_failure(input_path, ce)
            return input_path

    def _generate_compressed_filename(self, input_path: str) -> str:
        """Generate the output filename for the compressed video."""
        filename = f"{os.path.basename(input_path).split(".")[0]}.mp4"
        return os.path.join(os.path.dirname(input_path), filename)

    def _get_and_log_starting_size(self, input_path: str) -> float:
        """Get the starting file size and log the compression start."""
        return self.get_file_size_in_mb(input_path)

    def _execute_compression(self, input_path: str, output_path: str) -> float | None:
        """Execute the ffmpeg compression and return elapsed time."""
        self.log_message.emit(f"Compressing {input_path}")
        compression_start = time.time()
        try:
            ffmpeg_instance = FFmpeg(executable=get_ffmpeg_binary_path())
            ffmpeg_instance.option("y").input(input_path).output(
                output_path, vcodec="h264", crf=28, preset="medium"
            ).execute()
            return time.time() - compression_start
        except FFmpegError as fe:
            raise CompressionException(str(fe))

    def _log_compression_results(
        self, input_path: str, output_path: str, starting_size: float, elapsed_time: float | None
    ) -> None:
        """Calculate compression statistics and log the results."""
        ending_size = self.get_file_size_in_mb(output_path)
        reduction = starting_size - ending_size
        self.log_message.emit(f"Compressed {input_path} in {elapsed_time:.2f} seconds. Reduction: {reduction:.2f} MB")

    def _log_compression_failure(self, input_path: str, error: CompressionException) -> None:
        """Log a compression failure."""
        self.log_message.emit(f"Compression failed for {input_path}: {error}")

    def is_duplicate_upload(self, key: str, bucket: str | None = None) -> bool:
        """Checks if a file or its compressed version already exists in the bucket.

        This method verifies if either the original file specified by 'key' or its
        compressed counterpart exists in the given DigitalOcean Space bucket.

        Args:
            key (str): The key (file name) to check for existence.
            bucket (str, optional): The bucket to check within. Defaults to the worker's default bucket.

        Returns:
            bool: True if either the file or its compressed version exists, False otherwise.
        """
        if "compressed" in key.split("."):
            return True
        bucket = self.space_name if bucket is None else bucket
        compressed_key = self._generate_compressed_filename(key)
        try:
            self.s3_client.head_object(Bucket=bucket, Key=key)
            self.s3_client.head_object(Bucket=bucket, Key=compressed_key)
            return True
        except self.s3_client.exceptions.ClientError as e:
            if e.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                self.log_message.emit(f"Error checking existence of keys: {key} and {compressed_key}: {e}")
            return False

    def get_file_size_in_mb(self, file_path: str) -> float:
        """Get the size of the file in MB."""
        try:
            file_stats = os.stat(file_path)
            return file_stats.st_size / (1024 * 1024)  # Convert bytes to MB
        except Exception as e:
            self.log_message.emit(f"Error getting file size for {file_path}: {e}")
            return 0.0

    def cancel(self) -> None:
        """
        Cancel the ongoing transfers.
        """
        self.is_canceled = True
        self.log_message.emit("Canceling uploads... (This may take a moment)")
