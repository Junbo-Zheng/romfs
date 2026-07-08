# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng
"""Command-line interface for the romfs package."""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .diff import (
    ADDED,
    CHANGED,
    REMOVED,
    TYPE_CHANGED,
    VOLUME,
    DiffEntry,
    content_differs,
    diff_images,
    unified_text_diff,
)
from .errors import RomFSError
from .format import EntryType, header_meta_size
from .nodes import RomFSNode
from .reader import RomFSReader
from .writer import RomFSWriter

_TYPE_LABEL = {
    EntryType.REGULAR: "f",
    EntryType.DIRECTORY: "d",
    EntryType.SYMLINK: "l",
}


def _version() -> str:
    try:
        return version("romfs")
    except PackageNotFoundError:
        from . import __version__

        return __version__


_SUBCOMMANDS = {"list", "unpack", "pack", "info", "diff"}


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # Default to "list" when an image is given with no subcommand, so
    # `romfs image.img` behaves like `romfs list image.img`.
    if argv and not argv[0].startswith("-") and argv[0] not in _SUBCOMMANDS:
        argv = ["list", *argv]

    p = argparse.ArgumentParser(
        prog="romfs",
        description=f"romfs {_version()} — read and write Linux romfs images.",
    )
    p.add_argument("-V", "--version", action="version", version=f"romfs {_version()}")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list the file tree in an image")
    p_list.add_argument("image")
    p_list.add_argument(
        "--sort",
        choices=("name", "size", "none"),
        default="name",
        help="sort order: name (default), size (largest first), none (on-disk order)",
    )
    p_list.add_argument(
        "--all",
        action="store_true",
        help=(
            "show size-0 entries too "
            "(directories and empty files are hidden by default)"
        ),
    )

    p_unpack = sub.add_parser("unpack", help="unpack files from an image")
    p_unpack.add_argument("image")
    p_unpack.add_argument(
        "path", nargs="?", help="unpack a single entry to stdout (else all)"
    )
    p_unpack.add_argument("-o", "--outdir", help="output directory for full unpacking")

    p_pack = sub.add_parser("pack", help="build an image from a directory tree")
    p_pack.add_argument("srcdir")
    p_pack.add_argument("-o", "--out", required=True, help="output image path")
    p_pack.add_argument("-n", "--name", default="", help="volume name")

    p_info = sub.add_parser("info", help="print image header info and space breakdown")
    p_info.add_argument("image")
    p_info.add_argument(
        "--verify", action="store_true", help="verify the superblock checksum"
    )

    p_diff = sub.add_parser("diff", help="compare two images file-by-file")
    p_diff.add_argument("image_a")
    p_diff.add_argument("image_b")
    p_diff.add_argument(
        "--text",
        action="store_true",
        help="also print a line-level unified diff for each changed text file",
    )

    args = p.parse_args(argv)

    try:
        if args.cmd == "list":
            return _cmd_list(args)
        if args.cmd == "unpack":
            return _cmd_unpack(args)
        if args.cmd == "pack":
            return _cmd_pack(args)
        if args.cmd == "info":
            return _cmd_info(args)
        if args.cmd == "diff":
            return _cmd_diff(args)
    except RomFSError as e:
        print(f"romfs: error: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"romfs: error: {e}", file=sys.stderr)
        return 1

    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    with RomFSReader(args.image) as r:
        entries = list(r.walk())
        if args.sort == "name":
            entries.sort(key=lambda e: e[0])
        elif args.sort == "size":
            # Largest first; fall back to path for a stable tie-break.
            entries.sort(key=lambda e: (-e[1].size, e[0]))
        # "none": keep the on-disk traversal order from walk().
        for path, node in entries:
            if not args.all and node.size == 0:
                continue
            label = _TYPE_LABEL.get(node.type, "?")
            print(f"{label} {node.size:>10}  {path}")
    return 0


def _cmd_unpack(args: argparse.Namespace) -> int:
    with RomFSReader(args.image) as r:
        if args.path:
            node = r.find(args.path)
            if node is None:
                print(f"romfs: error: path not found: {args.path}", file=sys.stderr)
                return 1
            if node.type is EntryType.DIRECTORY:
                print(
                    f"romfs: error: cannot unpack a directory: {args.path}",
                    file=sys.stderr,
                )
                return 1
            sys.stdout.buffer.write(r.read(node))
            return 0
        outdir = args.outdir or _default_outdir(args.image)
        r.unpack(outdir)
        print(f"unpacked to {outdir}")
    return 0


def _default_outdir(image: str | Path) -> str:
    """Default unpack directory named after the image.

    ``vela_app.bin`` -> ``app``, ``vela_font.bin`` -> ``font`` (the ``vela_``
    prefix is stripped); any other name keeps its stem, e.g. ``resource.bin``
    -> ``resource``.
    """
    stem = Path(image).stem
    if stem.startswith("vela_"):
        stem = stem[len("vela_") :]
    return stem or Path(image).stem


def _cmd_pack(args: argparse.Namespace) -> int:
    RomFSWriter.from_directory(args.srcdir, volume_name=args.name).write(args.out)
    print(f"packed {args.srcdir} -> {args.out}")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    with RomFSReader(args.image, verify=args.verify) as r:
        print(f"image:      {args.image}")
        print("magic:      -rom1fs-")
        print(f"full_size:  {_fmt_mb(r.full_size)} ({r.full_size} bytes)")
        print(f"volume:     {r.volume_name!r}")
        n_files = sum(1 for _, n in r.walk() if n.type is EntryType.REGULAR)
        n_dirs = sum(1 for _, n in r.walk() if n.type is EntryType.DIRECTORY)
        n_links = sum(1 for _, n in r.walk() if n.type is EntryType.SYMLINK)
        print(f"entries:    {n_files} file, {n_dirs} dir, {n_links} symlink")
        if args.verify:
            print("checksum:   OK")
        print()
        _print_space_breakdown(r)
    return 0


def _entry_payload(node: RomFSNode) -> int:
    """File/symlink content bytes this entry contributes to the image.

    Directories contribute 0. A resolved hardlink points its ``data_offset``
    at another entry's payload, so its content is counted at the target entry
    (not double-counted here).
    """
    if node.type is not EntryType.REGULAR and node.type is not EntryType.SYMLINK:
        return 0
    # A resolved hardlink's data_offset points at the target's payload, which
    # is already counted at that target entry.
    meta = header_meta_size(len(node.name.encode("utf-8")))
    if (
        node.header_offset is not None
        and node.data_offset is not None
        and node.data_offset != node.header_offset + meta
    ):
        return 0
    return node.size


def _subtree_payload(node: RomFSNode) -> int:
    """Total content bytes under a node (the node itself plus all descendants)."""
    total = _entry_payload(node)
    for child in node.children:
        total += _subtree_payload(child)
    return total


def _fmt_mb(n: int) -> str:
    """Format bytes as MB with 2 decimal places."""
    return f"{n / (1024 * 1024):.2f} MB"


def _print_space_breakdown(r: RomFSReader) -> None:
    """Print each top-level entry's content size (MB) and share of total content.

    ``SIZE`` is file/symlink payload only — it excludes romfs format overhead
    (headers, name padding, 16-byte alignment). ``PCT`` is the entry's share of
    the sum of all top-level entries, so the rows sum to 100%.
    """
    rows: list[tuple[int, str]] = []
    for child in r.root.children:
        payload = _subtree_payload(child)
        label = child.name + ("/" if child.type is EntryType.DIRECTORY else "")
        rows.append((payload, label))
    rows.sort(key=lambda x: x[0], reverse=True)

    total = sum(p for p, _ in rows)

    print("Space usage by entry (descending):")
    if not rows:
        print("  (no top-level entries)")
        return

    sw = max(len("SIZE"), *(len(_fmt_mb(p)) for p, _ in rows))
    pctw = max(
        len("PCT"),
        *(len(f"{(p / total * 100 if total else 0.0):.1f}%") for p, _ in rows),
    )
    label_w = max(len("PATH"), *(len(lbl) for _, lbl in rows))
    rule_w = 2 + sw + 2 + pctw + 2 + label_w
    print(f"  {'SIZE':>{sw}}  {'PCT':>{pctw}}  PATH")
    print("-" * rule_w)
    for p, lbl in rows:
        pct_str = f"{((p / total * 100) if total else 0.0):.1f}%"
        print(f"  {_fmt_mb(p):>{sw}}  {pct_str:>{pctw}}  {lbl}")


def _cmd_diff(args: argparse.Namespace) -> int:
    with RomFSReader(args.image_a) as a, RomFSReader(args.image_b) as b:
        entries = diff_images(a, b)
        if entries:
            # Five-column view: SYM | old file | new file | size delta | detail.
            # Columns 2/3 hold the path on each side (blank = absent), so
            # additions/removals are visible at a glance; column 4 is the signed
            # byte delta; column 5 carries exec/target/type notes. The column
            # headers are the two image basenames so the old/new assignment is
            # unambiguous without a separate banner.
            for e in entries:
                if e.status == VOLUME:
                    print(f'~  volume  "{e.target_a}" -> "{e.target_b}"')
            rows = [e for e in entries if e.status != VOLUME]
            if rows:
                name_a = Path(args.image_a).name
                name_b = Path(args.image_b).name
                path_w = max(
                    4, len(name_a), len(name_b), max(len(e.path) for e in rows)
                )
                deltas = [_size_delta(e) for e in rows]
                size_w = max(4, max(len(d) for d in deltas))
                header = (
                    f"    {name_a:<{path_w}}  {name_b:<{path_w}}  "
                    f"{'SIZE':<{size_w}}  DETAIL"
                )
                print(header)
                print("-" * len(header))
                rule_w = len(header)
                for e, d in zip(rows, deltas, strict=True):
                    sym = _STATUS_TAG[e.status][0]
                    print(
                        f"{sym}   {_path_cell(e, 'a'):<{path_w}}  "
                        f"{_path_cell(e, 'b'):<{path_w}}  {d:<{size_w}}  "
                        f"{_detail(e)}"
                    )
                    if args.text and content_differs(e):
                        for line in unified_text_diff(a, b, e):
                            print(line)
            else:
                rule_w = 0
            # A dash rule (matching the header rule) separates the per-path
            # rows from the aggregate footer.
            if rule_w:
                print("-" * rule_w)
            print(
                _format_diff_total(
                    entries,
                    full_a=a.full_size,
                    full_b=b.full_size,
                    disk_a=Path(args.image_a).stat().st_size,
                    disk_b=Path(args.image_b).stat().st_size,
                )
            )
    # Match diff(1): exit 0 when identical, 1 when any difference.
    return 1 if entries else 0


_STATUS_TAG = {
    ADDED: ("+", "added"),
    REMOVED: ("-", "removed"),
    CHANGED: ("~", "changed"),
    TYPE_CHANGED: ("=", "type-changed"),
    VOLUME: ("~", "volume"),
}


def _type_label(t: EntryType | None) -> str:
    if t is None:
        return "?"
    return _TYPE_LABEL.get(t, t.name)


def _signed_b(n: int) -> str:
    """Signed byte count: ``+18B`` / ``-16B`` / ``0B``."""
    if n > 0:
        return f"+{n}B"
    if n < 0:
        return f"{n}B"
    return "0B"


def _path_cell(e: DiffEntry, side: str) -> str:
    """The path on one side, or '' if that side lacks the entry."""
    if side == "a" and e.status == ADDED:
        return ""
    if side == "b" and e.status == REMOVED:
        return ""
    return e.path


def _size_delta(e: DiffEntry) -> str:
    """Signed byte delta for regular files; ``-`` where size is not meaningful.

    Symlinks report their target-string length in ``size``, not payload bytes,
    so they (and type-changes, and the volume row) show ``-`` rather than a
    misleading delta.
    """
    if e.status in (VOLUME, TYPE_CHANGED):
        return "-"
    if e.status == ADDED and e.type_b is EntryType.SYMLINK:
        return "-"
    if e.status == REMOVED and e.type_a is EntryType.SYMLINK:
        return "-"
    if e.status == CHANGED and e.type_a is EntryType.SYMLINK:
        return "-"
    return _signed_b(e.size_b - e.size_a)


def _detail(e: DiffEntry) -> str:
    """Auxiliary notes: exec bit, symlink targets, type change, volume name."""
    if e.status == VOLUME:
        return f'"{e.target_a}" -> "{e.target_b}"'
    if e.status == TYPE_CHANGED:
        return f"{_type_label(e.type_a)}->{_type_label(e.type_b)}"
    if e.status == ADDED:
        if e.type_b is EntryType.SYMLINK:
            return f"-> {e.target_b}"
        if e.type_b is EntryType.DIRECTORY:
            return "dir"
        return "exec" if e.exec_b else ""
    if e.status == REMOVED:
        if e.type_a is EntryType.SYMLINK:
            return f"-> {e.target_a}"
        if e.type_a is EntryType.DIRECTORY:
            return "dir"
        return "exec" if e.exec_a else ""
    # CHANGED (same type on both sides)
    if e.type_a is EntryType.SYMLINK:
        return f"{e.target_a} -> {e.target_b}"
    if e.type_a is EntryType.DIRECTORY:
        return "dir"
    parts = []
    if e.exec_a != e.exec_b:
        parts.append(f"exec:{str(e.exec_a).lower()}->{str(e.exec_b).lower()}")
    elif e.exec_a:
        parts.append("exec")
    if e.same_size_content_diff:
        parts.append("(same size, content differs)")
    return "  ".join(parts)


def _format_diff_total(
    entries: list[DiffEntry],
    *,
    full_a: int,
    full_b: int,
    disk_a: int,
    disk_b: int,
) -> str:
    """Multi-line footer aggregating the diff.

    Three independent size bases are reported on separate lines so they are
    not conflated:

    - ``image:`` — each image's ``full_size`` (the romfs superblock's declared
      volume size: header + node metadata + payload + 16-byte alignment).
    - ``disk:``  — each image's on-disk file size (``stat().st_size``), which
      may be larger than ``full_size`` when the file was ``truncate``-padded to
      a flash-erase-block boundary.
    - ``size:``  — the signed byte delta of *regular-file payload only*
      (``+`` grown/new, ``-`` shrunk/gone, ``->`` net). This is the only line
      that reflects expanded file contents; it does NOT equal
      ``disk_b - disk_a`` because image size also includes header, metadata,
      and alignment.

    ``+/-/~`` match the row symbols. Size sums cover regular files only —
    directories (0B) and symlinks (no size column) contribute to counts but
    not to bytes.
    """
    added = sum(1 for e in entries if e.status == ADDED)
    removed = sum(1 for e in entries if e.status == REMOVED)
    # VOLUME shares the `~` symbol with CHANGED, so count both — the tilde
    # total then matches the number of `~` rows the user sees in the table.
    changed = sum(1 for e in entries if e.status in (CHANGED, VOLUME))
    type_changed = sum(1 for e in entries if e.status == TYPE_CHANGED)

    size_added = 0
    size_removed = 0
    for e in entries:
        # Only regular-file bytes count toward size — matching the table,
        # which shows directories as 0B and symlinks as ``-`` (their size
        # field holds the target-string length, not payload).
        if e.status == ADDED and e.type_b is EntryType.REGULAR:
            size_added += e.size_b
        elif e.status == REMOVED and e.type_a is EntryType.REGULAR:
            size_removed += e.size_a
        elif e.status == CHANGED and e.type_a is EntryType.REGULAR:
            delta = e.size_b - e.size_a
            if delta > 0:
                size_added += delta
            elif delta < 0:
                size_removed += -delta
    net = size_added - size_removed
    # f"{net}B" already renders the minus for negatives; add '+' only for
    # positives so zero reads as a plain "0B".
    net_str = f"+{net}B" if net > 0 else f"{net}B"

    counts = f"TOTAL  +{added} added  -{removed} removed  ~{changed} changed"
    if type_changed:
        counts += f"  ={type_changed} type-changed"
    image = f"image: A={full_a}B  B={full_b}B"
    disk = f"disk:  A={disk_a}B  B={disk_b}B"
    size = f"size:  +{size_added}B  -{size_removed}B  -> {net_str}"
    return f"{counts}\n{image}\n{disk}\n{size}"


if __name__ == "__main__":
    raise SystemExit(main())
