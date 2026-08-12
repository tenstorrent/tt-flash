# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Blackhole SPI write tests driven against a shadow copy of the flash layout.

These run the production flash path -- ``flash_chip_stage1`` then
``flash_chip_stage2`` -- against a scratch window well above the firmware
region, so every test performs real erase and program cycles and verifies by
reading back from SPI.  The firmware region is only ever read.

Why this is needed: re-flashing the same bundle is content-idempotent by
construction (``writeback_boardcfg`` reads boardcfg off the chip and writes it
straight back), and the SMC firmware's SpiSmartWrite is skip-if-identical, so a
same-version reflash issues no erases and no programs at all.  A test built on
that pattern passes even if erase and program are broken outright.  Seeding the
shadow with known-wrong state first is what makes the write observable.

Usage:
    pytest test_shadow_flash.py --fwbundle=/path/to/bundle.fwbundle \
        [--fwbundle-prev=/path/to/older.fwbundle]
"""

import pytest

import bh_image as bi
from bh_shadow import FD_SIZE, SECTOR, fill, get_board_name
from tt_flash import boot_fs
from tt_flash.blackhole import CCFGOVR_TAGS, calculate_checksum
from tt_flash.flash import (
    FlashStageResultState,
    flash_chip_stage1,
    flash_chip_stage2,
    verify_package,
)
from tt_flash.main import load_manifest


def bank_payload(seed: int, size: int) -> bytes:
    return bytes((seed * 7 + i * 31) & 0xFF for i in range(size))


def shipped_banks(image):
    """The ccfgovr banks an image carries, as (tag, spi_addr, body length).

    Bundles only started shipping these tags partway through the 19.x series,
    so callers skip when the list comes back empty.
    """
    found = []
    for tag in CCFGOVR_TAGS:
        located = bi.find_tag(image, tag)
        if located is not None:
            found.append((tag, located[2].spi_addr, located[2].flags.f.image_size))
    return found


def seed_banks(shadow, banks) -> dict[int, bytes]:
    """Put distinctive content in the shadow's ccfgovr banks.

    Stands in for banks that tt-mod rewrote in the field: the firmware
    validates them by a checksum in the bank header, not by the descriptor's
    data_crc, so a flash must leave the bodies alone.
    """
    on_chip = {}
    for index, (_, addr, size) in enumerate(banks):
        on_chip[addr] = bank_payload(100 + index, size)
        shadow.spi_write(addr, on_chip[addr])
    return on_chip


def stage1(chip, bundle_path, boardname, *, allow_major_downgrades=False):
    """Compute the writes for a flash, reading existing state off `chip`."""
    tar, version = load_manifest(bundle_path)
    try:
        manifest = verify_package(tar, version)
        messages: list[str] = []
        result = flash_chip_stage1(
            chip,
            boardname,
            manifest,
            tar,
            messages,
            True,  # force: stage 1's version gate reads the real chip, not the
            # shadow, because get_bundle_version goes through pyluwen
            allow_major_downgrades,
        )
    finally:
        tar.close()
    assert (
        result.state == FlashStageResultState.Ok
    ), f"stage 1 declined to flash: {result.state} {result.msg}"
    return result.data, messages


def flash_into_shadow(shadow, bundle_path, boardname, *, seed=None, **kwargs):
    """Run a full flash against the shadow, optionally seeding it first.

    Stage 1 runs *before* seeding on purpose: it reads the shadow's descriptor
    table and boardcfg body, so scribbling over them first would just make it
    fail.  Seeding between the stages means every byte stage 2 writes was the
    seed value immediately beforehand.
    """
    data, messages = stage1(shadow, bundle_path, boardname, **kwargs)
    if seed is not None:
        fill(shadow, data.write, seed)
    assert (
        flash_chip_stage2(shadow, data, messages) is not None
    ), "stage 2 failed:\n" + "\n".join(messages)
    return data


def read_back(shadow, writes) -> list[bytes]:
    return [shadow.spi_read(w.offset, len(w.write)) for w in writes]


def assert_flashed(shadow, writes) -> list[bytes]:
    """Every write must be readable back from SPI byte for byte."""
    got = read_back(shadow, writes)
    for write, actual in zip(writes, got):
        assert actual == bytes(write.write), (
            f"readback mismatch at 0x{write.offset:x}: "
            f"{len(write.write)} bytes intended, first differing byte at "
            f"{next(i for i, (a, b) in enumerate(zip(actual, write.write)) if a != b)}"
        )
    return got


def chip_tag(chip, tag):
    """Locate a tag in a chip's live descriptor table."""
    return boot_fs.read_tag(lambda addr, size: chip.spi_read(addr, size), tag)


