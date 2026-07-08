# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng
"""In-memory node tree shared by reader and writer."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .format import EntryType

if TYPE_CHECKING:
    from .reader import RomFSReader


@dataclass
class RomFSNode:
    """A single romfs entry (regular file, directory, or symlink).

    Reader and writer share this representation:
      - ``data_offset``: absolute byte offset of the payload in the image
        (regular/symlink). ``None`` until laid out / parsed.
      - ``header_offset``: absolute byte offset of this entry's header.
      - ``children`` / ``parent``: tree links (directories carry children).
      - ``src_path``: writer-side source path to copy payload from.
      - ``_reader``: reader-side back-reference for lazy content reads.
    """

    name: str
    type: EntryType
    size: int = 0
    spec_info: int = 0
    header_offset: int | None = None
    data_offset: int | None = None
    children: list[RomFSNode] = field(default_factory=list)
    parent: RomFSNode | None = None
    src_path: str | None = None
    _reader: RomFSReader | None = field(default=None, repr=False)
    # symlink target string (writer-side convenience; reader fills it on demand)
    target: str | None = None
    # executable bit (romfs stores a single exec flag in the ``next`` field's
    # bit 3; the only permission bit the format preserves). False for dirs and
    # symlinks, which have no meaningful exec bit.
    executable: bool = False

    @property
    def path(self) -> str:
        """Slash-joined path from the (nameless) root down to this node.

        Built by walking the ``parent`` chain, so it stays correct after
        arbitrary tree edits without re-running ``walk()``.
        """
        parts: list[str] = []
        node: RomFSNode | None = self
        while node is not None and node.parent is not None:
            parts.append(node.name)
            node = node.parent
        return "/".join(reversed(parts))

    @property
    def is_dir(self) -> bool:
        return self.type is EntryType.DIRECTORY

    @property
    def is_regular(self) -> bool:
        return self.type is EntryType.REGULAR

    @property
    def is_symlink(self) -> bool:
        return self.type is EntryType.SYMLINK

    def walk(self, prefix: str = "") -> Iterator[tuple[str, RomFSNode]]:
        """Yield ``(path, node)`` for this node and all descendants (pre-order)."""
        path = f"{prefix}/{self.name}" if prefix else self.name
        yield path, self
        for child in self.children:
            yield from child.walk(path)


__all__ = ["RomFSNode"]
