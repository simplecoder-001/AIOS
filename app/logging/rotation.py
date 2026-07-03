"""
Log rotation configuration and handler creation.

Supports two rotation strategies:
    1. SIZE  — rotate when file exceeds max_bytes, keep N backups
    2. TIME  — rotate at fixed intervals (midnight, hourly, daily, weekly)

Both strategies compress rotated files and are thread-safe.
"""

import gzip
import logging
import os
import shutil
from dataclasses import dataclass, field
from enum import Enum
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Any, List, Optional


class RotationType(Enum):
    """Strategy for when to rotate a log file."""
    SIZE = "size"
    TIME = "time"


@dataclass
class RotationConfig:
    """
    Declarative configuration for log file rotation.

    Size-based fields (used when rotation_type == SIZE):
        max_bytes    : rotate when file reaches this size (default 10 MB)
        backup_count : number of rotated archives to keep

    Time-based fields (used when rotation_type == TIME):
        when         : 'S' seconds, 'M' minutes, 'H' hours,
                       'D' days, 'W0'-'W6' weekly, 'midnight'
        interval     : number of units (default 1)
        backup_count : number of rotated archives to keep

    Common:
        compress     : gzip rotated files (default True)
        encoding     : file encoding (default utf-8)
    """
    rotation_type: RotationType = RotationType.SIZE

    # Size-based
    max_bytes: int = 10 * 1024 * 1024  # 10 MB
    backup_count: int = 5

    # Time-based
    when: str = "midnight"
    interval: int = 1

    # Common
    compress: bool = True
    encoding: str = "utf-8"

    def __post_init__(self) -> None:
        if self.backup_count < 0:
            raise ValueError("backup_count must be >= 0")
        if self.rotation_type == RotationType.SIZE and self.max_bytes <= 0:
            raise ValueError("max_bytes must be > 0 for size-based rotation")
        if self.rotation_type == RotationType.TIME and self.interval <= 0:
            raise ValueError("interval must be > 0 for time-based rotation")


class _CompressingRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that gzip-compresses rotated files."""

    compress: bool = True

    def rotate(self, source: str, dest: str) -> None:
        if self.compress and dest and not dest.endswith(".gz"):
            dest_gz = dest + ".gz"
            with open(source, "rb") as f_in:
                with gzip.open(dest_gz, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            os.remove(source)
        else:
            super().rotate(source, dest)


class _CompressingTimedRotatingFileHandler(TimedRotatingFileHandler):
    """TimedRotatingFileHandler that gzip-compresses rotated files."""

    compress: bool = True

    def rotate(self, source: str, dest: str) -> None:
        if self.compress and dest and not dest.endswith(".gz"):
            dest_gz = dest + ".gz"
            with open(source, "rb") as f_in:
                with gzip.open(dest_gz, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            os.remove(source)
        else:
            super().rotate(source, dest)


def create_rotating_handler(
    file_path: str,
    config: RotationConfig,
    formatter: Optional[logging.Formatter] = None,
) -> logging.Handler:
    """
    Create a rotating file handler from a RotationConfig.

    Args:
        file_path  : path to the active log file
        config     : rotation configuration
        formatter  : optional formatter to attach

    Returns:
        A configured logging.Handler ready to be added to a logger.
    """
    # Ensure parent directory exists
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)

    if config.rotation_type == RotationType.SIZE:
        handler = _CompressingRotatingFileHandler(
            filename=file_path,
            maxBytes=config.max_bytes,
            backupCount=config.backup_count,
            encoding=config.encoding,
            delay=True,
        )
    else:
        handler = _CompressingTimedRotatingFileHandler(
            filename=file_path,
            when=config.when,
            interval=config.interval,
            backupCount=config.backup_count,
            encoding=config.encoding,
            delay=True,
            utc=True,
        )

    handler.compress = config.compress

    if formatter is not None:
        handler.setFormatter(formatter)

    return handler