def seed_boardcfg(shadow) -> bytes:
    """Put a distinctive boardcfg in the shadow and reseal its descriptor.

    Stands in for the per-board identity data that a flash must preserve. The
    descriptor's data_crc is recomputed so the seeded state is self-consistent,
    which lets the test assert that the merge carried the metadata across too.
    """
    found = chip_tag(shadow, "boardcfg")
    assert found is not None, "shadow has no boardcfg descriptor"
    offset, fd = found

    marker = bytes((0xA5 ^ (i * 13)) & 0xFF for i in range(fd.flags.f.image_size))
    shadow.spi_write(fd.spi_addr, marker)

    fd.data_crc = calculate_checksum(marker)
    table = bytearray(shadow.spi_read(0, offset + FD_SIZE))
    bi.store_fd(table, offset, fd)
    shadow.spi_write(0, bytes(table))
    return marker


def repack(tmp_path, name, source_bundle, boardname, writes) -> str:
    """Write mutated writes back out as a fwbundle stage 1 can consume."""
    path = str(tmp_path / f"{name}-{boardname}.fwbundle")
    return bi.write_fwbundle(
        path,
        boardname,
        writes,
        bi.read_mask(source_bundle, boardname),
        bi.read_manifest(source_bundle),
    )


@pytest.mark.requires_hardware
@pytest.mark.shadow_flash
class TestShadowFlash:
    def test_flash_erases_and_programs(self, bh_chips, fwbundle_path, make_shadow):
        """
        A flash must actually erase and program, not just appear to.

        The shadow is filled with 0x00 immediately before stage 2 runs. NOR
        programming can only clear bits, so a byte reading back as anything
        non-zero cannot have got there without a real sector erase. That is the
        assertion the existing same-version reflash test cannot make.
        """
        for chip in bh_chips:
            boardname = get_board_name(chip)
            image = bi.load_image(fwbundle_path, boardname)
            shadow = make_shadow(chip, image)

            data = flash_into_shadow(shadow, fwbundle_path, boardname, seed=0x00)
            got = assert_flashed(shadow, data.write)

            assert any(
                byte for chunk in got for byte in chunk
            ), "everything read back as zero, so no erase can have happened"

    def test_reflash_is_idempotent(self, bh_chips, fwbundle_path, make_shadow):
        """
        Re-flashing identical content leaves the result correct.

        This is the shape of the pre-existing test_flash_preserves_board_id, and
        it passes here for the same reason it passes there: SpiSmartWrite
        memcmps each sector and skips it when it already matches, so the second
        flash touches the flash device not at all. Kept as an executable record
        of why that pattern alone proves nothing.
        """
        for chip in bh_chips:
            boardname = get_board_name(chip)
            image = bi.load_image(fwbundle_path, boardname)
            shadow = make_shadow(chip, image)

            flash_into_shadow(shadow, fwbundle_path, boardname, seed=0x00)
            data = flash_into_shadow(shadow, fwbundle_path, boardname)
            assert_flashed(shadow, data.write)

    def test_boardcfg_survives_flash(self, bh_chips, fwbundle_path, make_shadow):
        """
        Per-board identity data survives a genuine erase and program.

        The image ships placeholder boardcfg; writeback_boardcfg must replace it
        with what is already on the chip and repoint the descriptor. Here the
        image has its own body write at the descriptor's address, so the handler
        takes its overwrite branch.
        """
        for chip in bh_chips:
            boardname = get_board_name(chip)
            image = bi.load_image(fwbundle_path, boardname)
            shadow = make_shadow(chip, image)

            image_fd = bi.find_tag(image, "boardcfg")[2]
            assert (
                bi.body_write(image, image_fd) is not None
            ), "expected the image to carry a boardcfg body write"

            marker = seed_boardcfg(shadow)
            data = flash_into_shadow(shadow, fwbundle_path, boardname)
            assert_flashed(shadow, data.write)

            assert (
                shadow.spi_read(image_fd.spi_addr, len(marker)) == marker
            ), "the chip's boardcfg was replaced by the image's placeholder"

            _, fd = chip_tag(shadow, "boardcfg")
            assert fd.spi_addr == image_fd.spi_addr
            assert fd.flags.f.image_size == len(marker)
            assert fd.data_crc == calculate_checksum(marker), "stale data_crc"
            assert (
                calculate_checksum(bytes(fd)[:-4]) == fd.fd_crc
            ), "descriptor checksum does not validate"

    def test_boardcfg_appended_when_image_has_no_body(
        self, bh_chips, fwbundle_path, make_shadow, tmp_path
    ):
        """
        writeback_boardcfg's other branch: no body write to overwrite.

        Dropping the boardcfg section leaves its descriptor pointing at an
        address no write starts, so the handler must append one. This is the
        shape 18.x images have naturally, reproduced here inside the small
        window.
        """
        for chip in bh_chips:
            boardname = get_board_name(chip)
            image = bi.load_image(fwbundle_path, boardname)
            image_fd = bi.find_tag(image, "boardcfg")[2]

            mutated = bi.drop_section(image, image_fd.spi_addr)
            assert bi.body_write(mutated, image_fd) is None
            bundle = repack(
                tmp_path, "no-boardcfg-body", fwbundle_path, boardname, mutated
            )

            shadow = make_shadow(chip, mutated)
            marker = seed_boardcfg(shadow)
            data = flash_into_shadow(shadow, bundle, boardname)

            assert any(
                w.offset == image_fd.spi_addr for w in data.write
            ), "handler did not append a boardcfg write"
            assert_flashed(shadow, data.write)
            assert shadow.spi_read(image_fd.spi_addr, len(marker)) == marker

    def test_ccfgovr_banks_preserved(self, bh_chips, fwbundle_path, make_shadow):
        """
        On-chip ccfgovr banks survive a flash that ships its own copies.

        The banks are field-updated by tt-mod, which cannot fix up the
        descriptor's data_crc, so the firmware validates them by a checksum in
        the bank header instead. tt-flash mirrors that by writing the descriptor
        but dropping the body.
        """
        for chip in bh_chips:
            boardname = get_board_name(chip)
            image = bi.load_image(fwbundle_path, boardname)
            banks = shipped_banks(image)
            if not banks:
                pytest.skip(f"{fwbundle_path} ships no ccfgovr banks for {boardname}")

            shadow = make_shadow(chip, image)
            on_chip = seed_banks(shadow, banks)
            data = flash_into_shadow(shadow, fwbundle_path, boardname)

            for tag, addr, size in banks:
                assert not any(
                    w.offset == addr for w in data.write
                ), f"{tag} body write was not dropped"
                assert (
                    shadow.spi_read(addr, size) == on_chip[addr]
                ), f"{tag} bank was overwritten by the image"

                found = chip_tag(shadow, tag)
                assert found is not None, f"{tag} descriptor was not written"
                assert found[1].spi_addr == addr

    @pytest.mark.xfail(
        strict=True,
        reason="skip_ccfgovr matches bodies by exact write offset, so a bank "
        "embedded inside a larger write is not dropped and the on-chip "
        "override is clobbered",
    )
    def test_embedded_ccfgovr_banks_preserved(
        self, bh_chips, fwbundle_path, make_shadow, tmp_path
    ):
        """
        The same preservation guarantee, when the bodies are not their own writes.

        Both tag handlers locate a body by `write.offset == fd.spi_addr`, an
        exact match. Real images already embed partitions inside larger sections
        -- mainimg lives inside the write at 0x29d000 -- so an image generator
        that emitted the ccfgovr banks that way would silently defeat the
        preservation. This takes the banks the bundle really ships and folds
        them into one larger write, leaving contents and addresses untouched.

        Marked strict xfail: if it starts passing, the exact match has been
        fixed and the marker should come off.
        """
        for chip in bh_chips:
            boardname = get_board_name(chip)
            image = bi.load_image(fwbundle_path, boardname)
            banks = shipped_banks(image)
            if not banks:
                pytest.skip(f"{fwbundle_path} ships no ccfgovr banks for {boardname}")

            addrs = [addr for _, addr, _ in banks]
            embedded = bi.embed_sections(image, addrs, min(addrs) - SECTOR)
            for _, addr, _ in banks:
                assert not any(w.offset == addr for w in embedded)
            bundle = repack(
                tmp_path, "ccfgovr-embedded", fwbundle_path, boardname, embedded
            )

            shadow = make_shadow(chip, embedded)
            on_chip = seed_banks(shadow, banks)
            flash_into_shadow(shadow, bundle, boardname)

            for tag, addr, size in banks:
                assert (
                    shadow.spi_read(addr, size) == on_chip[addr]
                ), f"{tag} bank was overwritten by the image"

    def test_cross_major_upgrade_migrates_boardcfg(
        self, bh_chips, fwbundle_path, prev_fwbundle_path, make_shadow
    ):
        """
        A real 18.x -> 19.x upgrade, including the boardcfg relocation.

        18.x images are a single blob at 0 whose boardcfg descriptor points at
        0xfff000; 19.x images are 20 sections with boardcfg at 0xd4000. Seeding
        the shadow with the older layout and then flashing the newer one makes
        writeback_boardcfg read boardcfg from the old address and repoint it at
        the new one -- the genuine migration, against real flash.

        Needs the large shadow window, so it skips unless the truncation probe
        confirms addressing above 16MB really works.
        """
        for chip in bh_chips:
            boardname = get_board_name(chip)
            old_image = bi.load_image(prev_fwbundle_path, boardname)
            new_image = bi.load_image(fwbundle_path, boardname)
            shadow = make_shadow(chip, list(old_image) + list(new_image))

            old_fd = bi.find_tag(old_image, "boardcfg")[2]
            new_fd = bi.find_tag(new_image, "boardcfg")[2]
            assert old_fd.spi_addr != new_fd.spi_addr, (
                "bundles put boardcfg at the same address, so this test would "
                "not exercise a migration"
            )

            # Lay down the older layout. The running chip is 19.x as far as
            # stage 1 can tell, so this is a major downgrade from its point of
            # view even though only the shadow is touched.
            flash_into_shadow(
                shadow,
                prev_fwbundle_path,
                boardname,
                seed=0x00,
                allow_major_downgrades=True,
            )
            migrated = chip_tag(shadow, "boardcfg")
            assert migrated is not None
            assert migrated[1].spi_addr == old_fd.spi_addr, "seed did not take"
            before = shadow.spi_read(old_fd.spi_addr, migrated[1].flags.f.image_size)

            # Now the upgrade under test.
            data = flash_into_shadow(shadow, fwbundle_path, boardname)
            assert_flashed(shadow, data.write)

            _, fd = chip_tag(shadow, "boardcfg")
            assert fd.spi_addr == new_fd.spi_addr, "boardcfg was not relocated"
            assert (
                shadow.spi_read(fd.spi_addr, len(before)) == before
            ), "boardcfg content was lost in the move"

            tags = {
                t.image_tag_str()
                for _, t in bi.iter_fds(bi.head_write(new_image).write)
            }
            on_shadow = set()
            for offset in range(0, 0x1000, FD_SIZE):
                found = boot_fs.read_fd(lambda a, s: shadow.spi_read(a, s), offset)
                if found is None or found.flags.f.invalid != 0:
                    break
                on_shadow.add(found.image_tag_str())
            assert tags <= on_shadow, f"missing tags after upgrade: {tags - on_shadow}"
