"""
AIOS Logging System
===================

Production-grade logging infrastructure for the AI Operating System.

Provides:
    - Console logger with ANSI color support
    - File logger with detailed formatting
    - Rotating file logger (size-based and time-based)
    - JSON structured logging for machine consumption
    - Audit logger with HMAC-SHA256 integrity verification
    - Custom filters (level, module, rate-limit, context)
    - Logger factory for centralized logger creation

Usage:
    from app.logging import LoggerFactory, LogLevel

    factory = LoggerFactory()
    logger = factory.create_console_logger("my_app", LogLevel.DEBUG)
    logger.info("System initialized")

    file_logger = factory.create_rotating_logger(
        "voice.audio",
        LogLevel.INFO,
        "logs/audio/capture.log",
        max_bytes=10 * 1024 * 1024,
        backup_count=5,
    )
    file_logger.info("Microphone capture started")
"""

from app.logging.logger import Logger, LogLevel, LoggerConfig
from app.logging.logger_factory import LoggerFactory, LoggerType
from app.logging.formatters import (
    BaseFormatter,
    ConsoleFormatter,
    JSONFormatter,
    DetailedFileFormatter,
    ColorCodes,
)
from app.logging.handlers import (
    ConsoleHandler,
    FileLogHandler,
    RotatingFileLogHandler,
    CompositeHandler,
)
from app.logging.filters import (
    LevelFilter,
    ModuleFilter,
    RateLimitFilter,
    ContextFilter,
)
from app.logging.rotation import RotationType, RotationConfig
from app.logging.audit_logger import AuditLogger, AuditEntry

__all__ = [
    # Logger
    "Logger",
    "LogLevel",
    "LoggerConfig",
    # Factory
    "LoggerFactory",
    "LoggerType",
    # Formatters
    "BaseFormatter",
    "ConsoleFormatter",
    "JSONFormatter",
    "DetailedFileFormatter",
    "ColorCodes",
    # Handlers
    "ConsoleHandler",
    "FileLogHandler",
    "RotatingFileLogHandler",
    "CompositeHandler",
    # Filters
    "LevelFilter",
    "ModuleFilter",
    "RateLimitFilter",
    "ContextFilter",
    # Rotation
    "RotationType",
    "RotationConfig",
    # Audit
    "AuditLogger",
    "AuditEntry",
]

__version__ = "1.0.0"
