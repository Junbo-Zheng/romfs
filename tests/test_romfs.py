# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng

from __future__ import annotations

import struct
from pathlib import Path

import pytest

import romfs
from romfs import EntryType, RomFSReader, RomFSWriter
from romfs.diff import diff_images
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


def test_unpack_restores_tree(
    sample_tree: Path, sample_image: Path, tmp_path: Path
) -> None:
    out = tmp_path / "unpacked"
    with RomFSReader(sample_image) as r:
        r.unpack(out)
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


def test_cli_list_and_unpack(sample_image: Path, tmp_path: Path, capsys) -> None:
    from romfs.cli import main

    rc = main(["list", str(sample_image)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hello.txt" in out
    assert "sub/deep/leaf.txt" in out

    rc = main(["unpack", str(sample_image), "hello.txt"])
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


def test_cli_info_space_breakdown(sample_image: Path, capsys) -> None:
    from romfs.cli import main

    rc = main(["info", str(sample_image)])
    assert rc == 0
    out = capsys.readouterr().out

    # The header block and the space breakdown are both printed by `info`.
    assert "full_size:" in out
    assert "Space usage by entry" in out
    assert "SIZE" in out and "PCT" in out

    # Sizes are shown in MB, not raw bytes.
    assert "MB" in out

    # Top-level entries from sample_tree appear; directories get a trailing /.
    assert "data.bin" in out
    assert "sub/" in out

    # The old reconciliation/summary rows are gone — only per-entry rows remain.
    assert "(full_size)" not in out
    assert "(entries)" not in out
    assert "(image padding)" not in out


# --- executable bit --------------------------------------------------------


def test_executable_bit_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "x"
    root.mkdir()
    (root / "plain.sh").write_bytes(b"plain")
    (root / "plain.sh").chmod(0o644)
    (root / "exec.sh").write_bytes(b"exec")
    (root / "exec.sh").chmod(0o755)
    img = tmp_path / "x.img"
    RomFSWriter.from_directory(root, volume_name="x").write(img)
    with RomFSReader(img) as r:
        assert r.find("plain.sh").executable is False
        assert r.find("exec.sh").executable is True


def test_executable_bit_matches_genromfs(tmp_path: Path) -> None:
    """Any execute bit (owner/group/other) sets ROMFH_EXEC, matching genromfs."""
    root = tmp_path / "g"
    root.mkdir()
    (root / "owner").write_bytes(b"a")
    (root / "owner").chmod(0o755)  # owner-exec
    (root / "group").write_bytes(b"b")
    (root / "group").chmod(0o674)  # group-exec only (owner not)
    (root / "none").write_bytes(b"c")
    (root / "none").chmod(0o644)
    img = tmp_path / "g.img"
    RomFSWriter.from_directory(root, volume_name="g").write(img)
    with RomFSReader(img) as r:
        assert r.find("owner").executable is True
        assert r.find("group").executable is True
        assert r.find("none").executable is False


# --- path-based API --------------------------------------------------------


def test_find_and_read_path(sample_image: Path) -> None:
    with RomFSReader(sample_image) as r:
        assert r.find("hello.txt") is not None
        assert r.find("nope.txt") is None
        assert r.find("") is r.root
        assert r.find("/sub/deep/leaf.txt").name == "leaf.txt"
        assert r.read_path("hello.txt") == b"hello romfs\n"
        assert r.read_path("sub/deep/leaf.txt") == b"deep leaf\n"
        # Symlink payload (its target string) is readable by path too.
        assert r.read_path("link.txt") == b"hello.txt"


def test_read_path_missing_raises(sample_image: Path) -> None:
    with RomFSReader(sample_image) as r:
        with pytest.raises(FileNotFoundError):
            r.read_path("missing.txt")
        # A directory path raises IsADirectoryError, not TypeError — keeps the
        # error contract uniform with listdir's NotADirectoryError.
        with pytest.raises(IsADirectoryError):
            r.read_path("sub")


def test_listdir(sample_image: Path) -> None:
    with RomFSReader(sample_image) as r:
        root_names = sorted(n.name for n in r.listdir())
        assert root_names == sorted(
            ["hello.txt", "empty.bin", "data.bin", "sub", "link.txt"]
        )
        sub_names = sorted(n.name for n in r.listdir("sub"))
        assert sub_names == sorted(["nested.txt", "deep", "linkdir"])
        with pytest.raises(NotADirectoryError):
            r.listdir("hello.txt")


# --- node.path -------------------------------------------------------------


def test_node_path(sample_image: Path) -> None:
    with RomFSReader(sample_image) as r:
        assert r.root.path == ""
        assert r.find("hello.txt").path == "hello.txt"
        assert r.find("sub/deep/leaf.txt").path == "sub/deep/leaf.txt"


# --- diff ------------------------------------------------------------------


def _pack(tree: Path, img: Path, vol: str = "v") -> Path:
    RomFSWriter.from_directory(tree, volume_name=vol).write(img)
    return img


def _build_diff_trees(tmp_path: Path) -> tuple[Path, Path]:
    """Two trees exercising every diff status: same/grown/edited-same-size/
    removed/type-changed/symlink-target/exec-bit/added."""
    ta = tmp_path / "a"
    ta.mkdir()
    (ta / "same.txt").write_text("same")
    (ta / "grown.txt").write_text("short")
    (ta / "edited.txt").write_text("AAAA")  # same size, content differs
    (ta / "gone.txt").write_text("removed")
    (ta / "file_to_link").write_text("x")  # becomes a symlink in B
    (ta / "link.txt").symlink_to("same.txt")  # target changes in B
    (ta / "exec.sh").write_text("#!/bin/sh\n")
    (ta / "exec.sh").chmod(0o755)  # exec bit cleared in B

    tb = tmp_path / "b"
    tb.mkdir()
    (tb / "same.txt").write_text("same")
    (tb / "grown.txt").write_text("a bit longer")  # size differs
    (tb / "edited.txt").write_text("BBBB")  # same size (4), content differs
    # gone.txt absent in B
    (tb / "file_to_link").symlink_to("same.txt")  # now a symlink
    (tb / "link.txt").symlink_to("grown.txt")  # target changed
    (tb / "exec.sh").write_text("#!/bin/sh\n")
    (tb / "exec.sh").chmod(0o644)  # exec off, content identical
    (tb / "new.txt").write_text("new")  # added

    return _pack(ta, tmp_path / "a.img"), _pack(tb, tmp_path / "b.img")


def test_diff_full(tmp_path: Path) -> None:
    ia, ib = _build_diff_trees(tmp_path)
    with RomFSReader(ia) as ra, RomFSReader(ib) as rb:
        entries = {e.path: e for e in diff_images(ra, rb)}

    # Unchanged file is omitted entirely.
    assert "same.txt" not in entries
    # Size change.
    assert entries["grown.txt"].status == "changed"
    assert entries["grown.txt"].size_a != entries["grown.txt"].size_b
    assert entries["grown.txt"].same_size_content_diff is False
    # Same-size content diff (the L2 catch).
    assert entries["edited.txt"].status == "changed"
    assert entries["edited.txt"].size_a == entries["edited.txt"].size_b == 4
    assert entries["edited.txt"].same_size_content_diff is True
    # Removed / added.
    assert entries["gone.txt"].status == "removed"
    assert entries["new.txt"].status == "added"
    # Type change.
    assert entries["file_to_link"].status == "type-changed"
    assert entries["file_to_link"].type_a is EntryType.REGULAR
    assert entries["file_to_link"].type_b is EntryType.SYMLINK
    # Symlink target change.
    assert entries["link.txt"].target_a == "same.txt"
    assert entries["link.txt"].target_b == "grown.txt"
    # Exec bit change (content identical).
    assert entries["exec.sh"].exec_a is True
    assert entries["exec.sh"].exec_b is False
    assert entries["exec.sh"].same_size_content_diff is False


def test_diff_identical_empty(tmp_path: Path) -> None:
    ta = tmp_path / "a"
    ta.mkdir()
    (ta / "f").write_text("x")
    ia = tmp_path / "a.img"
    ib = tmp_path / "b.img"
    _pack(ta, ia)
    _pack(ta, ib)
    with RomFSReader(ia) as ra, RomFSReader(ib) as rb:
        assert diff_images(ra, rb) == []


def test_diff_added_vs_removed_direction(tmp_path: Path) -> None:
    ta = tmp_path / "a"
    ta.mkdir()
    (ta / "f").write_text("x")
    tb = tmp_path / "b"
    tb.mkdir()
    ia = tmp_path / "a.img"
    ib = tmp_path / "b.img"
    _pack(ta, ia)
    _pack(tb, ib)
    with RomFSReader(ia) as ra, RomFSReader(ib) as rb:
        forward = {e.path: e for e in diff_images(ra, rb)}
    assert forward["f"].status == "removed"
    with RomFSReader(ib) as ra, RomFSReader(ia) as rb:
        backward = {e.path: e for e in diff_images(ra, rb)}
    assert backward["f"].status == "added"


def test_diff_cli_exit_codes(tmp_path: Path, capsys) -> None:
    from romfs.cli import main

    ta = tmp_path / "a"
    ta.mkdir()
    (ta / "f").write_text("x")
    ia = tmp_path / "a.img"
    ib = tmp_path / "b.img"
    _pack(ta, ia)
    _pack(ta, ib)
    # Identical -> exit 0, no output.
    assert main(["diff", str(ia), str(ib)]) == 0
    assert capsys.readouterr().out == ""

    # Mutate B -> exit 1, path printed.
    (ta / "f").write_text("y")
    _pack(ta, ib)
    assert main(["diff", str(ia), str(ib)]) == 1
    out = capsys.readouterr().out
    assert "f" in out


def test_diff_cli_text(tmp_path: Path, capsys) -> None:
    from romfs.cli import main

    ta = tmp_path / "a"
    ta.mkdir()
    (ta / "c.txt").write_text("line1\nline2\nline3\n")
    tb = tmp_path / "b"
    tb.mkdir()
    (tb / "c.txt").write_text("line1\nCHANGED\nline3\n")
    ia = tmp_path / "a.img"
    ib = tmp_path / "b.img"
    _pack(ta, ia)
    _pack(tb, ib)
    rc = main(["diff", str(ia), str(ib), "--text"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "c.txt" in out
    assert "-line2" in out
    assert "+CHANGED" in out


def test_diff_cli_text_skips_binary(tmp_path: Path, capsys) -> None:
    from romfs.cli import main

    ta = tmp_path / "a"
    ta.mkdir()
    (ta / "b.bin").write_bytes(b"\x00\x01\x02AAA")
    tb = tmp_path / "b"
    tb.mkdir()
    (tb / "b.bin").write_bytes(b"\x00\x01\x02BBB")
    ia = tmp_path / "a.img"
    ib = tmp_path / "b.img"
    _pack(ta, ia)
    _pack(tb, ib)
    rc = main(["diff", str(ia), str(ib), "--text"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "b.bin" in out
    assert "same size, content differs" in out
    assert "@@" not in out  # no unified-diff hunk for a binary file


def test_diff_cli_total_line(tmp_path: Path, capsys) -> None:
    from romfs.cli import main

    ia, ib = _build_diff_trees(tmp_path)
    rc = main(["diff", str(ia), str(ib)])
    assert rc == 1
    out = capsys.readouterr().out

    # Aggregate footer: counts match the row symbols, size sums only regular
    # files (grown +7, new +3 vs gone -7 -> net +3), and a dash rule (not a
    # blank line) separates it from the per-path rows. Image/disk sizes are
    # reported on separate lines from the content delta so the three bases
    # (romfs-declared full_size, on-disk file size, regular-file payload) are
    # not conflated.
    assert "TOTAL  +1 added  -1 removed  ~4 changed  =1 type-changed" in out
    assert "image: A=" in out and out.count("  B=") >= 2
    assert "disk:  A=" in out
    assert "size:  +10B  -7B  -> +3B" in out
    lines = out.splitlines()
    total_idx = next(i for i, ln in enumerate(lines) if ln.startswith("TOTAL"))
    assert set(lines[total_idx - 1]) == {"-"}  # dash rule directly above TOTAL

    # Identical images print no footer at all.
    rc2 = main(["diff", str(ia), str(ia)])
    assert rc2 == 0
    assert "TOTAL" not in capsys.readouterr().out


def test_diff_cli_image_vs_disk_size(tmp_path: Path, capsys) -> None:
    """``truncate``-padding must surface as disk > full_size, not be hidden.

    The footer's reason for splitting ``image:`` (romfs ``full_size``) from
    ``disk:`` (on-disk file size) is exactly this case: an image padded to a
    flash-erase-block boundary has disk size > its romfs-declared size. The
    two values must differ, and the content-delta ``size:`` line must be
    independent of both.
    """
    from romfs.cli import main

    ta = tmp_path / "a"
    ta.mkdir()
    (ta / "f.txt").write_text("hello")
    ia = _pack(ta, tmp_path / "a.img")

    with RomFSReader(ia) as r:
        full_size = r.full_size
    padded_size = full_size + 512  # simulate erase-block alignment padding
    with open(ia, "r+b") as fh:
        fh.truncate(padded_size)

    # Two identical trees, but A's image file is truncate-padded on disk.
    tb = tmp_path / "b"
    tb.mkdir()
    (tb / "f.txt").write_text("hello")
    ib = _pack(tb, tmp_path / "b.img")

    rc = main(["diff", str(ia), str(ib)])
    assert rc == 0  # content identical -> diff(1)-style exit 0
    out = capsys.readouterr().out
    # No content differences -> no footer at all (identical images print none).
    assert "TOTAL" not in out

    # Force a content difference so the footer prints and the three bases can
    # be compared in one place.
    (tb / "f.txt").write_text("hello world")
    _pack(tb, ib)
    rc = main(["diff", str(ia), str(ib)])
    assert rc == 1
    out = capsys.readouterr().out
    lines = out.splitlines()
    image_line = next(ln for ln in lines if ln.startswith("image:"))
    disk_line = next(ln for ln in lines if ln.startswith("disk:"))
    size_line = next(ln for ln in lines if ln.startswith("size:"))

    # A was padded: its disk size must exceed its romfs full_size.
    a_full = int(image_line.split("A=")[1].split("B")[0])
    a_disk = int(disk_line.split("A=")[1].split("B")[0])
    assert a_disk == padded_size
    assert a_disk > a_full
    # B was not padded: disk == full_size.
    b_full = int(image_line.split("B=")[1].split("B")[0])
    b_disk = int(disk_line.split("B=")[1].split("B")[0])
    assert b_disk == b_full
    # Content delta is the payload growth only (+6 bytes: " world"), independent
    # of the disk/full_size gap.
    assert "size:  +6B  -0B  -> +6B" == size_line


def test_diff_volume_name(tmp_path: Path) -> None:
    ta = tmp_path / "a"
    ta.mkdir()
    (ta / "f").write_text("x")
    tb = tmp_path / "b"
    tb.mkdir()
    (tb / "f").write_text("x")
    ia = tmp_path / "a.img"
    ib = tmp_path / "b.img"
    _pack(ta, ia, vol="volA")
    _pack(tb, ib, vol="volB")
    with RomFSReader(ia) as ra, RomFSReader(ib) as rb:
        entries = diff_images(ra, rb)
    # Only the volume name differs; the file tree is identical.
    assert len(entries) == 1
    assert entries[0].status == "volume"
    assert entries[0].path == ""
    assert entries[0].target_a == "volA"
    assert entries[0].target_b == "volB"


def test_diff_added_symlink_shows_target(tmp_path: Path, capsys) -> None:
    from romfs.cli import main

    ta = tmp_path / "a"
    ta.mkdir()
    tb = tmp_path / "b"
    tb.mkdir()
    (tb / "lnk").symlink_to("target.txt")
    ia = tmp_path / "a.img"
    ib = tmp_path / "b.img"
    _pack(ta, ia)
    _pack(tb, ib)
    rc = main(["diff", str(ia), str(ib)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "lnk" in out
    # The symlink target is shown, not just its byte length.
    assert "target.txt" in out


# --- committed fixture trees ----------------------------------------------


_FIXTURES = Path(__file__).parent / "fixtures"


def test_fixtures_diff_cli(tmp_path: Path, capsys) -> None:
    """Pack the committed fixture trees and diff them via the real CLI.

    Exercises every diff status (volume / added / removed / changed-size /
    same-size-content / type-changed / symlink-target) against stable,
    committed fixtures rather than synthetic tmp_path trees, and confirms the
    exec bit and the old/new banner survive the round trip.
    """
    from romfs.cli import main

    old_img = tmp_path / "old.img"
    new_img = tmp_path / "new.img"
    RomFSWriter.from_directory(_FIXTURES / "old", volume_name="rootfs-v1").write(
        old_img
    )
    RomFSWriter.from_directory(_FIXTURES / "new", volume_name="rootfs-v2").write(
        new_img
    )

    rc = main(["diff", str(old_img), str(new_img)])
    assert rc == 1
    out = capsys.readouterr().out

    # Every diff status is represented as a symbol-led row (no banner/legend).
    lines = out.splitlines()
    assert "volume" in out
    assert any(ln.startswith("+") and "bin/newtool" in ln for ln in lines)  # added
    assert any(ln.startswith("-") and "old.conf" in ln for ln in lines)  # removed
    assert any(ln.startswith("=") and "var/run" in ln for ln in lines)  # type-changed
    assert "same size, content differs" in out
    assert "rcS -> rc.local" in out  # symlink target change
    assert "+7B" in out  # size delta (6B -> 13B)
    # The unchanged anchor must be omitted, and the exec bit surfaces.
    assert "keep.conf" not in out
    assert "exec" in out


def test_fixtures_exec_bit_round_trip(tmp_path: Path) -> None:
    """The exec bit on the fixture scripts survives a pack -> read round trip."""
    img = tmp_path / "old.img"
    RomFSWriter.from_directory(_FIXTURES / "old", volume_name="v").write(img)
    with RomFSReader(img) as r:
        assert r.find("etc/init.d/rcS").executable is True
