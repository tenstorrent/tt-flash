# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from typing import Callable, Optional, Tuple
import ctypes

# Define constants
TT_BOOT_FS_FD_HEAD_ADDR = 0x0
TT_BOOT_FS_SECURITY_BINARY_FD_ADDR = 0x3FE0
TT_BOOT_FS_FAILOVER_HEAD_ADDR = 0x4000
IMAGE_TAG_SIZE = 8

# SPI RX training word, the last four bytes of the region the SMC ROM reads
# before the first image at 0x14000. The bundle carries it as a fixed value.
TT_BOOT_FS_SPI_RX_ADDR = 0x13FFC

# Multi-table boot filesystems advertise their descriptor tables through a
# header at this fixed flash address. The header is not read by the SMC ROM
# (which uses the fixed 0x0/0x4000 tables); it exists for firmware and tooling.
TT_BOOT_FS_HEADER_ADDR = 0x120000
BOOT_FS_HEADER_MAGIC = 0x54544246  # 'TTBF' in ASCII, little-endian
BOOT_FS_HEADER_VERSION = 1

# Upper bounds when walking flash that may be corrupt. The per-table FD cap
# matches the firmware's CONFIG_TT_BOOT_FS_IMAGE_COUNT_MAX default.
MAX_TABLES = 16
MAX_FDS_PER_TABLE = 32


class ExtendedStructure(ctypes.Structure):
    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        for field in self._fields_:
            field_name = field[0]

            self_value = getattr(self, field_name)
            other_value = getattr(other, field_name)

            # Handle comparison for ctypes.Array fields
            if isinstance(self_value, ctypes.Array):
                if len(self_value) != len(other_value):
                    return False
                for i in range(len(self_value)):
                    if self_value[i] != other_value[i]:
                        return False
            else:
                if self_value != other_value:
                    return False
        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        field_strings = []
        for field in self._fields_:
            field_name = field[0]

            field_value = getattr(self, field_name)

            # Handle string representation for ctypes.Array fields
            if isinstance(field_value, ctypes.Array):
                array_str = ", ".join(str(x) for x in field_value)
                field_strings.append(f"{field_name}=[{array_str}]")
            else:
                field_strings.append(f"{field_name}={field_value}")

        fields_repr = ", ".join(field_strings)
        return f"{self.__class__.__name__}({fields_repr})"


class ExtendedUnion(ctypes.Union):
    def __eq__(self, other):
        for fld in self._fields_:
            if getattr(self, fld[0]) != getattr(other, fld[0]):
                return False
        return True

    def __ne__(self, other):
        for fld in self._fields_:
            if getattr(self, fld[0]) != getattr(other, fld[0]):
                return True
        return False

    def __repr__(self):
        field_strings = []
        for field in self._fields_:
            field_name = field[0]

            field_value = getattr(self, field_name)
            field_strings.append(f"{field_name}={field_value}")
        fields_repr = ", ".join(field_strings)
        return f"{self.__class__.__name__}({fields_repr})"


# Define fd_flags structure
class fd_flags(ExtendedStructure):
    _fields_ = [
        ("image_size", ctypes.c_uint32, 24),
        ("invalid", ctypes.c_uint32, 1),
        ("executable", ctypes.c_uint32, 1),
        ("fd_flags_rsvd", ctypes.c_uint32, 6),
    ]


# Define fd_flags union
class fd_flags_u(ExtendedUnion):
    _fields_ = [("val", ctypes.c_uint32), ("f", fd_flags)]


# Define security_fd_flags structure
class security_fd_flags(ExtendedStructure):
    _fields_ = [
        ("signature_size", ctypes.c_uint32, 12),
        ("sb_phase", ctypes.c_uint32, 8),  # 0 - Phase0A, 1 - Phase0B
    ]


# Define security_fd_flags union
class security_fd_flags_u(ExtendedUnion):
    _fields_ = [("val", ctypes.c_uint32), ("f", security_fd_flags)]


