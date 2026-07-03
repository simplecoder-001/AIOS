"""
Custom log filters for the AIOS logging system.

Filters control which log records pass through to a handler.
This module provides:

    - LevelFilter       : Minimum-level gate (e.g., only WARNING+ to stderr)
    - ModuleFilter      : Include or exclude records by module/logger name
    - RateLimitFilter   : Suppress repeated messages within a time window
    - ContextFilter     : Inject persistent context fields into every record
"""

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set


class LevelFilter(logging.Filter):
    """
    Pass only records at or above a minimum level.

    Useful for routing WARNING+ to stderr while INFO goes to stdout,
    or for creating an errors-only file handler.
    """

    def __init__(self, min_level: int = logging.WARNING) -> None:
        super().__init__()
        self.min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.min_level


class ModuleFilter(logging.Filter):
    """
    Include or exclude records based on logger name patterns.

    Parameters:
        include : list of regex patterns to allow (None = allow all)
        exclude : list of regex patterns to deny  (None = deny none)

    Exclude takes precedence over include.
    """

    def __init__(
        self,
        include: Optional[list] = None,
        exclude: Optional[list] = None,
    ) -> None:
        super().__init__()
        self._include = [re.compile(p) for p in (include or [])]
        self._exclude = [re.compile(p) for p in (exclude or [])]

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name

        # Check exclusions first (higher priority)
        for pattern in self._exclude:
            if pattern.search(name):
                return False

        # If no include list, allow everything not excluded
        if not self._include:
            return True

        return any(pattern.search(name) for pattern in self._include)


@dataclass
class _RateBucket:
    """Internal bookkeeping for rate-limit tracking."""
    timestamps: deque = field(default_factory=lambda: deque(maxlen=1000))


class RateLimitFilter(logging.Filter):
    """
    Suppress repeated log messages within a configurable time window.

    Prevents log flooding from tight loops or recurring errors.
    Each unique (level, message) pair is tracked independently.

    Parameters:
        max_per_window : max occurrences allowed per window
        window_seconds : time window in seconds
        cooldown_seconds: after hitting the limit, suppress for this long
    """

    def __init__(
        self,
        max_per_window: int = 10,
        window_seconds: float = 60.0,
        cooldown_seconds: float = 30.0,
    ) -> None:
        super().__init__()
        self.max_per_window = max_per_window
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._buckets: Dict[str, _RateBucket] = {}
        self._suppressed_until: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _key(self, record: logging.LogRecord) -> str:
        return f"{record.levelno}:{record.getMessage()}"

    def filter(self, record: logging.LogRecord) -> bool:
        key = self._key(record)
        now = time.monotonic()

        with self._lock:
            # Check if currently in cooldown
            suppress_until = self._suppressed_until.get(key)
            if suppress_until is not None and now < suppress_until:
                return False
            if suppress_until is not None and now >= suppress_until:
                del self._suppressed_until[key]

            bucket = self._buckets.setdefault(key, _RateBucket())

            # Prune old timestamps
            cutoff = now - self.window_seconds
            while bucket.timestamps and bucket.timestamps[0] < cutoff:
                bucket.timestamps.popleft()

            if len(bucket.timestamps) >= self.max_per_window:
                # Enter cooldown
                self._suppressed_until[key] = now + self.cooldown_seconds
                return False

            bucket.timestamps.append(now)
            return True


class ContextFilter(logging.Filter):
    """
    Inject persistent key-value context into every log record.

    Useful for adding session_id, user_id, component, or request_id
    to all records flowing through a handler without modifying call sites.

    Usage:
        ctx = ContextFilter()
        ctx.set("session_id", "abc-123")
        ctx.set("component", "voice_pipeline")
        handler.addFilter(ctx)
        # Now every record from this handler has record.session_id = "abc-123"
    """

    def __init__(self, **initial_context: Any) -> None:
        super().__init__()
        self._context: Dict[str, Any] = dict(initial_context)
        self._lock = threading.Lock()

    def set(self, key: str, value: Any) -> None:
        """Set or update a context field."""
        with self._lock:
            self._context[key] = value

    def set_many(self, fields: Dict[str, Any]) -> None:
        """Set multiple context fields at once."""
        with self._lock:
            self._context.update(fields)

    def remove(self, key: str) -> None:
        """Remove a context field."""
        with self._lock:
            self._context.pop(key, None)

    def clear(self) -> None:
        """Clear all context fields."""
        with self._lock:
            self._context.clear()

    def get(self, key: str, default: Any = None) -> Any:
        return self._context.get(key, default)

    @property
    def context(self) -> Dict[str, Any]:
        """Return a snapshot of current context."""
        with self._lock:
            return dict(self._context)

    def filter(self, record: logging.LogRecord) -> bool:
        with self._lock:
            for key, value in self._context.items():
                # Don't overwrite fields already set by the caller
                if not hasattr(record, key):
                    setattr(record, key, value)
        return True
