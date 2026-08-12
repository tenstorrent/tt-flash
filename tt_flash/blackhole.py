# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

import struct
from base64 import b16decode
from dataclasses import dataclass
from typing import Optional

from tt_flash import boot_fs
from tt_flash.chip import BhChip
from tt_flash.error import TTError


@dataclass
class FlashWrite:
    offset: int
    write: bytearray


def calculate_checksum(data: bytes) -> int:
    """
    Calculate 32-bit additive checksum for bootrom validation
    """
    calculated_checksum = 0

    if len(data) < 4:
        return 0

    for i in range(0, len(data), 4):
        value = int.from_bytes(data[i:][:4], "little")
        calculated_checksum += value

    calculated_checksum &= 0xFFFFFFFF

    return calculated_checksum


def writeback_boardcfg(chip: BhChip, writes: list[FlashWrite]) -> list[FlashWrite]:
    """
    Modify writes to flash to replace placeholder boardcfg data from the flash image
    with the existing boardcfg data on the chip. Writes back boardcfg data to the SPI
    address specified by the flash image and modifies the boardcfg boot fs fd to match
    the actual boardcfg data.

    Args:
        chip: BH chip to be written to
        writes: A list of FlashWrites created from the flash image

    Returns:
        A list of FlashWrites modified to include chip's existing boardcfg from its SPI.
    """
    # Find current boardcfg fd in SPI to modify and determine where to read boardcfg data from
    fd_in_spi = boot_fs.read_tag(
        lambda addr, size: chip.spi_read(addr, size), "boardcfg"
    )
    if fd_in_spi is None:
        raise TTError("Couldn't find boardcfg on chip")

    # Find boardcfg in current fd
    fd_to_flash = None
    boardcfg_write = None
    for write in writes:
        fd_to_flash = boot_fs.read_tag(
            lambda addr, size: write.write[addr : addr + size], "boardcfg"
        )
        if fd_to_flash is not None:
            boardcfg_write = write
            break
    if fd_to_flash is None:
        raise TTError("Couldn't find boardcfg in flash package")

    # Read back boardcfg data in SPI
    boardcfg_in_spi = chip.spi_read(
        fd_in_spi[1].spi_addr, fd_in_spi[1].flags.f.image_size
    )

    # Manipulate fd_in_spi
    fd_in_spi[1].spi_addr = fd_to_flash[1].spi_addr
    fd_in_spi[1].fd_crc = 0

    # Calculate fd checksum
    fd_chk = calculate_checksum(bytes(fd_in_spi[1])[:-4])
    fd_in_spi[1].fd_crc = fd_chk

    # Replace boardcfg fd_to_flash[1] with fd_in_spi[1] that we modified
    fd_as_data = bytes(fd_in_spi[1])
    boardcfg_write.write[fd_to_flash[0] : fd_to_flash[0] + len(fd_as_data)] = fd_as_data

    # Find boardcfg data write
    # this assumes that boardcfg has its own FlashWrite with its spi_addr as the offset
    data_write = None
    for write in writes:
        if write.offset == fd_to_flash[1].spi_addr:
            data_write = write
    if data_write is not None:
        # Replace boardcfg data to flash with boardcfg data from SPI
        data_write.write[0 : len(boardcfg_in_spi)] = boardcfg_in_spi
    else:
        # No boardcfg data write in this flash image, add it in so boardcfg is written to the correct spi_addr
        writes.append(FlashWrite(fd_to_flash[1].spi_addr, bytearray(boardcfg_in_spi)))
        writes.sort(key=lambda x: x.offset)

    flashed_fd = boot_fs.read_tag(
        lambda addr, size: boardcfg_write.write[addr : addr + size], "boardcfg"
    )
    assert flashed_fd[1] == fd_in_spi[1], f"{flashed_fd[1]} != {fd_in_spi[1]}"

    return writes


CCFGOVR_TAGS = ("ccfgovra", "ccfgovrb")


