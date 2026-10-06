"""Credential broker providing secret isolation outside the worker sandbox."""

import re
from typing import Dict, Optional


class CredentialBroker:
    """Manages secret isolation, ensuring workers never receive real credentials."""

    def __init__(self):
        self._secrets: Dict[str, str] = {}

    def register_secret(self, key: str, value: str) -> None:
        self._secrets[key] = value

    def get_secret(self, key: str) -> Optional[str]:
        return self._secrets.get(key)

    def mask_secrets(self, text: str) -> str:
        """Redacts registered secret values from logs, traces, and stdout."""
        masked = text
        for val in self._secrets.values():
            if val and len(val) >= 4:
                masked = masked.replace(val, "[REDACTED_SECRET]")
        return masked
