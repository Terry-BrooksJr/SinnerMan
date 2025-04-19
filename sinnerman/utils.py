import os
import tempfile
from typing import List

from tenacity import stop_after_attempt
from tenacity import wait_exponential


# Constants
DO_REGIONS: List[str] = ["nyc1", "nyc3", "ams3", "sfo2", "sfo3", "sgp1", "lon1", "fra1", "tor1", "blr1"]
FILE_SCAN_LOG_UPDATE_INTERVAL = 20
DEFAULT_VIDEO_EXTENSIONS: List[str] = [".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm"]
STATE_FILE = os.path.join(tempfile.gettempdir(), "transfer_state.json")
TRANSFER_TIMEOUT = 300  # seconds
RETRY_MULTIPLIER = 1
RETRY_MIN_WAIT = 4
RETRY_MAX_WAIT = 10
MAX_ATTEMPTS = stop_after_attempt(3)
WAIT_STRATEGY = wait_exponential(multiplier=RETRY_MULTIPLIER, min=RETRY_MIN_WAIT, max=RETRY_MAX_WAIT)
