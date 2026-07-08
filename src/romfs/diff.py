# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng
"""Compare two romfs images at the file-tree level."""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from .format import EntryType
from .nodes import RomFSNode
from .reader import RomFSReader

ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"
TYPE_CHANGED = "type-changed"
VOLUME = "volume"


@dataclass
class DiffEntry:
    """One row of a two-image diff.

    For ``added`` only the ``*_b`` fields are meaningful; for ``removed`` only
    ``*_a``; for ``type-changed`` and ``changed`` both sides are. The
    ``same_size_content_diff`` flag is the L2 catch: a regular file whose byte
    size is identical on both sides but whose content differs. A ``volume``
    entry (``path`` empty) carries the volume-name strings in ``target_a`` /
    ``target_b``.
    """

    status: str
    path: str
    type_a: EntryType | None
    type_b: EntryType | None
    size_a: int
    size_b: int
    exec_a: bool
    exec_b: bool
    target_a: str | None
    target_b: str | None
    same_size_content_diff: bool = False


def diff_images(a: RomFSReader, b: RomFSReader) -> list[DiffEntry]:
    """Compare two open readers; return one entry per added/removed/changed path.

    Paths present and byte-identical on both sides are omitted. Regular files
    of equal size are compared byte-for-byte so that same-size content edits are
    caught, not just size changes. A differing volume name is reported first as
    a ``volume`` entry (``path`` empty).
    """
    entries: list[DiffEntry] = []
    if a.volume_name != b.volume_name:
        entries.append(
            DiffEntry(
                status=VOLUME,
                path="",
                type_a=None,
                type_b=None,
                size_a=0,
                size_b=0,
                exec_a=False,
                exec_b=False,
                target_a=a.volume_name,
                target_b=b.volume_name,
            )
        )
    nodes_a = {p: n for p, n in a.walk()}
    nodes_b = {p: n for p, n in b.walk()}
    for path in sorted(set(nodes_a) | set(nodes_b)):
        na = nodes_a.get(path)
        nb = nodes_b.get(path)
        if na is None:
            assert nb is not None  # path is in the union, so B must have it
            entries.append(_single(ADDED, path, nb, b))
        elif nb is None:
            entries.append(_single(REMOVED, path, na, a))
        elif na.type is not nb.type:
            entries.append(_pair(TYPE_CHANGED, path, na, nb))
        else:
            entry = _pair(CHANGED, path, na, nb)
            if _fill_delta(entry, a, b, na, nb):
                entries.append(entry)
    return entries


def content_differs(entry: DiffEntry) -> bool:
    """True for a CHANGED regular file whose bytes differ (by size or content)."""
    return (
        entry.status == CHANGED
        and entry.type_a is EntryType.REGULAR
        and (entry.size_a != entry.size_b or entry.same_size_content_diff)
    )


def unified_text_diff(a: RomFSReader, b: RomFSReader, entry: DiffEntry) -> list[str]:
    """Return unified-diff lines for a changed text file; empty list if binary."""
    da = a.read_path(entry.path)
    db = b.read_path(entry.path)
    if _is_binary(da) or _is_binary(db):
        return []
    lines_a = da.decode("utf-8", "replace").splitlines()
    lines_b = db.decode("utf-8", "replace").splitlines()
    return list(
        difflib.unified_diff(
            lines_a,
            lines_b,
            fromfile=f"a: {entry.path}",
            tofile=f"b: {entry.path}",
            lineterm="",
        )
    )


def _is_binary(data: bytes) -> bool:
    # NUL bytes are the standard "this is not text" signal (git uses the same).
    return b"\x00" in data


def _single(status: str, path: str, n: RomFSNode, reader: RomFSReader) -> DiffEntry:
    is_a = status == REMOVED
    # For a symlink, surface the target string (not just its length) so an
    # added/removed link reads as clearly as a changed one.
    target = (
        reader.read(n).decode("utf-8", "replace")
        if n.type is EntryType.SYMLINK
        else None
    )
    return DiffEntry(
        status=status,
        path=path,
        type_a=n.type if is_a else None,
        type_b=None if is_a else n.type,
        size_a=n.size if is_a else 0,
        size_b=0 if is_a else n.size,
        exec_a=n.executable if is_a else False,
        exec_b=False if is_a else n.executable,
        target_a=target if is_a else None,
        target_b=None if is_a else target,
    )


def _pair(status: str, path: str, na: RomFSNode, nb: RomFSNode) -> DiffEntry:
    return DiffEntry(
        status=status,
        path=path,
        type_a=na.type,
        type_b=nb.type,
        size_a=na.size,
        size_b=nb.size,
        exec_a=na.executable,
        exec_b=nb.executable,
        target_a=None,
        target_b=None,
    )


def _fill_delta(
    entry: DiffEntry,
    a: RomFSReader,
    b: RomFSReader,
    na: RomFSNode,
    nb: RomFSNode,
) -> bool:
    """Populate content/target deltas on ``entry``; return True if anything changed."""
    changed = False
    if entry.size_a != entry.size_b:
        changed = True
    if entry.exec_a != entry.exec_b:
        changed = True
    if na.type is EntryType.REGULAR:
        if entry.size_a == entry.size_b:
            if a.read(na) != b.read(nb):
                entry.same_size_content_diff = True
                changed = True
        else:
            # Size differs -> content necessarily differs; skip the byte read.
            changed = True
    elif na.type is EntryType.SYMLINK:
        ta = a.read(na).decode("utf-8", "replace")
        tb = b.read(nb).decode("utf-8", "replace")
        entry.target_a = ta
        entry.target_b = tb
        if ta != tb:
            changed = True
    return changed


__all__ = [
    "DiffEntry",
    "diff_images",
    "content_differs",
    "unified_text_diff",
    "ADDED",
    "REMOVED",
    "CHANGED",
    "TYPE_CHANGED",
    "VOLUME",
]