# Define tt_boot_fs_fd structure (File descriptor)
class tt_boot_fs_fd(ExtendedStructure):
    _fields_ = [
        ("spi_addr", ctypes.c_uint32),
        ("copy_dest", ctypes.c_uint32),
        ("flags", fd_flags_u),
        ("data_crc", ctypes.c_uint32),
        ("security_flags", security_fd_flags_u),
        ("image_tag", ctypes.c_uint8 * IMAGE_TAG_SIZE),
        ("fd_crc", ctypes.c_uint32),
    ]

    def image_tag_str(self):
        output = ""
        for c in self.image_tag:
            # image_tag is a c_uint8 array, so each element is an int; the tag
            # is NUL-padded to IMAGE_TAG_SIZE. Stop at the first NUL so tags
            # shorter than 8 bytes (e.g. "cmfw") compare correctly.
            if c == 0:
                break
            output += chr(c)
        return output


# Header for a multi-table boot fs. Describes the number of descriptor tables;
# the 32-bit flash address of each table immediately follows the header.
class tt_boot_fs_header(ExtendedStructure):
    _fields_ = [
        ("magic", ctypes.c_uint32),
        ("version", ctypes.c_uint32),
        ("num_tables", ctypes.c_uint32),
    ]


def read_fd(reader, addr: int) -> Optional[tt_boot_fs_fd]:
    fd_size = ctypes.sizeof(tt_boot_fs_fd)
    fd = reader(addr, fd_size)
    if len(fd) < fd_size:
        return None
    return tt_boot_fs_fd.from_buffer_copy(fd)


def read_header(reader, addr: int) -> Optional[tt_boot_fs_header]:
    header_size = ctypes.sizeof(tt_boot_fs_header)
    raw = reader(addr, header_size)
    if len(raw) < header_size:
        return None
    return tt_boot_fs_header.from_buffer_copy(raw)


def find_descriptor_tables(reader: Callable[[int, int], bytes]) -> list:
    """
    Return the flash addresses of every descriptor table advertised by the
    boot fs header at TT_BOOT_FS_HEADER_ADDR.

    Falls back to the legacy fixed layout (ROM table at 0x0, failover table at
    0x4000) when there is no valid header: either the flash predates the
    multi-table layout, or the reader is backed by a buffer (e.g. a single
    FlashWrite chunk) that does not extend to the header address.
    """
    header = read_header(reader, TT_BOOT_FS_HEADER_ADDR)
    if header is None or header.magic != BOOT_FS_HEADER_MAGIC:
        return [TT_BOOT_FS_FD_HEAD_ADDR, TT_BOOT_FS_FAILOVER_HEAD_ADDR]
    if header.version != BOOT_FS_HEADER_VERSION or header.num_tables > MAX_TABLES:
        # A TTBF header we don't understand: don't guess at the layout.
        return []

    table_addrs = []
    addr = TT_BOOT_FS_HEADER_ADDR + ctypes.sizeof(tt_boot_fs_header)
    for _ in range(header.num_tables):
        raw = reader(addr, 4)
        if len(raw) < 4:
            break
        table_addrs.append(int.from_bytes(raw, "little"))
        addr += 4
    return table_addrs


def read_tag(
    reader: Callable[[int, int], bytes], tag: str
) -> Optional[Tuple[int, tt_boot_fs_fd]]:
    """
    Find the file descriptor for `tag`, scanning every descriptor table in the
    boot filesystem. Returns (fd_flash_addr, fd), or None if not found.

    The failover descriptor at TT_BOOT_FS_FAILOVER_HEAD_ADDR carries a blank
    image_tag on disk, because the SMC ROM identifies the failover slot by its
    fixed address rather than by tag. A caller asking for "failover" therefore
    also accepts a valid descriptor at that address whose on-disk tag is empty.

    Identifying it is deliberately a question of address alone. Whether the
    descriptor is marked executable says whether the ROM would boot it, not
    whether it is the failover slot, and a caller that cannot recognise a
    failover slot cannot reason about it either -- which for skip_boot_critical
    would mean silently rewriting the failover image on every update.
    """
    for table_addr in find_descriptor_tables(reader):
        curr_addr = table_addr
        for _ in range(MAX_FDS_PER_TABLE):
            fd = read_fd(reader, curr_addr)

            if fd is None or fd.flags.f.invalid != 0:
                break

            if fd.image_tag_str() == tag:
                return curr_addr, fd

            if (
                tag == "failover"
                and curr_addr == TT_BOOT_FS_FAILOVER_HEAD_ADDR
                and fd.image_tag_str() == ""
            ):
                return curr_addr, fd

            curr_addr += ctypes.sizeof(tt_boot_fs_fd)

    return None
