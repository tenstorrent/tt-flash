# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for tt_flash.boot_fs descriptor parsing.

These do not require hardware. They exercise the tt-boot-fs descriptor scanner
against the multi-table layout introduced when the mutable firmware records were
moved out of the ROM descriptor table (ROM table at 0x0, failover table at
0x4000, mutable table at 0x170000). tt-flash locates tags by scanning a
descriptor region from its start, so it must resolve a tag regardless of which
table it lives in, and must stop at a table's terminating (invalid) descriptor.
"""

import ctypes

from tt_flash import boot_fs
from tt_flash.boot_fs import tt_boot_fs_fd

FD_SIZE = ctypes.sizeof(tt_boot_fs_fd)


def make_fd(
    tag: str, spi_addr: int = 0, image_size: int = 0, executable: bool = False
) -> bytes:
    """Build a single valid tt-boot-fs file descriptor for the given tag."""
    fd = tt_boot_fs_fd()
    fd.spi_addr = spi_addr
    fd.flags.f.image_size = image_size
    fd.flags.f.invalid = 0
    fd.flags.f.executable = 1 if executable else 0
    encoded = tag.encode("ascii")
    assert len(encoded) <= boot_fs.IMAGE_TAG_SIZE
    for i, ch in enumerate(encoded):
        fd.image_tag[i] = ch
    return bytes(fd)


def make_table(entries: list[tuple[str, int]]) -> bytes:
    """Build a descriptor region: valid FDs followed by an invalid terminator."""
    blob = b"".join(make_fd(tag, spi_addr=addr) for tag, addr in entries)
    # A descriptor with all bits set has invalid == 1 and terminates the table.
    blob += b"\xff" * FD_SIZE
    return blob


def reader_for(buf: bytes):
    return lambda addr, size: buf[addr : addr + size]


def test_image_tag_str_stops_at_nul():
    """Tags shorter than 8 bytes are NUL-padded and must not include padding."""
    fd = tt_boot_fs_fd.from_buffer_copy(make_fd("cmfw"))
    assert fd.image_tag_str() == "cmfw"

    fd8 = tt_boot_fs_fd.from_buffer_copy(make_fd("boardcfg"))
    assert fd8.image_tag_str() == "boardcfg"


def test_read_tag_finds_short_and_full_length_tags():
    rom = make_table([("cmfw", 0x14000), ("recovery", 0x34000), ("boardcfg", 0xD4000)])
    reader = reader_for(rom)

    for tag, expected_addr in (
        ("cmfw", 0x14000),
        ("recovery", 0x34000),
        ("boardcfg", 0xD4000),
    ):
        found = boot_fs.read_tag(reader, tag)
        assert found is not None, f"read_tag failed to find {tag!r}"
        addr, fd = found
        assert fd.spi_addr == expected_addr


def test_read_tag_stops_at_table_terminator():
    """A tag beyond the terminating descriptor must not be found."""
    rom = make_table([("cmfw", 0x14000), ("boardcfg", 0xD4000)])
    # Simulate a following table (as on flash) that must not be reached by a
    # scan that starts in the ROM table.
    mutable = make_table([("mainimg", 0x29E000)])
    combined = rom + mutable

    reader = reader_for(combined)
    assert boot_fs.read_tag(reader, "boardcfg") is not None
    assert boot_fs.read_tag(reader, "mainimg") is None


def test_read_tag_resolves_within_each_table():
    """
    ROM tags resolve when scanning the ROM region; mutable tags (ccfgovra,
    ccfgovrb, mainimg) resolve when scanning the mutable region. This mirrors
    how blackhole.py scans each FlashWrite buffer independently.
    """
    rom = make_table([("cmfw", 0x14000), ("recovery", 0x34000), ("boardcfg", 0xD4000)])
    mutable = make_table(
        [("ccfgovra", 0x1F5000), ("ccfgovrb", 0x1F6000), ("mainimg", 0x29E000)]
    )

    rom_reader = reader_for(rom)
    mut_reader = reader_for(mutable)

    assert boot_fs.read_tag(rom_reader, "boardcfg") is not None
    assert boot_fs.read_tag(rom_reader, "ccfgovra") is None

    for tag in ("ccfgovra", "ccfgovrb", "mainimg"):
        assert boot_fs.read_tag(mut_reader, tag) is not None
    assert boot_fs.read_tag(mut_reader, "boardcfg") is None


def make_header(num_tables: int, version: int = boot_fs.BOOT_FS_HEADER_VERSION,
                magic: int = boot_fs.BOOT_FS_HEADER_MAGIC) -> bytes:
    header = boot_fs.tt_boot_fs_header()
    header.magic = magic
    header.version = version
    header.num_tables = num_tables
    return bytes(header)


def build_multi_table_flash() -> bytearray:
    """Full-flash buffer with the new layout: ROM table at 0x0, failover table
    at 0x4000, mutable table at 0x170000, TTBF header at 0x120000."""
    flash = bytearray(b"\xff" * 0x200000)
    rom = make_table([("cmfw", 0x14000), ("safeimg", 0x34000)])
    failover = make_table([("failover", 0xB4000)])
    mutable = make_table([("cmfwcfg", 0x1F7000), ("mainimg", 0x29E000)])
    flash[0x0 : len(rom)] = rom
    flash[0x4000 : 0x4000 + len(failover)] = failover
    flash[0x170000 : 0x170000 + len(mutable)] = mutable

    table_addrs = b"".join(a.to_bytes(4, "little") for a in (0x0, 0x4000, 0x170000))
    header = make_header(num_tables=3) + table_addrs
    flash[boot_fs.TT_BOOT_FS_HEADER_ADDR : boot_fs.TT_BOOT_FS_HEADER_ADDR + len(header)] = header
    return flash


def test_read_tag_multi_table_layout():
    """With a valid TTBF header, tags resolve from every advertised table,
    including the mutable table at 0x170000."""
    reader = reader_for(bytes(build_multi_table_flash()))

    for tag, expected_addr in (
        ("cmfw", 0x14000),
        ("safeimg", 0x34000),
        ("failover", 0xB4000),
        ("cmfwcfg", 0x1F7000),
        ("mainimg", 0x29E000),
    ):
        found = boot_fs.read_tag(reader, tag)
        assert found is not None, f"read_tag failed to find {tag!r}"
        assert found[1].spi_addr == expected_addr


def test_read_tag_legacy_layout_no_header():
    """Without a TTBF header (legacy flash), the fixed 0x0/0x4000 tables are
    scanned, so the failover tag still resolves."""
    flash = bytearray(b"\xff" * 0x200000)
    rom = make_table([("cmfw", 0x14000)])
    failover = make_table([("failover", 0xB4000)])
    flash[0x0 : len(rom)] = rom
    flash[0x4000 : 0x4000 + len(failover)] = failover

    reader = reader_for(bytes(flash))
    assert boot_fs.read_tag(reader, "cmfw") is not None
    assert boot_fs.read_tag(reader, "failover") is not None


def test_read_tag_rejects_unknown_header_version():
    """A TTBF header with an unsupported version must not be guessed at."""
    flash = build_multi_table_flash()
    bad = make_header(num_tables=3, version=boot_fs.BOOT_FS_HEADER_VERSION + 1)
    flash[boot_fs.TT_BOOT_FS_HEADER_ADDR : boot_fs.TT_BOOT_FS_HEADER_ADDR + len(bad)] = bad

    reader = reader_for(bytes(flash))
    assert boot_fs.find_descriptor_tables(reader) == []
    assert boot_fs.read_tag(reader, "cmfw") is None


def test_read_tag_bounded_on_missing_terminator():
    """A corrupt table with no terminating descriptor must not scan forever;
    the per-table FD cap bounds the walk."""
    # Every FD slot holds a valid-looking descriptor with a non-matching tag.
    flash = bytes(make_fd("other")) * (boot_fs.MAX_FDS_PER_TABLE * 4)
    assert boot_fs.read_tag(reader_for(flash), "cmfw") is None


def test_read_tag_matches_blank_failover_by_address():
    """
    The failover descriptor at TT_BOOT_FS_FAILOVER_HEAD_ADDR has a blank
    on-disk image_tag (the SMC ROM identifies it by fixed address, not tag).
    read_tag(reader, "failover") must still resolve it when scanning the
    failover table.
    """
    flash = bytearray(b"\xff" * 0x200000)
    rom = make_table([("cmfw", 0x14000)])
    # Failover FD with a blank tag but with the executable bit set, matching
    # what tt_boot_fs.py mkfs now emits.
    failover_fd = make_fd("", spi_addr=0xB4000, executable=True)
    flash[0x0 : len(rom)] = rom
    flash[0x4000 : 0x4000 + len(failover_fd)] = failover_fd

    reader = reader_for(bytes(flash))
    found = boot_fs.read_tag(reader, "failover")
    assert found is not None
    addr, fd = found
    assert addr == boot_fs.TT_BOOT_FS_FAILOVER_HEAD_ADDR
    assert fd.spi_addr == 0xB4000


def test_read_tag_ignores_blank_non_failover_slot():
    """A blank-tag descriptor NOT at the failover address is not the failover."""
    # Put a blank executable FD at the ROM table start (not the failover
    # address). read_tag("failover") must return None -- the blank-tag rule
    # only applies at TT_BOOT_FS_FAILOVER_HEAD_ADDR.
    flash = bytearray(b"\xff" * 0x200000)
    stray = make_fd("", spi_addr=0x14000, executable=True)
    flash[0x0 : len(stray)] = stray

    reader = reader_for(bytes(flash))
    assert boot_fs.read_tag(reader, "failover") is None


def test_read_tag_matches_non_executable_failover_slot():
    """
    A blank-tag descriptor at the failover address is the failover slot even
    when it is not marked executable. That bit says whether the ROM would boot
    the slot, which is a question about it rather than a test of whether this
    is it, and a caller that cannot find the slot cannot compare it against
    the bundle's copy either.
    """
    flash = bytearray(b"\xff" * 0x200000)
    rom = make_table([("cmfw", 0x14000)])
    non_exec = make_fd("", spi_addr=0xB4000, executable=False)
    flash[0x0 : len(rom)] = rom
    flash[0x4000 : 0x4000 + len(non_exec)] = non_exec

    reader = reader_for(bytes(flash))
    found = boot_fs.read_tag(reader, "failover")
    assert found is not None
    addr, fd = found
    assert addr == boot_fs.TT_BOOT_FS_FAILOVER_HEAD_ADDR
    assert fd.spi_addr == 0xB4000
    assert not fd.flags.f.executable


def test_read_tag_scans_a_full_rom_table():
    """
    A pre-split flash keeps every image in the table at 0x0, which has room for
    far more descriptors than the firmware's per-table default. Bounding the
    scan by that default hides the tail of such a table, and the caller sees a
    missing tag rather than a bounds problem.
    """
    entries = [(f"img{i:04}", 0x14000 + i * 0x1000) for i in range(64)]
    assert len(entries) > boot_fs.MAX_FDS_PER_TABLE

    flash = bytearray(b"\xff" * 0x200000)
    rom = make_table(entries)
    assert len(rom) <= boot_fs.TT_BOOT_FS_SECURITY_BINARY_FD_ADDR
    flash[0 : len(rom)] = rom

    reader = reader_for(bytes(flash))
    last_tag, last_addr = entries[-1]
    found = boot_fs.read_tag(reader, last_tag)

    assert found is not None, "descriptor past the per-table default was missed"
    assert found[1].spi_addr == last_addr


def test_table_fd_capacity_is_bounded_by_the_next_region():
    """Fixed tables end where the next structure in flash begins."""
    fd_size = ctypes.sizeof(tt_boot_fs_fd)
    assert boot_fs.table_fd_capacity(boot_fs.TT_BOOT_FS_FD_HEAD_ADDR) == (
        boot_fs.TT_BOOT_FS_SECURITY_BINARY_FD_ADDR // fd_size
    )
    # A table reached through the multi-table header has no known extent, so it
    # keeps the firmware default as a corruption backstop.
    assert boot_fs.table_fd_capacity(0x170000) == boot_fs.MAX_FDS_PER_TABLE
