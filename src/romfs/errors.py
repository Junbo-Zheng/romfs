# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng
"""romfs exception hierarchy."""

from __future__ import annotations


class RomFSError(Exception):
    """Base class for all romfs errors."""


class BadMagicError(RomFSError):
    """The image does not start with the ``-rom1fs-`` magic."""


class TruncatedImageError(RomFSError):
    """The image is shorter than the bytes a header claims to occupy."""


class UnsupportedTypeError(RomFSError):
    """The image contains an entry type this library does not handle."""


class ChecksumMismatchError(RomFSError):
    """The superblock checksum region does not sum to zero."""


__all__ = [
    "RomFSError",
    "BadMagicError",
    "TruncatedImageError",
    "UnsupportedTypeError",
    "ChecksumMismatchError",
]
