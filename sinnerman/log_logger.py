import logging
import os
import sys
import warnings
from tempfile import gettempdir

from loguru import logger


logger.remove()


def log_warning(message, category, filename, lineno, file=None, line=None):
    logger.warning(f" {message}")


# Set up logging
if not os.path.exists(gettempdir()):
    os.makedirs(gettempdir())
DEBUG_LOG_FILE = os.path.join(gettempdir(), "debug_video_transfer.log")
DEFAULT_LOG_FILE = os.path.join(gettempdir(), "default_video_transfer.log")
DEFAULT_HANDLER = sys.stdout

# noaq: E501
logger.add(
    DEFAULT_LOG_FILE,
    level="INFO",
    rotation="5 MB",
    retention="10 days",
    compression="zip",
    format="{time} {level} {message}",
)  # noaq: E501
logger.add(DEFAULT_HANDLER, level="INFO", format="{time} {level} {message}")
logger.add(sys.stderr, level="ERROR", format="{time} {level} {message}")


warnings.filterwarnings(action="ignore", message=r"w+")
warnings.showwarning = log_warning
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(DEBUG_LOG_FILE), logging.StreamHandler()],
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(DEFAULT_LOG_FILE), logging.StreamHandler(DEFAULT_HANDLER)],
)
