import os
from unittest import TestCase

import pytest

from ..workers.mover import VideoTransferWorker
from ..workers.scanner import FindFilesWorker


MP4_Test_File = "./test/test_data/SampleVideo_1280x720_5mb.mp4"
MOV_Test_File = "./test/test_data/SampleVideo_1280x720_5mb.mov"
CLOUD_Test_file = "./test_data/test_duplication_method.mp4"
class TestVideoTransferWorker(TestCase):
    """Test suite for the VideoTransferWorker class."""
    TEST_WORKER = VideoTransferWorker(files=[MP4_Test_File, MOV_Test_File, CLOUD_Test_file],
    path="",
    space_name="test",
    max_workers=1,
    region="nyc3")

    def test_duplication_check(self):
        """Test the duplication check functionality."""
        result = []
        result.extend(self.TEST_WORKER.is_duplicate_upload(f) for f in self.TEST_WORKER.files)

        self.assertFalse(result[0])
        self.assertFalse(result[1])
        self.assertTrue(result[2])


if __name__ == "__main__":
    pytest.main()
