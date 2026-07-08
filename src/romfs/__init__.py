# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng
"""romfs: pure-Python reader and writer for Linux romfs (-rom1fs-) images."""

from .errors import (
    BadMagicError,
    ChecksumMismatchError,
    RomFSError,
    TruncatedImageError,
    UnsupportedTypeError,
)
from .format import EntryType
from .nodes import RomFSNode
from .reader import RomFSReader
from .writer import RomFSWriter

__version__ = "0.0.1"

__all__ = [
    "RomFSReader",
    "RomFSWriter",
    "RomFSNode",
    "EntryType",
    "RomFSError",
    "BadMagicError",
    "ChecksumMismatchError",
    "TruncatedImageError",
    "UnsupportedTypeError",
    "__version__",
]