def skip_ccfgovr(chip: BhChip, writes: list[FlashWrite]) -> list[FlashWrite]:
    """
    Drop the body writes for the ccfgovra and ccfgovrb banks so the on-chip
    partitions are preserved across firmware updates.

    These banks hold a CRC-protected protobuf body that the firmware decodes
    on top of cmfwcfg at boot. They are field-updated by tt-mod (and friends),
    which rewrites the body but cannot update the FD's data_crc — the FD lives
    in the descriptor region, a different physical sector. The firmware
    therefore ignores data_crc for these tags and validates each bank via the
    cksum word stored inside its header. tt-flash mirrors that contract: we
    leave the FD entries (and their stale data_crc values) alone, and we don't
    overwrite the bank bodies. On a chip that has never seen these partitions,
    the on-chip bytes are 0xFF and the firmware falls back to cmfwcfg, which
    is the documented first-boot behaviour.
    """
    for tag in CCFGOVR_TAGS:
        fd_to_flash = None
        for write in writes:
            fd_to_flash = boot_fs.read_tag(
                lambda addr, size: write.write[addr : addr + size], tag
            )
            if fd_to_flash is not None:
                break

        if fd_to_flash is None:
            continue

        bank_addr = fd_to_flash[1].spi_addr
        writes = [w for w in writes if w.offset != bank_addr]

    return writes


# Boot-critical images provide the path by which the board boots at all: the
# SMC MCUBoot bootloader (cmfw), its recovery image (safeimg) and trailer
# (safetail), and the bootrom failover MCUBoot (failover). They live in the ROM
# (0x0) and failover (0x4000) descriptor tables. A field update should not
# rewrite them, so a power loss during an update cannot corrupt the fallback
# boot path.
BOOT_CRITICAL_TAGS = ("cmfw", "safeimg", "safetail", "failover")

# MCUBoot image header and TLV area, as written by imgtool.
MCUBOOT_IMAGE_MAGIC = 0x96F3B83D
MCUBOOT_HEADER = struct.Struct("<IIHHI")  # magic, load_addr, hdr_size, prot_tlv_size, img_size
MCUBOOT_TLV_INFO = struct.Struct("<HH")  # magic, total size
MCUBOOT_TLV = struct.Struct("<HH")  # type, size
MCUBOOT_TLV_INFO_MAGICS = (0x6907, 0x6908)
MCUBOOT_TLV_KEYHASH = 0x01
MCUBOOT_TLV_SHA256 = 0x10


def boot_image_identity(image: bytes) -> bytes:
    """
    Return a value identifying what a boot image contains, ignoring its signature.

    Signing is not reproducible: two builds of the same source produce images
    that are identical except for the trailing signature, so raw bytes cannot
    tell a rebuild apart from a real change. imgtool records a SHA-256 over the
    header and payload in the image's TLV area, and a hash of the signing key
    beside it; together they answer both "does this hold the same image" and
    "was it signed by the same key", the second of which matters because a key
    rotation leaves the payload hash untouched.

    An image with no MCUBoot header (MCUBoot itself) carries no signature and is
    reproducible, so it identifies as its own bytes. So does a signed image
    whose TLV area cannot be parsed, which costs an unnecessary rewrite but
    never skips one that was needed.
    """
    if len(image) < MCUBOOT_HEADER.size:
        return image

    magic, _, hdr_size, _, img_size = MCUBOOT_HEADER.unpack_from(image)
    if magic != MCUBOOT_IMAGE_MAGIC:
        return image

    sha256 = None
    keyhash = None

    # Protected and unprotected TLVs sit in separate sections, each introduced
    # by its own info header, so walk sections until one doesn't start with a
    # recognised magic.
    offset = hdr_size + img_size
    while offset + MCUBOOT_TLV_INFO.size <= len(image):
        info_magic, info_size = MCUBOOT_TLV_INFO.unpack_from(image, offset)
        if info_magic not in MCUBOOT_TLV_INFO_MAGICS:
            break

        if info_size <= MCUBOOT_TLV_INFO.size:
            # A section that doesn't extend past its own info header carries no
            # TLVs and, taken at face value, would leave offset where it is.
            # Truncated or zeroed TLV areas reach here, so stop rather than
            # re-read the same header forever.
            break

        section_end = min(offset + info_size, len(image))
        pos = offset + MCUBOOT_TLV_INFO.size
        while pos + MCUBOOT_TLV.size <= section_end:
            tlv_type, tlv_size = MCUBOOT_TLV.unpack_from(image, pos)
            value = image[pos + MCUBOOT_TLV.size : pos + MCUBOOT_TLV.size + tlv_size]
            if tlv_type == MCUBOOT_TLV_SHA256:
                sha256 = value
            elif tlv_type == MCUBOOT_TLV_KEYHASH:
                keyhash = value
            pos += MCUBOOT_TLV.size + tlv_size

        offset = section_end

    if sha256 is None:
        return image

    return sha256 + (keyhash or b"")


