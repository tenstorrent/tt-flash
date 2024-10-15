# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from typing import Callable, Optional, Tuple
import ctypes

# Define constants
TT_BOOT_FS_FD_HEAD_ADDR = 0x0
TT_BOOT_FS_SECURITY_BINARY_FD_ADDR = 0x3FE0
TT_BOOT_FS_FAILOVER_HEAD_ADDR = 0x4000
IMAGE_TAG_SIZE = 8


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
            if c == "\0":
                break
            output += chr(c)
        return output


def read_fd(reader, addr: int) -> tt_boot_fs_fd:
    fd = reader(addr, ctypes.sizeof(tt_boot_fs_fd))
    return tt_boot_fs_fd.from_buffer_copy(fd)


def read_tag(
    reader: Callable[[int, int], bytes], tag: str
) -> Optional[Tuple[int, tt_boot_fs_fd]]:
    curr_addr = 0
    while True:
        fd = read_fd(reader, curr_addr)

        if fd.flags.f.invalid != 0:
            return None

        if fd.image_tag_str() == tag:
            return curr_addr, fd

        curr_addr += ctypes.sizeof(tt_boot_fs_fd)
