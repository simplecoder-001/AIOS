"""
Logger factory for centralized logger creation.

The factory caches loggers by name to prevent duplicate handler
attachment (a common stdlib logging pitfall). It provides convenience
methods for the most common logger configurations:

    - create_console_logger       : stdout/stderr only, colored
    - create_file_logger          : single file, no rotation
    - create_rotating_logger      : file with size/time rotation
    - create_audit_logger         : HMAC-signed audit trail
    - create_composite_logger     : console + file combined
    - create_json_logger          : JSON-structured file output

For advanced use cases, use create_logger() with a full LoggerConfig.
"""

import logging
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Union

from app.logging.filters import ContextFilter, LevelFilter
from app.logging.formatters import (
    ConsoleFormatter,
    DetailedFileFormatter,
    JSONFormatter,
)
from app.logging.handlers import (
    CompositeHandler,
    ConsoleHandler,
    FileLogHandler,
    RotatingFileLogHandler,
)
from app.logging.logger import Logger, LoggerConfig, LogLevel
from app.logging.rotation import RotationConfig, RotationType
from app.logging.audit_logger import AuditLogger


class LoggerType(Enum):
    """Pre-defined logger archetypes for the factory."""
    CONSOLE = "console"
    FILE = "file"
    ROTATING = "rotating"
    COMPOSITE = "composite"
    JSON = "json"
    AUDIT = "audit"