def descriptor_identity(fd: boot_fs.tt_boot_fs_fd) -> bytes:
    """
    A descriptor's fields other than the checksums, for comparing two of them.

    data_crc covers the image bytes, which differ between two builds of the
    same source because signing is not reproducible, and fd_crc is derived from
    the rest of the descriptor. Everything else -- where the image lives, where
    it is loaded, whether the ROM executes it, its size and security flags --
    has to match before the resident descriptor can stand in for the bundle's.
    """
    copy = boot_fs.tt_boot_fs_fd.from_buffer_copy(bytes(fd))
    copy.data_crc = 0
    copy.fd_crc = 0
    return bytes(copy)


def _find_fd_in_writes(
    writes: list[FlashWrite], tag: str
) -> Optional[tuple[FlashWrite, int, boot_fs.tt_boot_fs_fd]]:
    """
    Locate tag's FD among the image writes.

    Returns the write holding the descriptor, the descriptor's offset within
    that write, and the descriptor itself, or None if the image doesn't carry
    this tag.

    The failover descriptor has a blank on-disk image_tag (the SMC ROM
    identifies it by its fixed address, not by tag), so the by-tag scan cannot
    find it. Match it by write offset instead: a write whose offset equals
    TT_BOOT_FS_FAILOVER_HEAD_ADDR starts with the failover descriptor.
    """
    for write in writes:
        found = boot_fs.read_tag(
            lambda addr, size: write.write[addr : addr + size], tag
        )
        if found is not None:
            return write, found[0], found[1]

        if tag == "failover" and write.offset == boot_fs.TT_BOOT_FS_FAILOVER_HEAD_ADDR:
            fd = boot_fs.read_fd(
                lambda addr, size: write.write[addr : addr + size], 0
            )
            if fd is not None and fd.flags.f.invalid == 0:
                return write, 0, fd

    return None


def _find_body_write(
    writes: list[FlashWrite], spi_addr: int, size: int
) -> Optional[tuple[FlashWrite, int]]:
    """
    Locate the write carrying an image's bytes, and the image's offset within it.

    An image body does not always get a write of its own. The bundle emits each
    contiguous run of flash as a single write, so the SPI RX training word at
    0x13FFC arrives merged with the MCUBoot image that begins right after it at
    0x14000. Matching a write by its offset alone therefore misses MCUBoot on
    every real bundle, which is why this looks for the write that *contains* the
    image instead.
    """
    for write in writes:
        start = spi_addr - write.offset
        if start >= 0 and start + size <= len(write.write):
            return write, start
    return None


def _without_range(write: FlashWrite, start: int, size: int) -> list[FlashWrite]:
    """
    The writes still needed once [start, start+size) is skipped.

    Usually the skipped image is the whole write and nothing remains, but when
    it shares a write with adjacent data -- the training word ahead of MCUBoot
    -- that data still has to be written.
    """
    remaining = []
    if start > 0:
        remaining.append(FlashWrite(write.offset, write.write[:start]))
    end = start + size
    if end < len(write.write):
        remaining.append(FlashWrite(write.offset + end, write.write[end:]))
    return remaining


