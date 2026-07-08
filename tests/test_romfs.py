# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng

from __future__ import annotations

import struct
from pathlib import Path

import pytest

import romfs
from romfs import EntryType, RomFSReader, RomFSWriter
from romfs.format import (
    ROMFS_IMAGE_ALIGN,
    ROMFS_MAGIC,
    align_up,
    encode_name,
    header_checksum,
    header_meta_size,
    pack_file_head,
    pack_superblock_head,
    superblock_checksum,
)
from romfs.format import (
    EntryType as FEntryType,
)

# --- fixtures --------------------------------------------------------------


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    """A small directory tree exercising files, dirs, symlinks, empty files."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "hello.txt").write_text("hello romfs\n")
    (root / "empty.bin").write_bytes(b"")
    (root / "data.bin").write_bytes(bytes(range(256)) * 4)  # 1024 bytes
    sub = root / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested content\n")
    deep = sub / "deep"
    deep.mkdir()
    (deep / "leaf.txt").write_text("deep leaf\n")
    # Symlink to a sibling file.
    (root / "link.txt").symlink_to("hello.txt")
    # Symlink to a directory.
    (sub / "linkdir").symlink_to("deep")
    return root


@pytest.fixture
def sample_image(sample_tree: Path, tmp_path: Path) -> Path:
    img = tmp_path / "out.img"
    RomFSWriter.from_directory(sample_tree, volume_name="testvol").write(img)
    return img


# --- round-trip ------------------------------------------------------------


def _collect(reader: RomFSReader) -> dict[str, tuple[EntryType, int]]:
    return {path: (node.type, node.size) for path, node in reader.walk()}


def test_round_trip_structure(sample_tree: Path, sample_image: Path) -> None:
    expected = {
        "hello.txt": (EntryType.REGULAR, len("hello romfs\n")),
        "empty.bin": (EntryType.REGULAR, 0),
        "data.bin": (EntryType.REGULAR, 1024),
        "sub": (EntryType.DIRECTORY, 0),
        "sub/nested.txt": (EntryType.REGULAR, len("nested content\n")),
        "sub/deep": (EntryType.DIRECTORY, 0),
        "sub/deep/leaf.txt": (EntryType.REGULAR, len("deep leaf\n")),
        "link.txt": (EntryType.SYMLINK, len("hello.txt")),
        "sub/linkdir": (EntryType.SYMLINK, len("deep")),
    }
    with RomFSReader(sample_image) as r:
        assert _collect(r) == expected


def test_round_trip_contents(sample_image: Path) -> None:
    with RomFSReader(sample_image) as r:
        contents = {
            path: r.read(node)
            for path, node in r.walk()
            if node.type is EntryType.REGULAR
        }
    assert contents["hello.txt"] == b"hello romfs\n"
    assert contents["empty.bin"] == b""
    assert contents["data.bin"] == bytes(range(256)) * 4
    assert contents["sub/nested.txt"] == b"nested content\n"
    assert contents["sub/deep/leaf.txt"] == b"deep leaf\n"


def test_round_trip_symlink_targets(sample_image: Path) -> None:
    with RomFSReader(sample_image) as r:
        links = {
            path: r.read(node).decode()
            for path, node in r.walk()
            if node.type is EntryType.SYMLINK
        }
    assert links["link.txt"] == "hello.txt"
    assert links["sub/linkdir"] == "deep"


def test_volume_name_round_trip(sample_image: Path) -> None:
    with RomFSReader(sample_image) as r:
        assert r.volume_name == "testvol"


def test_extract_restores_tree(
    sample_tree: Path, sample_image: Path, tmp_path: Path
) -> None:
    out = tmp_path / "extracted"
    with RomFSReader(sample_image) as r:
        r.extract(out)
    # Every regular file in the source tree reappears with identical bytes.
    for src in sample_tree.rglob("*"):
        if src.is_file() and not src.is_symlink():
            rel = src.relative_to(sample_tree)
            assert (out / rel).read_bytes() == src.read_bytes()


# --- format conformance ----------------------------------------------------


def test_magic_and_full_size(sample_image: Path) -> None:
    data = sample_image.read_bytes()
    assert data[:8] == ROMFS_MAGIC
    full_size = struct.unpack(">I", data[8:12])[0]
    assert full_size == len(data)  # image is padded to 1024, full_size includes pad
    assert full_size % ROMFS_IMAGE_ALIGN == 0


def test_superblock_checksum_zero(sample_image: Path) -> None:
    data = sample_image.read_bytes()
    window = min(len(data), 512)
    # The kernel sums every word (including the checksum field) and requires
    # the total to be zero — so sum the raw block, no zeroing.
    total = 0
    for i in range(0, window, 4):
        total += struct.unpack_from(">I", data, i)[0]
    assert total & 0xFFFFFFFF == 0


def test_all_headers_16_aligned(sample_image: Path) -> None:
    with RomFSReader(sample_image) as r:
        offsets = [n.header_offset for _, n in r.walk()]
        offsets.append(r.root.header_offset)
    for off in offsets:
        assert off is not None
        assert off % 16 == 0, f"header at {off} not 16-aligned"


def test_data_offsets_16_aligned(sample_image: Path) -> None:
    with RomFSReader(sample_image) as r:
        for _, node in r.walk():
            if node.type in (EntryType.REGULAR, EntryType.SYMLINK):
                assert node.data_offset is not None
                assert node.data_offset % 16 == 0


def test_header_checksums_zero(sample_image: Path) -> None:
    """Each file header (16 fixed bytes + padded name) must sum to zero."""
    data = sample_image.read_bytes()
    with RomFSReader(sample_image) as r:
        nodes = [r.root, *(n for _, n in r.walk())]
        for node in nodes:
            off = node.header_offset
            assert off is not None
            name_len = (
                len(node.name.encode("utf-8"))
                if node is not r.root
                else len(r.volume_name.encode("utf-8"))
            )
            meta = header_meta_size(name_len)
            header = data[off : off + meta]
            total = 0
            for i in range(0, len(header), 4):
                total += struct.unpack_from(">I", header, i)[0]
            assert total & 0xFFFFFFFF == 0, f"header at {off} does not sum to zero"


# --- genromfs-style images (with . / .. / hardlinks) -----------------------


def _pack_entry(next_off: int, etype: int, spec: int, size: int, name: str) -> bytes:
    """Pack one file header (16-byte fixed part + padded name) with checksum."""
    name_field = encode_name(name)
    head = pack_file_head(next_off | (etype & 0x07), spec, size, 0) + name_field
    checksum = header_checksum(head)
    return pack_file_head(next_off | (etype & 0x07), spec, size, checksum) + name_field


def _build_genromfs_style_image() -> bytes:
    """Build a tiny image the way genromfs does: every directory starts with a
    "." self-link and a ".." parent hardlink, plus a file hardlink for dedup.

    Layout (volume name "t", root_off = 32):
      @32  root "."    DIR  spec=32(self)  next=@64
      @64  root ".."   HRD  spec=32(parent) next=@96
      @96  a.txt       REG  size=2          next=@144   data="hi"
      @144 b.txt       HRD  spec=96(->a)    next=@176
      @176 sub         DIR  spec=208(sub/.) next=0
      @208 sub "."     DIR  spec=208(self)  next=@240
      @240 sub ".."    HRD  spec=176(parent) next=@272
      @272 c.txt       REG  size=1          next=0      data="x"
    """
    DIR, HRD, REG = (
        int(FEntryType.DIRECTORY),
        int(FEntryType.HARDLINK),
        int(FEntryType.REGULAR),
    )
    entries = [
        (32, 64, DIR, 32, 0, ".", b""),
        (64, 96, HRD, 32, 0, "..", b""),
        (96, 144, REG, 0, 2, "a.txt", b"hi"),
        (144, 176, HRD, 96, 0, "b.txt", b""),
        (176, 0, DIR, 208, 0, "sub", b""),
        (208, 240, DIR, 208, 0, ".", b""),
        (240, 272, HRD, 176, 0, "..", b""),
        (272, 0, REG, 0, 1, "c.txt", b"x"),
    ]
    content_end = (
        272 + header_meta_size(len("c.txt")) + align_up(1, 16)
    )  # 272+32+16 = 320
    total = align_up(content_end, ROMFS_IMAGE_ALIGN)
    buf = bytearray(total)

    for off, nxt, etype, spec, size, name, data in entries:
        buf[off : off + header_meta_size(len(name))] = _pack_entry(
            nxt, etype, spec, size, name
        )
        if data:
            data_off = off + header_meta_size(len(name))
            buf[data_off : data_off + len(data)] = data

    # Superblock: magic + full_size + checksum + volume name "t".
    head = pack_superblock_head(total, 0)
    name_field = encode_name("t")
    buf[0 : len(head)] = head
    buf[len(head) : len(head) + len(name_field)] = name_field
    checksum = superblock_checksum(bytes(buf[:512]))
    buf[0 : len(head)] = pack_superblock_head(total, checksum)
    return bytes(buf)


def test_genromfs_style_image_parses(tmp_path: Path) -> None:
    img = tmp_path / "genromfs.img"
    img.write_bytes(_build_genromfs_style_image())
    with RomFSReader(img) as r:
        # "." and ".." must NOT appear as children.
        paths = {path for path, _ in r.walk()}
        assert paths == {"a.txt", "b.txt", "sub", "sub/c.txt"}
        assert r.volume_name == "t"


def test_genromfs_style_hardlink_resolves(tmp_path: Path) -> None:
    img = tmp_path / "genromfs.img"
    img.write_bytes(_build_genromfs_style_image())
    with RomFSReader(img) as r:
        nodes = {path: n for path, n in r.walk()}
        # b.txt is a hardlink to a.txt — it must read as the target's content.
        assert r.read(nodes["a.txt"]) == b"hi"
        assert r.read(nodes["b.txt"]) == b"hi"
        assert nodes["b.txt"].type is EntryType.REGULAR
        assert r.read(nodes["sub/c.txt"]) == b"x"


# --- error handling --------------------------------------------------------


def test_bad_magic_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.img"
    bad.write_bytes(b"NOTROMFS" + b"\x00" * 64)
    with pytest.raises(romfs.BadMagicError):
        RomFSReader(bad)


def test_truncated_image_rejected(sample_image: Path, tmp_path: Path) -> None:
    truncated = tmp_path / "trunc.img"
    truncated.write_bytes(sample_image.read_bytes()[:32])
    with pytest.raises((romfs.TruncatedImageError, romfs.BadMagicError)):
        RomFSReader(truncated)


def test_checksum_mismatch_detected(sample_image: Path, tmp_path: Path) -> None:
    """Flipping a byte in the first 512 bytes (outside the checksum field) must
    fail verify."""
    data = bytearray(sample_image.read_bytes())
    # Flip a byte in the volume name (offset 16), not the checksum field.
    data[16] ^= 0xFF
    mutated = tmp_path / "mut.img"
    mutated.write_bytes(data)
    with pytest.raises(romfs.ChecksumMismatchError):
        RomFSReader(mutated, verify=True)


def test_verify_off_skips_checksum(sample_image: Path, tmp_path: Path) -> None:
    data = bytearray(sample_image.read_bytes())
    data[16] ^= 0xFF
    mutated = tmp_path / "mut.img"
    mutated.write_bytes(data)
    # With verify=False the reader must not raise on the bad checksum.
    with RomFSReader(mutated, verify=False) as r:
        assert r.volume_name != "testvol"  # the mutated byte changed it


# --- empty image -----------------------------------------------------------


def test_empty_directory_image(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    img = tmp_path / "empty.img"
    RomFSWriter.from_directory(root, volume_name="").write(img)
    with RomFSReader(img) as r:
        assert list(r.walk()) == []
        assert r.root.children == []


# --- CLI smoke -------------------------------------------------------------


def test_cli_list_and_extract(sample_image: Path, tmp_path: Path, capsys) -> None:
    from romfs.cli import main

    rc = main(["list", str(sample_image)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hello.txt" in out
    assert "sub/deep/leaf.txt" in out

    rc = main(["extract", str(sample_image), "hello.txt"])
    assert rc == 0
    assert capsys.readouterr().out == "hello romfs\n"


def test_cli_pack(tmp_path: Path) -> None:
    from romfs.cli import main

    root = tmp_path / "src"
    root.mkdir()
    (root / "a.txt").write_text("abc")
    img = tmp_path / "packed.img"
    rc = main(["pack", str(root), "-o", str(img), "-n", "cli"])
    assert rc == 0
    with RomFSReader(img) as r:
        assert r.volume_name == "cli"
        assert r.read(r.root.children[0]) == b"abc"
