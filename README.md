# romfs

[![PyPI](https://img.shields.io/pypi/v/romfs.svg)](https://pypi.org/project/romfs/)
[![CI](https://github.com/Junbo-Zheng/romfs/actions/workflows/ci.yml/badge.svg)](https://github.com/Junbo-Zheng/romfs/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/romfs.svg)](https://pypi.org/project/romfs/)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)

> Pure-Python reader and writer for Linux romfs (`-rom1fs-`) filesystem images.
> Standard library only, no runtime dependencies.

`romfs` lets you build, inspect, and unpack the small read-only filesystem
images the Linux kernel uses for initrds and embedded root filesystems. Images
produced by the writer are byte-compatible with `mount -t romfs` and
interchangeable with `genromfs`; the reader parses any such image, including
real `genromfs` output with `.`/`..` self-links and dedup hardlinks.

## Features

- **Read** an image into a node tree; list entries and read file contents
  lazily via `mmap` — large images don't blow up memory.
- **Write** an image from a local directory tree, preserving the structure,
  regular files, and symbolic links.
- **CLI** with `list`, `unpack`, `pack`, `info`, and `diff` subcommands.
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
# `list` is the default — an image path with no subcommand is shorthand for it
romfs rootfs.img

# Unpack one file to stdout, or everything to a directory
romfs unpack rootfs.img etc/init.d/rcS
romfs unpack rootfs.img -o ./out

# Show header info and verify the superblock checksum
romfs info rootfs.img --verify

# Compare two images file-by-file (exit 0 if identical, 1 if different)
romfs diff old.img new.img
romfs diff old.img new.img --text   # also print a line-level diff for changed text files
```

`diff` output is a five-column table — symbol | old file | new file | size delta
| detail — framed by dash rules. Columns 2/3 hold the path on each side (blank =
absent), so an addition (blank old cell) or removal (blank new cell) is visible at
a glance; column 4 is the signed byte delta. The column headers are the two image
basenames. A leading `~ volume` row appears when the volume name differs:

```text
~  volume  "v1" -> "v2"
    old.img         new.img         SIZE    DETAIL
-------------------------------------------------------
+                   bin/newtool     +12345B exec
~   etc/init.d/rcS  etc/init.d/rcS  -4B     exec  (same size, content differs)
-   etc/old.conf                    -89B
~   init            init            -       rcS -> rc.local
~   lib/libfoo.so   lib/libfoo.so   0B      (same size, content differs)
=   var/run         var/run         -       f->l
-------------------------------------------------------
TOTAL  +1 added  -1 removed  ~4 changed  =1 type-changed
image: A=65536B  B=65536B
disk:  A=131072B  B=131072B
size:  +12345B  -93B  -> +12252B
```

The far-left symbol is `+` added (in new only), `-` removed (in old only), `~`
changed (both sides), `=` type-changed (both sides); a blank path cell on either
side means that image lacks the entry. The SIZE column is the signed byte delta
(`+N` grew, `-N` shrank, `0B` unchanged size); `-` means size does not apply
(symlink / type-change / volume). The DETAIL column carries the executable bit (`exec`, or `exec:true->false` when
it flips), `dir` for directories, symlink targets (`-> target` or `old -> new`),
the type pair for a type-change (`f->l`), and the `(same size, content differs)`
flag — regular files of equal size are compared byte-for-byte, so same-size
content edits are surfaced explicitly. The trailing footer reports three
independent size bases on separate lines so they are not conflated: `image:`
is each image's `full_size` (the romfs superblock's declared volume size —
header, node metadata, payload, 16-byte alignment); `disk:` is the on-disk
file size, which may exceed `full_size` when the file was `truncate`-padded to
a flash-erase-block boundary; `size:` is the signed byte delta of
regular-file payload only (`+XB` appeared, `-YB` disappeared, `->` the signed
net). `size:` does NOT equal `disk_b - disk_a`, because image size also
includes header, metadata, and alignment — which is why the three are split.

## CLI reference

```text
romfs <image>                            shorthand for `list <image>`
romfs list   <image> [--sort S] [--all]  list the file tree (size-0 hidden without --all)
romfs unpack <image> [-o OUTDIR] [PATH] unpack all, or one entry to stdout
romfs pack <srcdir> -o <image> [-n NAME] build an image from a directory tree
romfs info <image> [--verify]            print header info + checksum check
romfs diff <a.img> <b.img> [--text]      compare two images file-by-file (exit 1 if different)
```

`romfs -V` prints the version; `romfs -h` shows help. `list --sort` orders by
`name` (default), `size` (largest first), or `none` (on-disk order); `--all`
also shows size-0 entries (directories and empty files).

## Library API

```python
from romfs import RomFSReader, RomFSWriter, EntryType, diff_images

# Read an image
with RomFSReader("rootfs.img") as r:
    print(r.volume_name, r.full_size)
    for path, node in r.walk():
        print(f"{node.type.name:9} {node.size:>8}  {path}")
    # File contents are sliced from the mmap on demand
    data = r.read(r.root.children[0])

    # Path-based access: find a node, read its payload, list a directory.
    node = r.find("etc/init.d/rcS")     # None if absent
    if node is not None:
        print(node.path, node.executable)
        print(r.read_path("etc/init.d/rcS"))
    print([n.name for n in r.listdir("etc")])

# Build an image from a directory tree
RomFSWriter.from_directory("./rootfs", volume_name="myvol").write("out.img")

# Compare two images: added / removed / changed / type-changed paths.
# Regular files of equal size are compared byte-for-byte, so same-size
# content edits are caught, not just size changes.
with RomFSReader("old.img") as a, RomFSReader("new.img") as b:
    for entry in diff_images(a, b):
        print(entry.status, entry.path)
```

## Supported entry types

| Type                                | Read | Write |
|-------------------------------------|:----:|:-----:|
| Regular file                        | yes  | yes   |
| Directory                           | yes  | yes   |
| Symlink                             | yes  | yes   |
| Hardlink / device / socket / fifo   | yes  | no    |

> [!NOTE]
> romfs stores only the executable bit (no full Unix mode), so the kernel
> assigns fixed default modes for the rest. The writer preserves the exec bit
> from the source file (set when any execute bit is on, matching `genromfs`);
> other permission bits are not preserved. Hardlinks and special files are
> parsed on read but not emitted on write.

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