def skip_boot_critical(
    chip: BhChip, writes: list[FlashWrite], update_boot_images: bool = False
) -> list[FlashWrite]:
    """
    Leave each boot-critical image alone when the chip already holds the same
    image, so a routine field update never erases or rewrites the boot-critical
    flash sectors (avoiding a power-loss window that could corrupt the fallback
    boot path).

    "The same image" is a comparison of content, not of raw bytes, because
    signing is not reproducible; see boot_image_identity.

    Skipping means keeping the resident descriptor as well as the resident
    image. The two have to move together: the bundle's descriptor records a
    data_crc over the bundle's bytes, so writing it while leaving the resident
    image in place would describe an image that is not on flash -- a
    combination the boot ROM rejects, on both the primary and failover paths.
    For the same reason the resident descriptor is only reused when it says
    what the bundle's descriptor says apart from that checksum. A bundle that
    relocates a boot-critical image, loads it somewhere else, or changes
    whether the ROM executes it is changing something the image bytes do not
    carry, so it writes the descriptor and the image in full.

    When the on-chip copy differs (an unprovisioned board, a damaged image, or
    a genuine change to a boot image), both writes are kept, restoring a
    consistent state. Set update_boot_images to write the bundle's own images
    unconditionally.
    """
    if update_boot_images:
        return writes

    for tag in BOOT_CRITICAL_TAGS:
        found = _find_fd_in_writes(writes, tag)
        if found is None:
            # This board's image doesn't ship this tag; nothing to skip.
            continue
        table_write, fd_offset, image_fd = found

        image_size = image_fd.flags.f.image_size
        body = _find_body_write(writes, image_fd.spi_addr, image_size)
        if body is None:
            continue
        body_write, body_offset = body

        resident = boot_fs.read_tag(
            lambda addr, size: chip.spi_read(addr, size), tag
        )
        if resident is None:
            # Nothing on the chip to keep, so write the bundle's copy.
            continue
        resident_fd = resident[1]

        if descriptor_identity(resident_fd) != descriptor_identity(image_fd):
            print(
                f"Boot-critical image '{tag}' is described differently by the "
                "firmware bundle than by the chip; it will be rewritten. Do "
                "not power off the board until the update completes."
            )
            continue

        resident_image = chip.spi_read(
            resident_fd.spi_addr, resident_fd.flags.f.image_size
        )
        if calculate_checksum(resident_image) != resident_fd.data_crc:
            # The image on flash isn't the one its descriptor describes. The
            # content comparison below cannot catch this on its own, since a
            # damaged image still carries the hash of what it was meant to be,
            # so the descriptor is what reveals it.
            print(
                f"Boot-critical image '{tag}' on the chip does not match its "
                "descriptor; it will be rewritten. Do not power off the board "
                "until the update completes."
            )
            continue

        bundle_image = bytes(body_write.write[body_offset : body_offset + image_size])
        if boot_image_identity(resident_image) != boot_image_identity(bundle_image):
            print(
                f"Boot-critical image '{tag}' on the chip differs from the "
                "firmware bundle; it will be rewritten. Do not power off the "
                "board until the update completes."
            )
            continue

        # Keep the descriptor that describes the image actually on flash.
        resident_fd_bytes = bytes(resident_fd)
        table_write.write[fd_offset : fd_offset + len(resident_fd_bytes)] = (
            resident_fd_bytes
        )
        writes = [w for w in writes if w is not body_write] + _without_range(
            body_write, body_offset, image_size
        )
        writes.sort(key=lambda w: w.offset)

    return writes


# Fixed-address regions below the first image that a field update has no reason
# to change: the two descriptor tables the SMC ROM reads, and the SPI RX training
# word. Not writing them is the whole point -- a write into a sector erases that
# sector first, which is the power-loss window the static/mutable table split
# exists to close.
STATIC_REGION_ADDRS = (
    boot_fs.TT_BOOT_FS_FD_HEAD_ADDR,
    boot_fs.TT_BOOT_FS_FAILOVER_HEAD_ADDR,
    boot_fs.TT_BOOT_FS_SPI_RX_ADDR,
)


