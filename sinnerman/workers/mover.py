"""Handles asynchronous transfer of video files to DigitalOcean Spaces.

This module provides the `VideoTransferWorker` class, which uses multithreading
to upload multiple video files concurrently to a specified DigitalOcean Space.
It includes features for progress tracking, error handling, retries, and cancellation.
"""

import concurrent.futures
import json
import os
import time
from typing import List
from typing import Tuple

import boto3
import ffmpeg
from boto3.exceptions import S3UploadFailedError
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ConnectionClosedError
from botocore.exceptions import EndpointConnectionError
from botocore.exceptions import NoCredentialsError
from botocore.exceptions import PartialCredentialsError
from loguru import logger
from PyQt5.QtCore import QThread
from PyQt5.QtCore import pyqtSignal
from tenacity import retry
from utils import FILE_SCAN_LOG_UPDATE_INTERVAL
from utils import MAX_ATTEMPTS
from utils import STATE_FILE
from utils import WAIT_STRATEGY


class VideoTransferWorker(QThread):
    """Manages the transfer of video files to a DigitalOcean Space.

    This worker utilizes multithreading to upload multiple files concurrently,
    tracks progress, handles errors, and supports cancellation. It also logs
    messages and emits signals to update the UI.
    """

    progress_updated = pyqtSignal(int, str, str)  # (file_index, status, message)
    transfer_complete = pyqtSignal(int, int)  # (success_count, fail_count)
    log_message = pyqtSignal(str)

    def __init__(self, files: List[str], space_name: str, max_workers: int, region: str = os.environ["DO_REGION"]):
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
        self.region = region
        self.max_workers = max_workers
        self.success_rate = 0
        self.is_canceled = False
        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=os.environ["DO_SPACES_KEY"],
            aws_secret_access_key=os.environ["DO_SPACES_SECRET"],
            endpoint_url=f"https://{self.region}.digitaloceanspaces.com",
            region_name=self.region,
        )

    def run(self) -> None:
        """Execute the file transfer process.

        This method initiates and manages the concurrent upload of video files
        to the specified DigitalOcean Space. It handles progress tracking,
        cancellation, error management, and logging.
        """
        self.is_running = True
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                    self.log_message.emit(f"Previous transfer state: {state}")
            except Exception as e:
                self.log_message.emit(f"Could not read previous state: {e}")

        successful = 0
        failed = 0
        total_files = len(self.files)
        self.log_message.emit(f"Starting uploads with {self.max_workers} concurrent threads")

        # Using ThreadPoolExecutor for concurrent uploads
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_index = {}
            for i, file in enumerate(self.files):
                if self.is_canceled:
                    self.log_message.emit("Transfer canceled before starting remaining tasks.")
                    self.is_running = False
                    self.progress_updated.emit(i, "Canceled", "Transfer was canceled.")
                    break
                key = os.path.basename(file)
                future = executor.submit(self.boto3_transfer, file, key, i)
                future_to_index[future] = i
                self.progress_updated.emit(i, "Queued", "Waiting to start...")

            # Process results as they complete
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
                if result:
                    successful += 1
                    self.progress_updated.emit(file_index, "Completed", message)
                else:
                    failed += 1
                    self.progress_updated.emit(file_index, "Failed", message)

                total_processed = successful + failed
                if total_processed % FILE_SCAN_LOG_UPDATE_INTERVAL == 0 or total_processed == total_files:
                    self.log_message.emit(
                        f"Progress: {total_processed}/{total_files} " f"({successful} successful, {failed} failed)"
                    )
        if self.is_canceled:
            self.log_message.emit("Transfer canceled.")
            self.is_running = False
        else:
            self.log_message.emit(
                f"Transfer completed. {successful}/{total_files} " f"files transferred successfully, {failed} failed."
            )
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(
                    {"successful": successful, "failed": failed, "total": total_files, "canceled": self.is_canceled}, f
                )
        except Exception as e:
            self.log_message.emit(f"Failed to save state: {e!s}")
        finally:
            self.is_running = False

        self.transfer_complete.emit(successful, failed)
        self.log_message.emit("Transfer complete.")
        self.is_running = False

    @retry(stop=MAX_ATTEMPTS, wait=WAIT_STRATEGY)
    def boto3_transfer(
        self, file_path: str, key: str, file_index: int, bucket: str | None = None
    ) -> Tuple[bool, str, int]:
        """Transfer a single file to the DigitalOcean Space using boto3.

        This method attempts to upload the given file to the specified bucket
        with retries. It handles various exceptions and returns a tuple indicating
        success or failure, a message, and the file index.
        """
        bucket = self.space_name if bucket is None else bucket
        # Check cancellation before proceeding
        if self.is_canceled:
            self.progress_updated.emit(file_index, "Canceled", "Transfer was canceled.")
            return False, "Transfer canceled", file_index
        try:
            if self.is_duplicate_upload(key, bucket):
                self.progress_updated.emit(file_index, "Skipped", f"File already exists: {key}")
                return True, "File already exists", file_index
            # Compress the video file if needed
            start = time.time()
            file_path = self.compress_video(file_path)
            self.progress_updated.emit(file_index, "In Progress", f"Starting upload: {file_path}")
            self.log_message.emit(f"Uploading: {file_path} -> {bucket}/{key}")
            # Use multipart upload configuration for large files
            config = TransferConfig(
                multipart_threshold=50 * 1024 * 1024,  # 50MB
                max_concurrency=self.max_workers,
                multipart_chunksize=10 * 1024 * 1024,  # 10MB
                use_threads=True,
            )
            self.s3_client.upload_file(file_path, bucket, key, Config=config)
            elapsed = time.time() - start
            self.log_message.emit(f"Uploaded {file_path} in {elapsed:.2f} seconds")
        except S3UploadFailedError as e:
            logger.error(f"Upload failed: {e}")
            return False, f"Upload failed: {e!s}", file_index
        except NoCredentialsError:
            logger.error("No credentials provided.")
            return False, "No credentials provided.", file_index
        except PartialCredentialsError:
            logger.error("Incomplete credentials provided.")
            return False, "Incomplete credentials provided.", file_index
        except EndpointConnectionError as e:
            logger.error(f"Endpoint connection error: {e}")
            return False, f"Network error: {e!s}", file_index
        except ConnectionClosedError as e:
            logger.error(f"Connection closed unexpectedly: {e}")
            return False, f"Network error: {e!s}", file_index
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return False, f"Unexpected error: {e!s}", file_index
        return True, "Upload successful", file_index

    def compress_video(self, input_path: str) -> str:
        """Compress the video using ffmpeg-python and return the path to the compressed file."""
        output_path = f"{input_path}.compressed.mp4"
        try:
            starting_size = self.get_file_size_in_mb(input_path)
            self.log_message.emit(f"Compressing {input_path} to {output_path}")
            compression_time = time.time()
            (
                ffmpeg.input(input_path)
                .output(output_path, vcodec="libx264", crf=28, preset="medium")
                .run(quiet=True, overwrite_output=True)
            )
            elapsed = time.time() - compression_time
            ending_size = self.get_file_size_in_mb(output_path)
            reduction = starting_size - ending_size
            self.log_message.emit(f"Compressed {input_path} in {elapsed:.2f} seconds. Reduction: {reduction:.2f} MB")
            return output_path
        except Exception as e:
            self.log_message.emit(f"Compression failed for {input_path}: {e}")
            return input_path

    def is_duplicate_upload(self, key: str, bucket: str | None = None) -> bool:
        bucket = self.space_name if bucket is None else bucket
        try:
            self.s3_client.head_object(Bucket=bucket, Key=key)
            return True
        except self.s3_client.exceptions.ClientError as e:
            if e.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                self.log_message.emit(f"Error checking existence of {key}: {e}")
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
