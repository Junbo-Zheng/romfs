# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng
"""romfs on-disk format primitives.

Byte-exact per the Linux kernel sources:
  - fs/romfs/super.c            (superblock parse, root offset, checksum)
  - include/uapi/linux/romfs_fs.h  (struct + constant definitions)
  - Documentation/filesystems/romfs.rst

All multi-byte integers are big-endian. Every header and payload begins on a
16-byte boundary. The superblock checksum covers the first
``min(full_size, 512)`` bytes; the file-header checksum covers the whole header
(fixed 16 bytes + padded name). Both are stored as the two's complement of the
sum of the remaining words so that summing every word in the covered region
(including the checksum word) yields zero.
"""

from __future__ import annotations

import struct
from enum import IntEnum

# --- Superblock ------------------------------------------------------------

# "-rom1fs-" as two big-endian words: '-','r','o','m' and '1','f','s','-'.
ROMFS_MAGIC_WORD0 = b"-rom"
ROMFS_MAGIC_WORD1 = b"1fs-"
ROMFS_MAGIC = ROMFS_MAGIC_WORD0 + ROMFS_MAGIC_WORD1  # b"-rom1fs-"

# The superblock checksum window: the first min(full_size, 512) bytes.
ROMFS_SUPERBLOCK_CHECK_WINDOW = 512

# Whole-image padding to a 1024-byte boundary (block-device mount requirement,
# per Documentation/filesystems/romfs.rst).
ROMFS_IMAGE_ALIGN = 1024

# --- File header -----------------------------------------------------------

ROMFH_SIZE = 16  # fixed part of a file header (next, spec, size, checksum)
ROMFH_PAD = 15  # low 4 bits carry type + exec, so offsets are 16-aligned
ROMFH_MASK = ~ROMFH_PAD & 0xFFFFFFFF  # next/spec offset mask (clear low 4 bits)
ROMFH_TYPE = 7  # low 3 bits: entry type
ROMFH_EXEC = 8  # bit 3: executable

# 16-byte alignment for every header and payload.
ALIGN = 16


class EntryType(IntEnum):
    """romfs entry type codes (low 3 bits of the ``next`` field)."""

    HARDLINK = 0
    DIRECTORY = 1
    REGULAR = 2
    SYMLINK = 3
    BLOCKDEV = 4
    CHARDEV = 5
    SOCKET = 6
    FIFO = 7


# --- Structs ---------------------------------------------------------------

# Superblock fixed part: magic0, magic1, full_size, checksum (name[] follows).
_superblock_head = struct.Struct(">4s4sII")
# File header fixed part: next, spec, size, checksum (name[] follows).
_file_head = struct.Struct(">IIII")


def align_up(n: int, alignment: int = ALIGN) -> int:
    """Round ``n`` up to the next multiple of ``alignment``."""
    return (n + alignment - 1) & ~(alignment - 1)


def align16(n: int) -> int:
    """Round ``n`` up to the next 16-byte boundary."""
    return align_up(n, ALIGN)


def name_field_size(name_len: int) -> int:
    """Padded size of a null-terminated name field of ``name_len`` bytes.

    The name is stored null-terminated and the whole field (name + NUL +
    padding) is rounded up to 16 bytes. Minimum field size is 16.
    """
    return align16(name_len + 1)


def header_meta_size(name_len: int) -> int:
    """Total metadata size of a file header: fixed 16 bytes + padded name."""
    return ROMFH_SIZE + name_field_size(name_len)


def _sum_words(buf: bytes) -> int:
    """Sum every big-endian uint32 word in ``buf`` (length must be a multiple of 4)."""
    if len(buf) % 4 != 0:
        raise ValueError(f"buffer length {len(buf)} is not a multiple of 4")
    total = 0
    for i in range(0, len(buf), 4):
        total += struct.unpack_from(">I", buf, i)[0]
    return total & 0xFFFFFFFF


def superblock_checksum(first_block: bytes) -> int:
    """Compute the superblock checksum word for ``first_block``.

    ``first_block`` is the first ``min(full_size, 512)`` bytes of the image
    with the checksum field (offset 12) set to 0. The returned value, when
    stored at offset 12, makes the sum of every word in ``first_block`` equal
    to zero.
    """
    return (-_sum_words(first_block)) & 0xFFFFFFFF


def header_checksum(header: bytes) -> int:
    """Compute the file-header checksum word for ``header``.

    ``header`` is the full header (16 fixed bytes + padded name) with the
    checksum field (offset 12) set to 0. Same convention as the superblock:
    the stored value makes the whole header sum to zero.
    """
    return (-_sum_words(header)) & 0xFFFFFFFF


def verify_superblock_checksum(first_block: bytes, stored: int) -> bool:
    """Return True if the superblock checksum region sums to zero."""
    return (_sum_words(first_block[:ROMFS_SUPERBLOCK_CHECK_WINDOW]) & 0xFFFFFFFF) == 0


# --- Pack / unpack helpers -------------------------------------------------


def pack_superblock_head(full_size: int, checksum: int) -> bytes:
    """Pack the 16-byte superblock fixed head (magic + size + checksum)."""
    return _superblock_head.pack(
        ROMFS_MAGIC_WORD0, ROMFS_MAGIC_WORD1, full_size, checksum
    )


def unpack_superblock_head(buf: bytes) -> tuple[bytes, bytes, int, int]:
    """Unpack the 16-byte superblock fixed head.

    Returns ``(magic0, magic1, full_size, checksum)``.
    """
    return _superblock_head.unpack(buf[:ROMFH_SIZE])


def pack_file_head(next_off: int, spec: int, size: int, checksum: int) -> bytes:
    """Pack the 16-byte file header fixed part."""
    return _file_head.pack(
        next_off & 0xFFFFFFFF,
        spec & 0xFFFFFFFF,
        size & 0xFFFFFFFF,
        checksum & 0xFFFFFFFF,
    )


def unpack_file_head(buf: bytes) -> tuple[int, int, int, int]:
    """Unpack the 16-byte file header fixed part -> (next, spec, size, checksum)."""
    return _file_head.unpack(buf[:ROMFH_SIZE])


def encode_name(name: str) -> bytes:
    """Encode a name to its null-terminated, 16-byte-padded field bytes."""
    raw = name.encode("utf-8")
    field = name_field_size(len(raw))
    return raw + b"\x00" * (field - len(raw))


def decode_name(field: bytes) -> str:
    """Decode a padded name field up to its NUL terminator."""
    nul = field.find(b"\x00")
    if nul >= 0:
        field = field[:nul]
    return field.decode("utf-8")
