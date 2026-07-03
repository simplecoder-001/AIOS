"""
Core Logger class for the AIOS logging system.

Wraps Python's standard logging.Logger with a clean, type-safe API
that supports:
    - Structured extra context per log call
    - Child logger creation for hierarchical naming
    - Runtime level changes
    - Handler and filter management
    - Thread-safe operation (inherited from stdlib logging)
    - Context binding for request/session-scoped fields

The Logger is designed to be created by LoggerFactory but can also
be instantiated directly for custom configurations.
"""

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional


class LogLevel(IntEnum):
    """Log severity levels matching Python's logging constants."""
    DEBUG = logging.DEBUG
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR
    CRITICAL = logging.CRITICAL

    @classmethod
    def from_string(cls, name: str) -> "LogLevel":
        """Parse a level from a case-insensitive string."""
        return cls[name.upper()]


@dataclass
class LoggerConfig:
    """
    Declarative configuration for a Logger instance.

    Attributes:
        name       : hierarchical logger name (e.g., "voice.audio.capture")
        level      : minimum level this logger processes
        handlers   : list of handlers attached to this logger
        filters    : list of filters applied to all handlers
        propagate  : whether records propagate to parent loggers
    """
    name: str
    level: LogLevel = LogLevel.INFO
    handlers: List[logging.Handler] = field(default_factory=list)
    filters: List[logging.Filter] = field(default_factory=list)
    propagate: bool = False


class Logger:
    """
    High-level logger wrapping Python's logging.Logger.

    Provides a fluent, type-safe API while delegating all heavy lifting
    to the battle-tested stdlib logging machinery. This means thread
    safety, handler management, and formatting are all inherited from
    the standard library.

    Usage:
        config = LoggerConfig(
            name="voice.audio",
            level=LogLevel.DEBUG,
            handlers=[console_handler, file_handler],
        )
        logger = Logger(config)
        logger.info("Capture started", extra={"device": "USB Mic", "rate": 16000})

        child = logger.child("capture")
        child.debug("Chunk processed", extra={"chunk_id": 42})
    """

    def __init__(self, config: LoggerConfig) -> None:
        self._config = config
        self._logger = logging.getLogger(config.name)
        self._logger.setLevel(int(config.level))
        self._logger.propagate = config.propagate

        # Clear any pre-existing handlers (avoid duplicates on re-config)
        self._logger.handlers.clear()

        for handler in config.handlers:
            self._logger.addHandler(handler)

        for flt in config.filters:
            self._logger.addFilter(flt)

        self._bound_context: Dict[str, Any] = {}

    # -- properties --------------------------------------------------------

    @property
    def name(self) -> str:
        return self._logger.name

    @property
    def level(self) -> LogLevel:
        return LogLevel(self._logger.level)

    @property
    def handlers(self) -> List[logging.Handler]:
        return list(self._logger.handlers)

    @property
    def effective_level(self) -> LogLevel:
        return LogLevel(self._logger.getEffectiveLevel())

    # -- level management -------------------------------------------------

    def set_level(self, level: LogLevel) -> None:
        """Change the minimum log level at runtime."""
        self._logger.setLevel(int(level))
        self._config.level = level

    # -- context binding --------------------------------------------------

    def bind(self, **context: Any) -> "Logger":
        """
        Return a new Logger with additional bound context fields.

        Bound fields are automatically attached to every log record
        as extras. This is useful for request-scoped or session-scoped
        tracing (e.g., session_id, request_id, user_id).

        The original logger is not modified.
        """
        bound = Logger(LoggerConfig(
            name=self.name,
            level=self.level,
            handlers=list(self.handlers),
            propagate=self._logger.propagate,
        ))
        bound._bound_context = {**self._bound_context, **context}
        return bound

    def _merge_extras(self, extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        merged = dict(self._bound_context)
        if extra:
            merged.update(extra)
        return merged

    # -- child loggers ----------------------------------------------------

    def child(self, suffix: str) -> "Logger":
        """
        Create a child logger with a hierarchical name.

        The child inherits handlers from this logger via propagation
        if propagate=True, or gets its own handler copies if propagate=False.
        """
        child_name = f"{self.name}.{suffix}" if self.name else suffix
        child_config = LoggerConfig(
            name=child_name,
            level=self.level,
            handlers=[],  # children rely on propagation by default
            propagate=True,
        )
        return Logger(child_config)

    # -- logging methods --------------------------------------------------

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        extra = self._merge_extras(kwargs.pop("extra", None))
        self._logger.debug(msg, *args, extra=extra or None, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        extra = self._merge_extras(kwargs.pop("extra", None))
        self._logger.info(msg, *args, extra=extra or None, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        extra = self._merge_extras(kwargs.pop("extra", None))
        self._logger.warning(msg, *args, extra=extra or None, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        extra = self._merge_extras(kwargs.pop("extra", None))
        self._logger.error(msg, *args, extra=extra or None, **kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        extra = self._merge_extras(kwargs.pop("extra", None))
        self._logger.critical(msg, *args, extra=extra or None, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an ERROR with exception traceback attached."""
        extra = self._merge_extras(kwargs.pop("extra", None))
        self._logger.exception(msg, *args, extra=extra or None, **kwargs)

    def log(self, level: LogLevel, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log at a specific level."""
        extra = self._merge_extras(kwargs.pop("extra", None))
        self._logger.log(int(level), msg, *args, extra=extra or None, **kwargs)

    # -- handler / filter management -------------------------------------

    def add_handler(self, handler: logging.Handler) -> None:
        self._logger.addHandler(handler)
        self._config.handlers.append(handler)

    def remove_handler(self, handler: logging.Handler) -> None:
        self._logger.removeHandler(handler)
        if handler in self._config.handlers:
            self._config.handlers.remove(handler)

    def add_filter(self, flt: logging.Filter) -> None:
        self._logger.addFilter(flt)
        self._config.filters.append(flt)

    def remove_filter(self, flt: logging.Filter) -> None:
        self._logger.removeFilter(flt)
        if flt in self._config.filters:
            self._config.filters.remove(flt)

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Flush and close all handlers. Call on shutdown."""
        for handler in self._logger.handlers[:]:
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass
            self._logger.removeHandler(handler)

    # -- dunder -----------------------------------------------------------

    def __repr__(self) -> str:
        return f"<Logger name={self.name!r} level={self.level.name}>"
