import json
import os
import sys
import tempfile
from enum import Enum
from typing import Any
from typing import Dict
from typing import List
from typing import Literal

import boto3
from loguru import logger
from tenacity import stop_after_attempt
from tenacity import wait_exponential


def get_ffmpeg_binary_path():
    """Get the path to the FFmpeg binary.

    This function determines the correct path to the FFmpeg binary based on
    whether the script is running within a PyInstaller bundle or not.
    If in a bundle, it returns the path within the bundle; otherwise,
    it assumes FFmpeg is available in the system's PATH.

    Returns:
        The path to the FFmpeg binary.
    """
    if getattr(sys, "frozen", False):
        # We're in a PyInstaller bundle
        return os.path.join(getattr(sys, "_MEIPASS", ""), "bin", "ffmpeg")
    return "ffmpeg"


class SpacesClient:
    @staticmethod
    def list_spaces(region: str = "nyc3") -> List[str]:
        try:
            s3_client = boto3.client(
                "s3",
                aws_access_key_id=os.environ["DO_SPACES_KEY"],
                aws_secret_access_key=os.environ["DO_SPACES_SECRET"],
                endpoint_url=f"https://{os.environ.get('DO_REGION', region)}.digitaloceanspaces.com",
                region_name=os.environ.get("DO_REGION", region),
            )
            response = s3_client.list_buckets()
            buckets = [bucket["Name"] for bucket in response["Buckets"]]
            if not buckets:
                logger.warning("No buckets found.")
            return buckets
        except Exception as e:
            logger.warning("Error retrieving buckets: %s", e)
            return []


# Constants
DO_REGIONS: List[str] = ["nyc1", "nyc3", "ams3", "sfo2", "sfo3", "sgp1", "lon1", "fra1", "tor1", "blr1"]
FILE_SCAN_LOG_UPDATE_INTERVAL = 20
DEFAULT_VIDEO_EXTENSIONS: List[str] = [".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm"]
STATE_FILE = os.path.join(tempfile.gettempdir(), "transfer_state.json")
TRANSFER_TIMEOUT: Literal[300] = 300  # seconds
RETRY_MULTIPLIER: Literal[1] = 1
RETRY_MIN_WAIT: Literal[4] = 4
RETRY_MAX_WAIT: Literal[10] = 10
MAX_ATTEMPTS = stop_after_attempt(3)
WAIT_STRATEGY = wait_exponential(multiplier=RETRY_MULTIPLIER, min=RETRY_MIN_WAIT, max=RETRY_MAX_WAIT)


def update_state_file(state_file: str, key: str, new_state: dict) -> None:
    """Update a specific section of the state file with new state information.

    This function opens the specified state file, reads the existing JSON data,
    updates a section identified by the provided key with the given state, and
    writes the modified JSON back to the file.
    """
    with open(file=state_file, mode="a", encoding="utf-8") as f:
        # Convert Currrent state file to Python dict
        state_dict: Dict[str, Any] = json.load(f)
        # Get the current state
        updating_state: Dict[str, Any] = state_dict.get(key, {})
        # Update the specific section with the new state
        updating_state = new_state
        # Save the updated state back to the file
        updated_state_json = json.dumps(state_dict)
        f.write(updated_state_json)


class StateAction(Enum):
    SET = "SET"
    GET = "GET"


class CompressionException(BaseException):
    """Exception raised when compression fails."""

    pass


class MediaTransferException(BaseException):
    """Exception raised when media transfer fails."""

    pass


class DirectoryScanException(BaseException):
    """Exception raised when directory scan fails."""

    pass


class WorkerError(BaseException):
    pass
