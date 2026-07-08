# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng
"""Parse a romfs image into a node tree with lazy content reads."""

from __future__ import annotations

import mmap
from collections.abc import Iterator
from pathlib import Path

from .errors import (
    BadMagicError,
    ChecksumMismatchError,
    TruncatedImageError,
    UnsupportedTypeError,
)
from .format import (
    ROMFH_MASK,
    ROMFS_MAGIC,
    ROMFS_SUPERBLOCK_CHECK_WINDOW,
    EntryType,
    _sum_words,
    decode_name,
    header_meta_size,
    unpack_file_head,
    unpack_superblock_head,
)
from .nodes import RomFSNode

_MAX_NAME = 128  # ROMFS_MAXFN


class RomFSReader:
    """Read a romfs image from a file, building a node tree.

    File contents are not loaded eagerly: the image is memory-mapped and
    :meth:`read` slices the payload on demand.
    """

    def __init__(self, path: str | Path, *, verify: bool = True) -> None:
        self._path = Path(path)
        self._fd = open(self._path, "rb")
        self._mm = mmap.mmap(self._fd.fileno(), 0, access=mmap.ACCESS_READ)

        magic0, magic1, full_size, checksum = unpack_superblock_head(self._mm[:16])
        if magic0 + magic1 != ROMFS_MAGIC:
            raise BadMagicError(f"not a romfs image: bad magic {magic0 + magic1!r}")

        self.full_size = full_size
        self._checksum = checksum

        if verify:
            self._verify_checksum()

        self.volume_name = self._read_volume_name()
        self.root = self._build_root()

    # --- public API --------------------------------------------------------

    def walk(self) -> Iterator[tuple[str, RomFSNode]]:
        """Yield ``(path, node)`` for every node under root (root itself excluded)."""
        for child in self.root.children:
            yield from child.walk()

    def read(self, node: RomFSNode) -> bytes:
        """Return the payload bytes of a regular file or symlink target."""
        if node.type is EntryType.DIRECTORY:
            raise TypeError(f"{node.name!r}: directories have no payload")
        if node.data_offset is None:
            raise TypeError(f"{node.name!r}: node has no data offset")
        return bytes(self._mm[node.data_offset : node.data_offset + node.size])

    def extract(self, outdir: str | Path) -> None:
        """Write the whole tree to ``outdir`` (created if missing)."""
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        for path, node in self.walk():
            rel = Path(path)
            if node.type is EntryType.DIRECTORY:
                (out / rel).mkdir(parents=True, exist_ok=True)
            elif node.type is EntryType.SYMLINK:
                target = self.read(node).decode("utf-8")
                (out / rel).parent.mkdir(parents=True, exist_ok=True)
                (out / rel).symlink_to(target)
            elif node.type is EntryType.REGULAR:
                (out / rel).parent.mkdir(parents=True, exist_ok=True)
                (out / rel).write_bytes(self.read(node))

    def close(self) -> None:
        self._mm.close()
        self._fd.close()

    def __enter__(self) -> RomFSReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- internals ---------------------------------------------------------

    def _read_volume_name(self) -> str:
        # Volume name starts at offset 16, NUL-terminated (capped at ROMFS_MAXFN).
        end = 16
        limit = min(16 + _MAX_NAME, len(self._mm))
        while end < limit and self._mm[end] != 0:
            end += 1
        return bytes(self._mm[16:end]).decode("utf-8", errors="replace")

    def _verify_checksum(self) -> None:
        # The kernel sums EVERY word in the first min(full_size, 512) bytes
        # (including the checksum field) and requires the total to be zero:
        # stored_checksum == -(sum of all other words). So we sum the raw
        # block as-is and check for zero — no zeroing of the checksum field.
        window = min(self.full_size, ROMFS_SUPERBLOCK_CHECK_WINDOW)
        if window > len(self._mm):
            raise TruncatedImageError(
                f"image is {len(self._mm)} bytes but checksum window needs {window}"
            )
        if _sum_words(bytes(self._mm[:window])) & 0xFFFFFFFF != 0:
            raise ChecksumMismatchError("superblock checksum does not sum to zero")

    def _build_root(self) -> RomFSNode:
        # Root header offset = align16(16 + len(volume_name) + 1), matching the
        # kernel's `(ROMFH_SIZE + len + 1 + ROMFH_PAD) & ROMFH_MASK`.
        root_off = header_meta_size(len(self.volume_name.encode("utf-8")))
        root = self._parse_node(root_off)
        if root.type is not EntryType.DIRECTORY:
            raise BadMagicError(f"root entry at offset {root_off} is not a directory")
        # The on-disk root entry is named "." (genromfs convention); expose it
        # as a nameless root so child paths are clean ("watchface", not "./watchface").
        root.name = ""
        return root

    def _parse_node(self, offset: int) -> RomFSNode:
        if offset + 16 > len(self._mm):
            raise TruncatedImageError(
                f"file header at offset {offset} is out of bounds"
            )

        next_off, spec, size, _checksum = unpack_file_head(
            self._mm[offset : offset + 16]
        )
        type_code = next_off & 0x07
        try:
            etype = EntryType(type_code)
        except ValueError as e:
            raise UnsupportedTypeError(
                f"unknown entry type {type_code} at offset {offset}"
            ) from e

        name_len = self._strnlen(offset + 16)
        name = decode_name(self._mm[offset + 16 : offset + 16 + name_len])
        meta = header_meta_size(name_len)

        node = RomFSNode(
            name=name,
            type=etype,
            size=size,
            spec_info=spec & ROMFH_MASK,
            header_offset=offset,
            _reader=self,
        )
        if etype is EntryType.HARDLINK:
            # A hardlink's spec points at its destination header. genromfs uses
            # hardlinks both for ".." (skipped in _parse_children) and for
            # deduplicating identical files. Resolve file/symlink targets so
            # read()/extract() see the linked content; directory targets are
            # left unresolved to avoid cycles in the tree.
            self._resolve_hardlink(node, spec & ROMFH_MASK)
        elif etype in (EntryType.REGULAR, EntryType.SYMLINK):
            node.data_offset = offset + meta
        elif etype is EntryType.DIRECTORY:
            self._parse_children(node, spec & ROMFH_MASK)

        return node

    def _resolve_hardlink(self, node: RomFSNode, target_off: int) -> None:
        """Mirror a hardlink's file/symlink target so its content is readable."""
        if not target_off or target_off == node.header_offset:
            return
        if target_off + 16 > len(self._mm):
            return
        t_next, _t_spec, t_size, _ = unpack_file_head(
            self._mm[target_off : target_off + 16]
        )
        try:
            t_type = EntryType(t_next & 0x07)
        except ValueError:
            return
        if t_type not in (EntryType.REGULAR, EntryType.SYMLINK):
            return
        t_name_len = self._strnlen(target_off + 16)
        t_meta = header_meta_size(t_name_len)
        node.type = t_type
        node.size = t_size
        node.data_offset = target_off + t_meta

    def _parse_children(self, parent: RomFSNode, first_off: int) -> None:
        offset = first_off
        while offset and offset + 16 <= len(self._mm):
            # Peek the name first to skip the "." / ".." self/parent entries
            # that genromfs emits at the head of every directory. "." is a DIR
            # whose spec points back at the directory itself, so recursing into
            # it would loop forever; ".." is a hardlink to the parent.
            name_len = self._strnlen(offset + 16)
            name = decode_name(self._mm[offset + 16 : offset + 16 + name_len])
            if name not in (".", ".."):
                child = self._parse_node(offset)
                child.parent = parent
                parent.children.append(child)
            # Advance to the next sibling via this entry's `next` field. The
            # offset lives in the high 28 bits; the low 4 bits carry type+exec.
            next_raw = unpack_file_head(self._mm[offset : offset + 16])[0]
            offset = next_raw & ROMFH_MASK

    def _strnlen(self, offset: int) -> int:
        """Length of the NUL-terminated name at ``offset`` (capped at ROMFS_MAXFN)."""
        end = offset
        limit = min(offset + _MAX_NAME, len(self._mm))
        while end < limit and self._mm[end] != 0:
            end += 1
        return end - offset


__all__ = ["RomFSReader"]
