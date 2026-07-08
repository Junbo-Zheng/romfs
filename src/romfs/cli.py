# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng
"""Command-line interface for the romfs package."""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .errors import RomFSError
from .format import EntryType
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="romfs",
        description=f"romfs {_version()} — read and write Linux romfs images.",
    )
    p.add_argument("-V", "--version", action="version", version=f"romfs {_version()}")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list the file tree in an image")
    p_list.add_argument("image")

    p_extract = sub.add_parser("extract", help="extract files from an image")
    p_extract.add_argument("image")
    p_extract.add_argument(
        "path", nargs="?", help="extract a single entry to stdout (else all)"
    )
    p_extract.add_argument(
        "-o", "--outdir", help="output directory for full extraction"
    )

    p_pack = sub.add_parser("pack", help="build an image from a directory tree")
    p_pack.add_argument("srcdir")
    p_pack.add_argument("-o", "--out", required=True, help="output image path")
    p_pack.add_argument("-n", "--name", default="", help="volume name")

    p_info = sub.add_parser("info", help="print image header info")
    p_info.add_argument("image")
    p_info.add_argument(
        "--verify", action="store_true", help="verify the superblock checksum"
    )

    args = p.parse_args(argv)

    try:
        if args.cmd == "list":
            return _cmd_list(args)
        if args.cmd == "extract":
            return _cmd_extract(args)
        if args.cmd == "pack":
            return _cmd_pack(args)
        if args.cmd == "info":
            return _cmd_info(args)
    except RomFSError as e:
        print(f"romfs: error: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"romfs: error: {e}", file=sys.stderr)
        return 1

    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    with RomFSReader(args.image) as r:
        for path, node in r.walk():
            label = _TYPE_LABEL.get(node.type, "?")
            print(f"{label} {node.size:>10}  {path}")
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    with RomFSReader(args.image) as r:
        if args.path:
            node = _find(r, args.path)
            if node is None:
                print(f"romfs: error: path not found: {args.path}", file=sys.stderr)
                return 1
            if node.type is EntryType.DIRECTORY:
                print(
                    f"romfs: error: cannot extract a directory: {args.path}",
                    file=sys.stderr,
                )
                return 1
            sys.stdout.buffer.write(r.read(node))
            return 0
        outdir = args.outdir or Path(args.image).stem + ".extracted"
        r.extract(outdir)
        print(f"extracted to {outdir}")
    return 0


def _cmd_pack(args: argparse.Namespace) -> int:
    RomFSWriter.from_directory(args.srcdir, volume_name=args.name).write(args.out)
    print(f"packed {args.srcdir} -> {args.out}")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    with RomFSReader(args.image, verify=args.verify) as r:
        print(f"image:      {args.image}")
        print("magic:      -rom1fs-")
        print(f"full_size:  {r.full_size} bytes")
        print(f"volume:     {r.volume_name!r}")
        n_files = sum(1 for _, n in r.walk() if n.type is EntryType.REGULAR)
        n_dirs = sum(1 for _, n in r.walk() if n.type is EntryType.DIRECTORY)
        n_links = sum(1 for _, n in r.walk() if n.type is EntryType.SYMLINK)
        print(f"entries:    {n_files} file, {n_dirs} dir, {n_links} symlink")
        if args.verify:
            print("checksum:   OK")
    return 0


def _find(reader: RomFSReader, path: str) -> RomFSNode | None:
    path = path.lstrip("/")
    for p, node in reader.walk():
        if p == path:
            return node
    return None


if __name__ == "__main__":
    raise SystemExit(main())
