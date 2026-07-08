# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng
"""Build a romfs image from a node tree or a local directory tree."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from .format import (
    ALIGN,
    ROMFS_IMAGE_ALIGN,
    ROMFS_SUPERBLOCK_CHECK_WINDOW,
    EntryType,
    align_up,
    encode_name,
    header_checksum,
    header_meta_size,
    pack_file_head,
    pack_superblock_head,
    superblock_checksum,
)
from .nodes import RomFSNode


class RomFSWriter:
    """Assemble a romfs image from a root :class:`RomFSNode` tree.

    Build a tree with :meth:`from_directory` (or by hand), then call
    :meth:`write` to emit the image.
    """

    def __init__(self, root: RomFSNode, volume_name: str = "") -> None:
        if root.type is not EntryType.DIRECTORY:
            raise ValueError("root node must be a directory")
        self.root = root
        self.volume_name = volume_name

    # --- construction ------------------------------------------------------

    @classmethod
    def from_directory(cls, root_dir: str | Path, volume_name: str = "") -> RomFSWriter:
        """Build a writer from a local directory tree.

        - directories  -> DIRECTORY nodes (recursed)
        - symlinks     -> SYMLINK nodes (target stored as payload)
        - regular files-> REGULAR nodes (payload copied from disk)
        Other entry types (devices, fifos, sockets) are skipped with a warning.
        """
        root_dir = Path(root_dir)
        if not root_dir.is_dir():
            raise NotADirectoryError(f"not a directory: {root_dir}")
        root = RomFSNode(name="", type=EntryType.DIRECTORY)
        cls._scan(root_dir, root)
        return cls(root, volume_name=volume_name)

    @classmethod
    def _scan(cls, dir_path: Path, node: RomFSNode) -> None:
        for entry in sorted(dir_path.iterdir(), key=lambda p: p.name):
            child = cls._make_node(entry)
            if child is not None:
                child.parent = node
                node.children.append(child)

    @classmethod
    def _make_node(cls, path: Path) -> RomFSNode | None:
        if path.is_symlink():
            target = os.readlink(path)
            tgt_bytes = target.encode("utf-8")
            return RomFSNode(
                name=path.name,
                type=EntryType.SYMLINK,
                size=len(tgt_bytes),
                target=target,
            )
        if path.is_dir():
            node = RomFSNode(name=path.name, type=EntryType.DIRECTORY)
            cls._scan(path, node)
            return node
        if path.is_file():
            return RomFSNode(
                name=path.name,
                type=EntryType.REGULAR,
                size=path.stat().st_size,
                src_path=str(path),
            )
        # Skip devices / fifos / sockets — out of scope for this writer.
        return None

    # --- emission ----------------------------------------------------------

    def write(self, out: str | Path | BinaryIO) -> None:
        """Write the romfs image to a path or open binary stream."""
        image = self._build_image()
        if isinstance(out, (str, os.PathLike)):
            Path(out).write_bytes(image)
        else:
            out.write(image)

    def _build_image(self) -> bytes:
        # 1. Assign header offsets via pre-order DFS.
        root_off = header_meta_size(len(self.volume_name.encode("utf-8")))
        content_end = self._layout(self.root, root_off)
        total = align_up(content_end, ROMFS_IMAGE_ALIGN)

        # 2. Render into a zeroed buffer of the final size.
        buf = bytearray(total)
        self._render(self.root, buf)

        # 3. Fill the superblock (magic + size + checksum) last.
        self._render_superblock(buf, total)

        return bytes(buf)

    # --- layout ------------------------------------------------------------

    def _layout(self, node: RomFSNode, offset: int) -> int:
        """Assign ``header_offset``/``data_offset``; return the next free offset."""
        node.header_offset = offset
        meta = header_meta_size(len(node.name.encode("utf-8")))

        if node.type is EntryType.DIRECTORY:
            cursor = offset + meta
            for child in node.children:
                cursor = self._layout(child, cursor)
            if node.children:
                first = node.children[0].header_offset
                assert first is not None
                node.spec_info = first
            else:
                node.spec_info = 0
            return cursor

        # Regular file or symlink: payload follows the (padded) header.
        node.data_offset = offset + meta
        return offset + meta + align_up(node.size, ALIGN)

    # --- rendering ---------------------------------------------------------

    def _render(self, node: RomFSNode, buf: bytearray) -> None:
        offset = node.header_offset
        assert offset is not None

        next_off = self._next_sibling_offset(node)
        type_bits = int(node.type) & 0x07
        next_field = (next_off | type_bits) & 0xFFFFFFFF

        # Pack the header with checksum=0, then fix up the checksum.
        name_field = encode_name(node.name)
        head = pack_file_head(next_field, node.spec_info, node.size, 0) + name_field
        checksum = header_checksum(head)
        head = (
            pack_file_head(next_field, node.spec_info, node.size, checksum) + name_field
        )
        buf[offset : offset + len(head)] = head

        if node.type is EntryType.REGULAR:
            assert (
                node.src_path is not None
            ), f"regular node {node.name!r} has no src_path"
            data = Path(node.src_path).read_bytes()
            self._write_payload(buf, node, data)
        elif node.type is EntryType.SYMLINK:
            assert node.target is not None, f"symlink node {node.name!r} has no target"
            data = node.target.encode("utf-8")
            self._write_payload(buf, node, data)
        elif node.type is EntryType.DIRECTORY:
            for child in node.children:
                self._render(child, buf)

    def _write_payload(self, buf: bytearray, node: RomFSNode, data: bytes) -> None:
        assert node.data_offset is not None
        buf[node.data_offset : node.data_offset + len(data)] = data
        # Trailing padding stays zero (buffer is pre-zeroed).

    def _next_sibling_offset(self, node: RomFSNode) -> int:
        if node.parent is None:
            # Root has no siblings.
            return 0
        siblings = node.parent.children
        idx = siblings.index(node)
        if idx + 1 < len(siblings):
            nxt = siblings[idx + 1].header_offset
            assert nxt is not None
            return nxt
        return 0

    # --- superblock --------------------------------------------------------

    def _render_superblock(self, buf: bytearray, total: int) -> None:
        # Magic + full_size + checksum(=0 placeholder) + volume name field.
        head = pack_superblock_head(total, 0)
        name_field = encode_name(self.volume_name)
        buf[0 : len(head)] = head
        buf[len(head) : len(head) + len(name_field)] = name_field

        # Checksum covers the first min(total, 512) bytes with the checksum
        # field (offset 12) zeroed.
        window = min(total, ROMFS_SUPERBLOCK_CHECK_WINDOW)
        block = bytes(buf[:window])
        checksum = superblock_checksum(block)
        final_head = pack_superblock_head(total, checksum)
        buf[0 : len(final_head)] = final_head


__all__ = ["RomFSWriter"]
