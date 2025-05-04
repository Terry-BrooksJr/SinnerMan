import os

import keyring
from PyQt5.QtWidgets import QInputDialog
from PyQt5.QtWidgets import QMessageBox


class KeyManager:
    """Manages DigitalOcean API credentials.

    This class provides functionality to ensure the DO_SPACES_KEY and DO_SPACES_SECRET
    environment variables are set. It prompts the user for credentials if they are
    missing and stores them securely using keyring.
    """

    SERVICE_NAME = "SinnerMan"
    ORG_NAME = "Blackberry-Py Development"
    APP_NAME = "VideoTransferTool"

    @classmethod
    def ensure_credentials(cls, parent=None) -> None:
        """
        Ensure the DO_SPACES_KEY and DO_SPACES_SECRET are available.
        Prompts the user if missing, then stores them in keyring.
        """
        key = keyring.get_password(cls.SERVICE_NAME, "DO_SPACES_KEY")
        secret = keyring.get_password(cls.SERVICE_NAME, "DO_SPACES_SECRET")

        if not key:
            key, ok1 = QInputDialog.getText(parent, "DigitalOcean API Key", "Enter your Digital Ocean API Key:")
            if ok1 and key:
                keyring.set_password(cls.SERVICE_NAME, "DO_SPACES_KEY", key)
            else:
                QMessageBox.critical(
                    parent, "Missing Credentials", "DigitalOcean  Spaces API Key credential is required."
                )
                raise RuntimeError("DigitalOcean API Key not provided")
        if not secret:
            secret, ok2 = QInputDialog.getText(parent, "DigitalOcean Secret", "Enter your Digital Ocean  API Secret:")
            if ok2 and secret:
                keyring.set_password(cls.SERVICE_NAME, "DO_SPACES_SECRET", secret)
            else:
                QMessageBox.critical(
                    parent, "Missing Credentials", "DigitalOcean Spaces Secret credential is required."
                )
                raise RuntimeError("DigitalOcean Spaces Secret not provided")

        # Set env vars so boto3 and other code can use them
        current_key = os.environ.get("DO_SPACES_KEY")
        current_secret = os.environ.get("DO_SPACES_SECRET")
        if current_key != key or current_secret != secret:
            os.environ["DO_SPACES_KEY"] = key
            os.environ["DO_SPACES_SECRET"] = secret