class LoggerFactory:
    """
    Centralized factory for creating and caching Logger instances.

    The factory maintains a registry of created loggers. Requesting
    the same logger name twice returns the same instance, preventing
    duplicate handlers and ensuring consistent configuration.

    Usage:
        factory = LoggerFactory()

        # Console-only logger for development
        dev_logger = factory.create_console_logger("dev", LogLevel.DEBUG)

        # Rotating file logger for production
        prod_logger = factory.create_rotating_logger(
            name="voice.stt",
            level=LogLevel.INFO,
            file_path="logs/stt/transcription.log",
            max_bytes=20 * 1024 * 1024,  # 20 MB
            backup_count=10,
        )

        # Combined console + file
        combined = factory.create_composite_logger(
            name="app",
            level=LogLevel.INFO,
            file_path="logs/app.log",
        )
    """

    def __init__(self) -> None:
        self._loggers: Dict[str, Logger] = {}
        self._audit_loggers: Dict[str, AuditLogger] = {}

    # -- registry management ----------------------------------------------

    def get(self, name: str) -> Optional[Logger]:
        """Retrieve a previously created logger by name."""
        return self._loggers.get(name)

    def get_audit(self, name: str) -> Optional[AuditLogger]:
        """Retrieve a previously created audit logger by name."""
        return self._audit_loggers.get(name)

    def is_registered(self, name: str) -> bool:
        return name in self._loggers

    def unregister(self, name: str) -> None:
        """Remove a logger from the registry and close its handlers."""
        logger = self._loggers.pop(name, None)
        if logger:
            logger.close()

    def clear(self) -> None:
        """Close and remove all registered loggers."""
        for name in list(self._loggers.keys()):
            self.unregister(name)
        self._audit_loggers.clear()

    @property
    def registered_names(self) -> List[str]:
        return list(self._loggers.keys())

    # -- convenience creation methods ------------------------------------

    def create_console_logger(
        self,
        name: str,
        level: LogLevel = LogLevel.INFO,
        use_colors: bool = True,
        filters: Optional[List[logging.Filter]] = None,
    ) -> Logger:
        """
        Create a logger that outputs only to the console (stdout/stderr).

        Ideal for development and debugging. Uses ConsoleFormatter
        with ANSI colors when use_colors=True.
        """
        if name in self._loggers:
            return self._loggers[name]

        handler = ConsoleHandler(
            formatter=ConsoleFormatter(use_colors=use_colors),
            filters=filters,
            use_colors=use_colors,
        )

        config = LoggerConfig(
            name=name,
            level=level,
            handlers=[handler],
        )
        logger = Logger(config)
        self._loggers[name] = logger
        return logger

    def create_file_logger(
        self,
        name: str,
        file_path: str,
        level: LogLevel = LogLevel.INFO,
        filters: Optional[List[logging.Filter]] = None,
    ) -> Logger:
        """
        Create a logger that writes to a single file (no rotation).

        Suitable for short-lived sessions, crash dumps, or logs that
        are managed externally by logrotate.
        """
        if name in self._loggers:
            return self._loggers[name]

        handler = FileLogHandler(
            file_path=file_path,
            formatter=DetailedFileFormatter(),
            filters=filters,
        )

        config = LoggerConfig(
            name=name,
            level=level,
            handlers=[handler],
        )
        logger = Logger(config)
        self._loggers[name] = logger
        return logger

    def create_rotating_logger(
        self,
        name: str,
        file_path: str,
        level: LogLevel = LogLevel.INFO,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        rotation_type: RotationType = RotationType.SIZE,
        when: str = "midnight",
        interval: int = 1,
        compress: bool = True,
        filters: Optional[List[logging.Filter]] = None,
    ) -> Logger:
        """
        Create a logger with rotating file output.

        Supports two rotation strategies:

        Size-based (default):
            Rotates when the file reaches max_bytes.
            Keeps backup_count rotated archives.

        Time-based:
            Rotates at fixed intervals (when='midnight', 'H', 'D', etc.).
            Keeps backup_count rotated archives.

        Rotated files are gzip-compressed when compress=True.

        This is the recommended logger for all long-running production
        components (voice pipeline, AI brain, security, etc.).
        """
        if name in self._loggers:
            return self._loggers[name]

        config = RotationConfig(
            rotation_type=rotation_type,
            max_bytes=max_bytes,
            backup_count=backup_count,
            when=when,
            interval=interval,
            compress=compress,
        )

        handler = RotatingFileLogHandler(
            file_path=file_path,
            config=config,
            formatter=DetailedFileFormatter(),
            filters=filters,
        )

        logger_config = LoggerConfig(
            name=name,
            level=level,
            handlers=[handler],
        )
        logger = Logger(logger_config)
        self._loggers[name] = logger
        return logger

    def create_composite_logger(
        self,
        name: str,
        file_path: str,
        level: LogLevel = LogLevel.INFO,
        use_colors: bool = True,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        compress: bool = True,
        filters: Optional[List[logging.Filter]] = None,
    ) -> Logger:
        """
        Create a logger that writes to BOTH console and a rotating file.

        Console output uses ConsoleFormatter (colored, human-readable).
        File output uses DetailedFileFormatter (full context, persistent).

        This is the recommended default for most feature-group components
        because it provides immediate terminal feedback during development
        while maintaining a persistent audit trail on disk.
        """
        if name in self._loggers:
            return self._loggers[name]

        console_handler = ConsoleHandler(
            formatter=ConsoleFormatter(use_colors=use_colors),
            use_colors=use_colors,
        )

        rotation_config = RotationConfig(
            rotation_type=RotationType.SIZE,
            max_bytes=max_bytes,
            backup_count=backup_count,
            compress=compress,
        )

        file_handler = RotatingFileLogHandler(
            file_path=file_path,
            config=rotation_config,
            formatter=DetailedFileFormatter(),
        )

        composite = CompositeHandler(
            handlers=[console_handler, file_handler],
            filters=filters,
        )

        config = LoggerConfig(
            name=name,
            level=level,
            handlers=[composite],
        )
        logger = Logger(config)
        self._loggers[name] = logger
        return logger

    def create_json_logger(
        self,
        name: str,
        file_path: str,
        level: LogLevel = LogLevel.INFO,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        compress: bool = True,
        filters: Optional[List[logging.Filter]] = None,
    ) -> Logger:
        """
        Create a logger that emits structured JSON lines to a rotating file.

        Each log record is a single-line JSON object with deterministic
        key ordering. This format is ideal for:

            - Log aggregation systems (ELK, Loki, Datadog)
            - Automated log analysis and alerting
            - Machine-readable audit trails

        Use this for subsystems that produce high-volume, structured
        telemetry (performance metrics, search results, tool executions).
        """
        if name in self._loggers:
            return self._loggers[name]

        rotation_config = RotationConfig(
            rotation_type=RotationType.SIZE,
            max_bytes=max_bytes,
            backup_count=backup_count,
            compress=compress,
        )

        handler = RotatingFileLogHandler(
            file_path=file_path,
            config=rotation_config,
            formatter=JSONFormatter(),
            filters=filters,
        )

        config = LoggerConfig(
            name=name,
            level=level,
            handlers=[handler],
        )
        logger = Logger(config)
        self._loggers[name] = logger
        return logger

    def create_audit_logger(
        self,
        name: str,
        file_path: str,
        hmac_key: bytes,
        level: LogLevel = LogLevel.INFO,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 10,
        compress: bool = True,
    ) -> AuditLogger:
        """
        Create a tamper-evident audit logger with HMAC-SHA256 chaining.

        Audit loggers produce cryptographically signed log entries where
        each entry's signature incorporates the previous entry's signature,
        forming a hash chain. Any deletion, insertion, or modification of
        entries is detectable during verification.

        Parameters:
            name        : logger name (e.g., "security.audit")
            file_path   : path to the audit log file
            hmac_key    : secret key for HMAC signing (keep secure!)
            level       : minimum log level (usually INFO for audit)
            max_bytes   : rotation threshold
            backup_count: number of rotated archives to retain
            compress    : gzip rotated files

        The hmac_key should be generated once and stored securely
        (e.g., via Windows DPAPI or the encryption module's key_manager).
        """
        if name in self._audit_loggers:
            return self._audit_loggers[name]

        audit = AuditLogger(
            name=name,
            file_path=file_path,
            hmac_key=hmac_key,
            level=level,
            max_bytes=max_bytes,
            backup_count=backup_count,
            compress=compress,
        )
        self._audit_loggers[name] = audit
        return audit

    def create_logger(
        self,
        name: str,
        level: LogLevel = LogLevel.INFO,
        handlers: Optional[List[logging.Handler]] = None,
        filters: Optional[List[logging.Filter]] = None,
        propagate: bool = False,
    ) -> Logger:
        """
        Create a fully custom logger from individual components.

        This is the escape hatch for configurations that don't fit
        the convenience methods. Pass in any combination of handlers
        and filters for maximum flexibility.

        Example:
            handler = CompositeHandler([
                ConsoleHandler(formatter=ConsoleFormatter()),
                RotatingFileLogHandler(
                    file_path="logs/custom.log",
                    config=RotationConfig(max_bytes=5_000_000),
                    formatter=JSONFormatter(),
                ),
            ])
            logger = factory.create_logger(
                name="custom",
                level=LogLevel.DEBUG,
                handlers=[handler],
                filters=[LevelFilter(logging.WARNING)],
            )
        """
        if name in self._loggers:
            return self._loggers[name]

        config = LoggerConfig(
            name=name,
            level=level,
            handlers=handlers or [],
            filters=filters or [],
            propagate=propagate,
        )
        logger = Logger(config)
        self._loggers[name] = logger
        return logger

    # -- shutdown ---------------------------------------------------------

    def shutdown(self) -> None:
        """
        Flush and close all registered loggers and audit loggers.

        Call this during application shutdown (after all feature groups
        have stopped) to ensure no log records are lost.
        """
        for name in list(self._loggers.keys()):
            self.unregister(name)

        for name, audit in list(self._audit_loggers.items()):
            audit.close()
        self._audit_loggers.clear()
