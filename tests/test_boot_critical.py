# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for skip_boot_critical. These do not require hardware: a small
in-memory flash and a fake chip stand in for a real BH device.

Usage:
    pytest tests/test_boot_critical.py -v
"""

import ctypes
import struct
from typing import Optional

from tt_flash import boot_fs
from tt_flash.blackhole import (
    BOOT_CRITICAL_TAGS,
    MCUBOOT_IMAGE_MAGIC,
    MCUBOOT_TLV_INFO,
    MCUBOOT_TLV_KEYHASH,
    MCUBOOT_TLV_SHA256,
    FlashWrite,
    calculate_checksum,
    _find_fd_in_writes,
    boot_image_identity,
    skip_boot_critical,
    skip_unchanged_static_regions,
)

MCUBOOT_HDR_SIZE = 32
TLV_INFO_MAGIC = 0x6907
TLV_RSA2048_PSS = 0x20

FD_SIZE = ctypes.sizeof(boot_fs.tt_boot_fs_fd)
INVALID_FD = b"\xff" * FD_SIZE

# Fixed flash layout used by these tests. cmfw/safeimg/safetail live in the ROM
# table at 0x0; failover lives in the failover table at 0x4000.
ROM_HEAD = boot_fs.TT_BOOT_FS_FD_HEAD_ADDR
FAILOVER_HEAD = boot_fs.TT_BOOT_FS_FAILOVER_HEAD_ADDR

IMAGE_ADDRS = {
    "cmfw": 0x14000,
    "safeimg": 0x34000,
    "safetail": 0xB3000,
    "failover": 0xB4000,
}

# The SPI RX training word the bootfs carries at 0x13FFC, directly ahead of
# MCUBoot at 0x14000. The bundle emits contiguous flash as a single write, so
# these two arrive merged and cmfw's body does not start at its descriptor's
# spi_addr. Modelled here because a fixture that gives cmfw its own write hides
# that, and with it the only case where the two differ.
SPI_RX_ADDR = 0x13FFC
SPI_RX_VALUE = (0xA5A55A5A).to_bytes(4, "little")


class FakeChip:
    """Minimal stand-in exposing just spi_read over an in-memory flash."""

    def __init__(self, flash: bytes):
        self.flash = bytearray(flash)

    def spi_read(self, addr: int, size: int) -> bytes:
        return bytes(self.flash[addr : addr + size])

    def apply(self, writes: list[FlashWrite]) -> None:
        """Carry out a write plan, so the resulting flash can be inspected."""
        for write in writes:
            self.flash[write.offset : write.offset + len(write.write)] = write.write


def make_fd(
    tag: str,
    spi_addr: int,
    data: bytes,
    executable: bool = False,
    copy_dest: int = 0,
) -> bytes:
    fd = boot_fs.tt_boot_fs_fd()
    fd.spi_addr = spi_addr
    fd.copy_dest = copy_dest
    fd.flags.f.image_size = len(data)
    fd.flags.f.invalid = 0
    fd.flags.f.executable = 1 if executable else 0
    fd.data_crc = calculate_checksum(data)
    for i, b in enumerate(tag.encode()):
        fd.image_tag[i] = b
    return bytes(fd)


def make_failover_fd(data: bytes, executable: bool = True) -> bytes:
    """
    A failover descriptor as tt_boot_fs.py mkfs actually emits it: at the fixed
    failover address with a blank image_tag (the SMC ROM identifies the slot by
    address, not tag), and by default marked executable so the ROM will jump
    to it.
    """
    return make_fd("", IMAGE_ADDRS["failover"], data, executable=executable)


def body_for(tag: str) -> bytes:
    # 16 bytes of a tag-derived pattern; length is a multiple of 4 so the
    # additive checksum covers all of it.
    return bytes([sum(tag.encode()) & 0xFF]) * 16


def bodies_for(override: Optional[dict] = None) -> dict:
    """Image bodies keyed by tag, with any given tags substituted."""
    bodies = {tag: body_for(tag) for tag in IMAGE_ADDRS}
    bodies.update(override or {})
    return bodies


def signed_image(
    payload: bytes,
    signature: bytes,
    sha256: bytes = b"\xa5" * 32,
    keyhash: bytes = b"\x5a" * 32,
) -> bytes:
    """
    An image shaped like one imgtool produces: a header, the payload, and a TLV
    area carrying the payload hash, the signing key's hash, and the signature.
    """
    header = struct.pack(
        "<IIHHI", MCUBOOT_IMAGE_MAGIC, 0, MCUBOOT_HDR_SIZE, 0, len(payload)
    )
    header += b"\x00" * (MCUBOOT_HDR_SIZE - len(header))

    def tlv(tlv_type: int, value: bytes) -> bytes:
        return struct.pack("<HH", tlv_type, len(value)) + value

    tlvs = (
        tlv(MCUBOOT_TLV_SHA256, sha256)
        + tlv(MCUBOOT_TLV_KEYHASH, keyhash)
        + tlv(TLV_RSA2048_PSS, signature)
    )
    info = struct.pack("<HH", TLV_INFO_MAGIC, MCUBOOT_TLV_INFO.size + len(tlvs))

    return header + payload + info + tlvs


def build_flash(
    corrupt: set[str] = frozenset(),
    missing: set[str] = frozenset(),
    bodies: Optional[dict] = None,
    failover_executable: bool = True,
) -> bytes:
    """
    Build an in-memory flash with a ROM table (cmfw/safeimg/safetail) at 0x0, a
    failover table at 0x4000, and each image body at its fixed address.

    corrupt: tags whose on-chip body bytes are mangled (checksum will fail).
    missing: tags to omit from the descriptor tables entirely.
    bodies: image bodies to use in place of the defaults.
    failover_executable: whether the failover descriptor is marked executable.
    """
    bodies = bodies_for(bodies)
    flash = bytearray(0xC0000)

    rom_fds = b"".join(
        make_fd(tag, IMAGE_ADDRS[tag], bodies[tag])
        for tag in ("cmfw", "safeimg", "safetail")
        if tag not in missing
    )
    flash[ROM_HEAD : ROM_HEAD + len(rom_fds)] = rom_fds
    flash[ROM_HEAD + len(rom_fds) : ROM_HEAD + len(rom_fds) + FD_SIZE] = INVALID_FD

    if "failover" not in missing:
        fo_fd = make_failover_fd(bodies["failover"], executable=failover_executable)
        flash[FAILOVER_HEAD : FAILOVER_HEAD + FD_SIZE] = fo_fd
    flash[FAILOVER_HEAD + FD_SIZE : FAILOVER_HEAD + 2 * FD_SIZE] = INVALID_FD

    for tag, addr in IMAGE_ADDRS.items():
        body = bytearray(bodies[tag])
        if tag in corrupt:
            body[0] ^= 0xFF
        flash[addr : addr + len(body)] = body

    flash[SPI_RX_ADDR : SPI_RX_ADDR + len(SPI_RX_VALUE)] = SPI_RX_VALUE

    return bytes(flash)


def build_image_writes(
    bodies: Optional[dict] = None, failover_executable: bool = True
) -> list[FlashWrite]:
    """Writes as parsed from a fresh bundle: descriptor tables plus image bodies."""
    bodies = bodies_for(bodies)
    rom_fds = b"".join(
        make_fd(tag, IMAGE_ADDRS[tag], bodies[tag])
        for tag in ("cmfw", "safeimg", "safetail")
    )
    writes = [
        FlashWrite(ROM_HEAD, bytearray(rom_fds + INVALID_FD)),
        FlashWrite(
            FAILOVER_HEAD,
            bytearray(
                make_failover_fd(bodies["failover"], executable=failover_executable)
                + INVALID_FD
            ),
        ),
        # cmfw merged with the training word that precedes it, as a bundle emits it.
        FlashWrite(SPI_RX_ADDR, bytearray(SPI_RX_VALUE + bodies["cmfw"])),
    ]
    for tag in ("safeimg", "safetail", "failover"):
        writes.append(FlashWrite(IMAGE_ADDRS[tag], bytearray(bodies[tag])))
    writes.sort(key=lambda w: w.offset)
    return writes


def is_written(writes: list[FlashWrite], addr: int) -> bool:
    """Whether any write would touch addr. Not the same as starting at it."""
    return any(w.offset <= addr < w.offset + len(w.write) for w in writes)


def fd_in_writes(writes: list[FlashWrite], tag: str) -> boot_fs.tt_boot_fs_fd:
    """The descriptor a set of writes would put on flash for tag."""
    for write in writes:
        found = boot_fs.read_tag(
            lambda addr, size: write.write[addr : addr + size], tag
        )
        if found is not None:
            return found[1]
    raise AssertionError(f"no descriptor for {tag} in writes")


def test_skip_boot_critical_drops_identical_bodies():
    chip = FakeChip(build_flash())
    writes = build_image_writes()

    result = skip_boot_critical(chip, writes)

    # Every boot-critical image body is gone (on-chip copy identical). cmfw
    # counts only if the merged write no longer reaches 0x14000.
    for tag in BOOT_CRITICAL_TAGS:
        assert not is_written(result, IMAGE_ADDRS[tag]), (
            f"body for {tag} at 0x{IMAGE_ADDRS[tag]:x} was not skipped"
        )
    # The training word shares cmfw's write but is not part of the image, so it
    # still has to be written.
    assert is_written(result, SPI_RX_ADDR), "SPI RX training word was lost"
    # The descriptor tables are still in the plan at this stage; they carry the
    # resident descriptors this function just spliced in. Dropping them when
    # they match flash is skip_unchanged_static_regions' job.
    result_offsets = {w.offset for w in result}
    assert ROM_HEAD in result_offsets
    assert FAILOVER_HEAD in result_offsets


def test_skip_boot_critical_writes_differing_image():
    # The on-chip cmfw differs from the bundle's: its body write must be kept,
    # otherwise the (always written) descriptor's data_crc would mismatch the
    # image left on flash and the boot ROM would reject it.
    chip = FakeChip(build_flash(corrupt={"cmfw"}))
    writes = build_image_writes()

    result = skip_boot_critical(chip, writes)

    assert is_written(result, IMAGE_ADDRS["cmfw"]), (
        "differing cmfw body write was skipped; descriptor and image on flash "
        "would be inconsistent"
    )
    # The identical images are still skipped.
    for tag in ("safeimg", "safetail", "failover"):
        assert not is_written(result, IMAGE_ADDRS[tag])


def test_skip_boot_critical_writes_everything_on_blank_chip():
    # An unprovisioned (fully erased) chip holds no boot-critical images:
    # every body write must be kept so the flash ends up self-consistent.
    chip = FakeChip(b"\xff" * 0xC0000)
    writes = build_image_writes()

    result = skip_boot_critical(chip, writes)

    for tag in BOOT_CRITICAL_TAGS:
        assert is_written(result, IMAGE_ADDRS[tag]), (
            f"body write for {tag} was skipped on an unprovisioned chip"
        )


def test_skip_boot_critical_ignores_a_fresh_signature():
    # The case the content comparison exists for: the bundle holds the same
    # recovery image the chip does, rebuilt and so signed again. Only the
    # signature differs, and nothing should be written.
    payload = bytes(range(64))
    resident = signed_image(payload, signature=b"\x11" * 256)
    rebuilt = signed_image(payload, signature=b"\x22" * 256)
    assert resident != rebuilt

    chip = FakeChip(build_flash(bodies={"safeimg": resident}))
    writes = build_image_writes(bodies={"safeimg": rebuilt})

    result = skip_boot_critical(chip, writes)

    assert IMAGE_ADDRS["safeimg"] not in {w.offset for w in result}, (
        "recovery image was rewritten even though the chip holds the same image"
    )
    # The descriptor left on flash has to describe the resident image, not the
    # one the bundle would have written: their data_crc values differ because
    # the signatures do.
    assert fd_in_writes(result, "safeimg").data_crc == calculate_checksum(resident)


def test_skip_boot_critical_writes_a_damaged_image():
    # A signed image damaged on flash still carries the hash of what it was
    # supposed to be, so only its descriptor can reveal the damage.
    image = signed_image(bytes(range(64)), b"\x11" * 256)

    chip = FakeChip(build_flash(bodies={"safeimg": image}))
    # Flip a byte of the signature, the part of the image the content
    # comparison deliberately ignores.
    chip.flash[IMAGE_ADDRS["safeimg"] + len(image) - 8] ^= 0xFF

    writes = build_image_writes(bodies={"safeimg": image})

    result = skip_boot_critical(chip, writes)

    assert IMAGE_ADDRS["safeimg"] in {w.offset for w in result}, (
        "damaged recovery image was left on the board"
    )


def test_skip_boot_critical_writes_a_changed_payload():
    # Same signing key, different payload: a real change, so it must be written.
    resident = signed_image(bytes(range(64)), b"\x11" * 256, sha256=b"\x01" * 32)
    updated = signed_image(bytes(range(64)), b"\x11" * 256, sha256=b"\x02" * 32)

    chip = FakeChip(build_flash(bodies={"safeimg": resident}))
    writes = build_image_writes(bodies={"safeimg": updated})

    result = skip_boot_critical(chip, writes)

    assert IMAGE_ADDRS["safeimg"] in {w.offset for w in result}
    assert fd_in_writes(result, "safeimg").data_crc == calculate_checksum(updated)


def test_skip_boot_critical_writes_a_resigned_image():
    # Same payload, different signing key. The payload hash matches, so only
    # the key hash can catch this; skipping it would leave an image signed by
    # a retired key on the board.
    payload = bytes(range(64))
    resident = signed_image(payload, b"\x11" * 256, keyhash=b"\x01" * 32)
    resigned = signed_image(payload, b"\x22" * 256, keyhash=b"\x02" * 32)

    chip = FakeChip(build_flash(bodies={"safeimg": resident}))
    writes = build_image_writes(bodies={"safeimg": resigned})

    result = skip_boot_critical(chip, writes)

    assert IMAGE_ADDRS["safeimg"] in {w.offset for w in result}


def test_update_boot_images_writes_everything():
    chip = FakeChip(build_flash())
    writes = build_image_writes()

    result = skip_boot_critical(chip, writes, update_boot_images=True)

    for tag in BOOT_CRITICAL_TAGS:
        assert is_written(result, IMAGE_ADDRS[tag])


def test_skip_boot_critical_recognises_a_non_executable_failover():
    # The failover slot is identified by its address alone. When recognising it
    # also required the executable bit, a bundle that left that bit clear went
    # unrecognised, and the whole failover image was rewritten on every update
    # with nothing said about it.
    chip = FakeChip(build_flash(failover_executable=False))
    writes = build_image_writes(failover_executable=False)

    result = skip_boot_critical(chip, writes)

    assert not is_written(result, IMAGE_ADDRS["failover"]), (
        "failover image was rewritten even though the chip holds the same one"
    )


def test_skip_boot_critical_writes_a_newly_executable_failover():
    # Same image on both sides, but the board's failover descriptor is not
    # marked executable and the bundle's is. The ROM would not boot what is on
    # the board, and identical image bytes cannot say so, so the descriptor has
    # to reach flash.
    chip = FakeChip(build_flash(failover_executable=False))

    chip.apply(full_plan(chip, build_image_writes()))

    found = boot_fs.read_tag(lambda a, s: chip.spi_read(a, s), "failover")
    assert found is not None
    assert found[1].flags.f.executable, (
        "the board was left with a failover slot the ROM will not boot"
    )


def test_skip_boot_critical_writes_a_changed_load_address():
    # The image is byte for byte what the board holds, but the bundle loads it
    # somewhere else. Nothing in the image records where it is loaded, so only
    # the descriptor can reveal the change, and reusing the resident one would
    # quietly drop it.
    chip = FakeChip(build_flash())
    writes = build_image_writes()
    rom_write = next(w for w in writes if w.offset == ROM_HEAD)
    rom_write.write[0:FD_SIZE] = make_fd(
        "cmfw", IMAGE_ADDRS["cmfw"], body_for("cmfw"), copy_dest=0x20000000
    )

    chip.apply(full_plan(chip, writes))

    found = boot_fs.read_tag(lambda a, s: chip.spi_read(a, s), "cmfw")
    assert found is not None
    assert found[1].copy_dest == 0x20000000, (
        "the bundle's load address was dropped in favour of the resident one"
    )


def test_skip_boot_critical_writes_a_relocated_image():
    # The bundle moves cmfw somewhere else. The resident descriptor cannot be
    # reused, because it points at the old address, so the image is written in
    # full at its new home.
    chip = FakeChip(build_flash())
    writes = build_image_writes()
    moved = 0x18000
    # At its new home cmfw no longer abuts the training word, so it gets a
    # write of its own.
    writes = [w for w in writes if w.offset != SPI_RX_ADDR]
    writes.append(FlashWrite(SPI_RX_ADDR, bytearray(SPI_RX_VALUE)))
    writes.append(FlashWrite(moved, bytearray(body_for("cmfw"))))
    rom_write = next(w for w in writes if w.offset == ROM_HEAD)
    rom_write.write[0:FD_SIZE] = make_fd("cmfw", moved, body_for("cmfw"))

    result = skip_boot_critical(chip, writes)

    assert is_written(result, moved)


def full_plan(chip, writes, update_boot_images: bool = False) -> list[FlashWrite]:
    """The write plan as boot_fs_write leaves it, for the boot-critical stages."""
    writes = skip_boot_critical(chip, writes, update_boot_images)
    return skip_unchanged_static_regions(chip, writes, update_boot_images)


def test_static_tables_not_written_when_unchanged():
    # The guarantee the static/mutable table split exists for: an update that
    # changes nothing boot-critical must not write the ROM or failover tables,
    # because writing them means erasing their sectors.
    chip = FakeChip(build_flash())

    result = full_plan(chip, build_image_writes())

    result_offsets = {w.offset for w in result}
    assert ROM_HEAD not in result_offsets, "ROM descriptor table was rewritten"
    assert FAILOVER_HEAD not in result_offsets, "failover table was rewritten"


def test_mcuboot_not_written_when_unchanged():
    # cmfw is the case a fixture without the training word cannot see.
    chip = FakeChip(build_flash())

    result = full_plan(chip, build_image_writes())

    assert not is_written(result, IMAGE_ADDRS["cmfw"]), (
        "MCUBoot was rewritten even though the board holds the same image"
    )


def test_rom_table_written_when_a_boot_image_changed():
    # A genuinely different cmfw: its descriptor records a data_crc over the new
    # bytes, so it differs from the one on flash and the table has to be written
    # alongside the image.
    chip = FakeChip(build_flash())
    writes = build_image_writes(bodies={"cmfw": b"\x77" * 16})

    result = full_plan(chip, writes)

    assert ROM_HEAD in {w.offset for w in result}, (
        "ROM table was skipped even though cmfw's descriptor changed"
    )
    assert is_written(result, IMAGE_ADDRS["cmfw"])


def test_plan_leaves_flash_self_consistent():
    """
    The invariant that matters, whatever gets skipped: every boot-critical
    descriptor on flash must describe the image actually on flash. A skipped
    table write is only correct because flash already says the right thing, so
    check the end state rather than the plan.

    Covers a board that already matches, one whose image is damaged under an
    intact descriptor, one the bundle genuinely changes, and a blank board.
    """
    scenarios = {
        "identical": (build_flash(), None),
        "damaged": (build_flash(corrupt={"cmfw"}), None),
        "changed": (build_flash(), {"cmfw": b"\x77" * 16}),
        "blank": (b"\xff" * 0xC0000, None),
    }

    for name, (flash, bundle_bodies) in scenarios.items():
        chip = FakeChip(flash)
        chip.apply(full_plan(chip, build_image_writes(bodies=bundle_bodies)))

        for tag in BOOT_CRITICAL_TAGS:
            found = boot_fs.read_tag(lambda a, s: chip.spi_read(a, s), tag)
            assert found is not None, f"[{name}] no descriptor for {tag} on flash"
            fd = found[1]
            body = chip.spi_read(fd.spi_addr, fd.flags.f.image_size)
            assert calculate_checksum(body) == fd.data_crc, (
                f"[{name}] {tag}: descriptor at 0x{fd.spi_addr:x} does not "
                "describe the image on flash; the boot ROM would reject it"
            )


def test_static_tables_written_on_blank_chip():
    chip = FakeChip(b"\xff" * 0xC0000)

    result = full_plan(chip, build_image_writes())

    result_offsets = {w.offset for w in result}
    assert ROM_HEAD in result_offsets
    assert FAILOVER_HEAD in result_offsets


def test_update_boot_images_writes_static_tables():
    chip = FakeChip(build_flash())

    result = full_plan(chip, build_image_writes(), update_boot_images=True)

    result_offsets = {w.offset for w in result}
    assert ROM_HEAD in result_offsets
    assert FAILOVER_HEAD in result_offsets


def test_skip_boot_critical_ignores_tags_absent_from_image():
    # A bundle that ships no boot-critical tags must be a no-op, not an error.
    chip = FakeChip(build_flash())
    writes = [FlashWrite(0x170000, bytearray(body_for("mainimg")))]

    result = skip_boot_critical(chip, writes)

    assert [w.offset for w in result] == [0x170000]


def test_boot_image_identity_terminates_on_a_zero_size_tlv_section():
    # A TLV info header claiming a total size that cannot hold a TLV leaves the
    # section walk where it started. Taken at face value the outer loop re-reads
    # the same header forever, hanging tt-flash mid-flash behind the spinner.
    payload = body_for("cmfw")
    header = struct.pack(
        "<IIHHI", MCUBOOT_IMAGE_MAGIC, 0, MCUBOOT_HDR_SIZE, 0, len(payload)
    )
    header += b"\x00" * (MCUBOOT_HDR_SIZE - len(header))

    for claimed in (0, 1, 2, 3, MCUBOOT_TLV_INFO.size):
        image = header + payload + struct.pack("<HH", TLV_INFO_MAGIC, claimed)
        # No sha256 is reachable, so the image identifies as its own bytes.
        assert boot_image_identity(image) == image


def test_failover_protected_when_its_table_shares_a_write():
    # The bundle emits each contiguous run of flash as one write, so the
    # failover table is not guaranteed to start one. Matching it by write offset
    # alone stops protecting the failover image the moment it doesn't, silently
    # reopening the power-loss window this whole path exists to close.
    chip = FakeChip(build_flash())
    writes = build_image_writes()

    failover_table = next(w for w in writes if w.offset == FAILOVER_HEAD)
    writes.remove(failover_table)
    # Prepend the security binary descriptor slot that sits right before it.
    pre_addr = boot_fs.TT_BOOT_FS_SECURITY_BINARY_FD_ADDR
    pre = bytearray(chip.spi_read(pre_addr, FAILOVER_HEAD - pre_addr))
    writes.append(FlashWrite(pre_addr, pre + failover_table.write))
    writes.sort(key=lambda w: w.offset)

    result = full_plan(chip, writes)

    assert not is_written(result, IMAGE_ADDRS["failover"]), "failover body rewritten"
    assert not is_written(result, FAILOVER_HEAD), "failover table rewritten"


def test_failover_lookup_ignores_a_zeroed_run_at_the_wrong_address():
    # read_tag's addresses are buffer-relative for a write, so its blank-tag
    # rule keys on the wrong bytes for any write not starting at flash 0. A
    # zeroed run landing at that buffer offset must not be taken for the
    # failover descriptor.
    writes = build_image_writes()
    stray = FlashWrite(0x100, bytearray(FAILOVER_HEAD + FD_SIZE))

    found = _find_fd_in_writes(writes + [stray], "failover")

    assert found is not None
    write, offset, fd = found
    assert write.offset + offset == FAILOVER_HEAD
    assert fd.spi_addr == IMAGE_ADDRS["failover"]