def skip_unchanged_static_regions(
    chip: BhChip, writes: list[FlashWrite], update_boot_images: bool = False
) -> list[FlashWrite]:
    """
    Drop writes to those regions when the board already holds exactly those bytes.

    This has to run after every handler that edits a descriptor in place
    (skip_boot_critical, writeback_boardcfg), so that what gets compared is what
    would actually reach flash. When a boot-critical image did change, its
    descriptor still carries the bundle's data_crc, the comparison fails, and the
    table is written as before.
    """
    if update_boot_images:
        return writes

    kept = []
    for write in writes:
        if write.offset in STATIC_REGION_ADDRS:
            resident = chip.spi_read(write.offset, len(write.write))
            if resident == bytes(write.write):
                continue
        kept.append(write)

    return kept


TAG_HANDLERS = {"write-boardcfg": writeback_boardcfg}


def parse_writes_from_image(image: bytes) -> list[FlashWrite]:
    """
    Parse data from an image file into a list of FlashWrites.

    Args:
        image: raw bytes read from an image file in a fwbundle

    Returns:
        A sorted list of FlashWrites corresponding to the image data
    """
    writes = []

    curr_addr = 0
    for line in image.decode("utf-8").splitlines():
        line = line.strip()
        if line.startswith("@"):  # address of a flash partition
            curr_addr = int(line.lstrip("@").strip())
        else:
            data = b16decode(line)
            curr_stop = curr_addr + len(data)
            if not isinstance(data, bytearray):
                data = bytearray(data)
            writes.append(FlashWrite(curr_addr, data))

            curr_addr = curr_stop

    writes.sort(key=lambda x: x.offset)

    return writes


def boot_fs_write(
    chip: BhChip,
    boardname_to_display: str,
    mask: list[dict],
    writes: list[FlashWrite],
    update_boot_images: bool = False,
) -> list[FlashWrite]:
    """
    Apply board-specific modifications to writes using tags from the mask. Process the mask tags to determine which tag handlers
    to apply to writes, then apply the handlers to modify writes.

    Args:
        chip: BH chip to be written to
        boardname_to_display: boardname of the chip, used for generating error messages
        mask: list of dicts containing tags
        writes: list of FlashWrites to be modified by tag handlers
        update_boot_images: write the bundle's boot-critical images even when
            the chip already holds the same ones

    Returns:
        list of FlashWrites modified by tag handlers
    """
    param_handlers = []
    for v in mask:
        tag = v.get("tag", None)

        if tag is None or not isinstance(tag, str):
            raise TTError(
                f"Invalid mask format for {boardname_to_display}; expected to see a list of dicts with keys 'tag'"
            )

        if tag in TAG_HANDLERS:
            param_handlers.append(TAG_HANDLERS[tag])
        else:
            if len(TAG_HANDLERS) > 0:
                pretty_tags = [f"'{x}'" for x in TAG_HANDLERS.keys()]
                pretty_tags[-1] = f"or {pretty_tags[-1]}"
                raise TTError(
                    f"Invalid tag {tag} for {boardname_to_display}; expected to see one of {pretty_tags}"
                )
            else:
                raise TTError(
                    f"Invalid tag {tag} for {boardname_to_display}; there aren't any tags defined!"
                )

    for handler in param_handlers:
        writes = handler(chip, writes)

    # Always preserve any on-chip ccfgovr banks: the image ships an empty
    # bank but the on-chip bytes may have been mutated in the field by
    # tt-mod, and the firmware decodes them on top of cmfwcfg at boot.
    # Triggered by the FD entries being present in the image, not by a
    # mask.json tag, so older bundles without ccfgovr remain a no-op.
    writes = skip_ccfgovr(chip, writes)

    # Leave the boot-critical images alone unless they actually changed (or the
    # user asked for them): rewriting them only risks corrupting the fallback
    # boot path.
    writes = skip_boot_critical(chip, writes, update_boot_images)

    # Last, once every handler has settled what the descriptors will say: if a
    # fixed ROM-region write would go back unchanged, don't write it at all.
    writes = skip_unchanged_static_regions(chip, writes, update_boot_images)

    return writes
