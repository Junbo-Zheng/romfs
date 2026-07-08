# romfs

[![PyPI](https://img.shields.io/pypi/v/romfs.svg)](https://pypi.org/project/romfs/)
[![CI](https://github.com/Junbo-Zheng/romfs/actions/workflows/ci.yml/badge.svg)](https://github.com/Junbo-Zheng/romfs/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/romfs.svg)](https://pypi.org/project/romfs/)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)

> Pure-Python reader and writer for Linux romfs (`-rom1fs-`) filesystem images.
> Standard library only, no runtime dependencies.

`romfs` lets you build, inspect, and extract the small read-only filesystem
images the Linux kernel uses for initrds and embedded root filesystems. Images
produced by the writer are byte-compatible with `mount -t romfs` and
interchangeable with `genromfs`; the reader parses any such image, including
real `genromfs` output with `.`/`..` self-links and dedup hardlinks.

## Features

- **Read** an image into a node tree; list entries and read file contents
  lazily via `mmap` — large images don't blow up memory.
- **Write** an image from a local directory tree, preserving the structure,
  regular files, and symbolic links.
- **CLI** with `list`, `extract`, `pack`, and `info` subcommands.
- **Importable library** API for embedding in other tools.
- Byte-exact conformance with the kernel romfs format: 16-byte alignment,
  big-endian, superblock + header checksums.
- Entries sorted by name for a deterministic, diff-friendly layout.

> [!NOTE]
> The writer lays out entries sorted by name, so two versions of the same
> resource tree produce byte-stable images — keeping binary diffs (delta
> packages) small.

## Installation

```bash
pip install romfs
```

Run from source with no install:

```bash
./main.py <args>
```

## Quick start

```bash
# Pack a folder tree into a single romfs image
romfs pack ./rootfs -o rootfs.img -n myvol

# List the file tree (type, size, path)
romfs list rootfs.img

# Extract one file to stdout, or everything to a directory
romfs extract rootfs.img etc/init.d/rcS
romfs extract rootfs.img -o ./out

# Show header info and verify the superblock checksum
romfs info rootfs.img --verify
```

## CLI reference

```text
romfs list   <image>                     list the file tree (path, type, size)
romfs extract <image> [-o OUTDIR] [PATH] extract all, or one entry to stdout
romfs pack <srcdir> -o <image> [-n NAME] build an image from a directory tree
romfs info <image> [--verify]            print header info + checksum check
```

`romfs -V` prints the version; `romfs -h` shows help.

## Library API

```python
from romfs import RomFSReader, RomFSWriter, EntryType

# Read an image
with RomFSReader("rootfs.img") as r:
    print(r.volume_name, r.full_size)
    for path, node in r.walk():
        print(f"{node.type.name:9} {node.size:>8}  {path}")
    # File contents are sliced from the mmap on demand
    data = r.read(r.root.children[0])

# Build an image from a directory tree
RomFSWriter.from_directory("./rootfs", volume_name="myvol").write("out.img")
```

## Supported entry types

| Type                                | Read | Write |
|-------------------------------------|:----:|:-----:|
| Regular file                        | yes  | yes   |
| Directory                           | yes  | yes   |
| Symlink                             | yes  | yes   |
| Hardlink / device / socket / fifo   | yes  | no    |

> [!NOTE]
> romfs does not store per-file Unix mode bits in the standard format, so the
> kernel assigns fixed default modes. Permissions are not preserved on write.
> Hardlinks and special files are parsed on read but not emitted on write.

## Development

```bash
pip install -e ".[dev]"        # pytest, black, ruff, mypy, build, twine
pytest                          # runs against src/ directly, no install needed
black src tests main.py         # format
ruff check src tests            # lint
mypy src                        # type check
python -m build                 # build sdist + wheel into dist/
```

CI runs black + ruff + mypy + pytest across Python 3.10-3.13, plus a packaging
job that builds the wheel and tests the installed package. Releases publish to
PyPI automatically on a GitHub Release via Trusted Publishing (OIDC).
