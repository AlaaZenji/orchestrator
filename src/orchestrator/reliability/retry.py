"""Retry policy, exponential backoff, and budget enforcement."""

import math
import random
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RetryPolicy:
    """Configurable exponential backoff and jitter policy."""
    max_retries: int = 3
    initial_interval_seconds: float = 1.0
    backoff_coefficient: float = 2.0
    maximum_interval_seconds: float = 60.0
    jitter_factor: float = 0.2

    def calculate_delay(self, attempt: int) -> float:
        """Calculates backoff delay with randomization jitter."""
        if attempt <= 0:
            return 0.0
        calculated = self.initial_interval_seconds * (self.backoff_coefficient ** (attempt - 1))
        capped = min(calculated, self.maximum_interval_seconds)
        if self.jitter_factor > 0:
            jitter_range = capped * self.jitter_factor
            jitter = random.uniform(-jitter_range, jitter_range)
            capped = max(0.0, capped + jitter)
        return capped

    def should_retry(self, current_retry_count: int) -> bool:
        """Determines whether another retry attempt is legally permitted."""
        return current_retry_count < self.max_retries
