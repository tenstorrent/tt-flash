# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Helpers for reading, mutating and repacking Blackhole flash images.

A Blackhole ``image.bin`` is an ASCII file of alternating ``@<decimal address>``
lines and uppercase base16 data lines.  ``parse_writes_from_image`` turns that
into absolute-addressed :class:`FlashWrite` objects; :func:`serialize_image` is
its inverse, which lets tests mutate a real image and feed it back through the
production flash path.
"""

import ctypes
import io
import json
import tarfile
from base64 import b16encode
from typing import Iterator, Optional, Sequence

from tt_flash import boot_fs
from tt_flash.blackhole import FlashWrite, calculate_checksum, parse_writes_from_image

FD_SIZE = ctypes.sizeof(boot_fs.tt_boot_fs_fd)
SECTOR = 0x1000

# The boot fs terminates the descriptor table on the first FD whose `invalid`
# bit is set; erased flash (0xFF) reads as invalid, so 0xFF is the correct pad.
ERASED = 0xFF


def serialize_image(writes: Sequence[FlashWrite]) -> bytes:
    """Inverse of :func:`parse_writes_from_image`."""
    lines = []
    for write in sorted(writes, key=lambda w: w.offset):
        lines.append(f"@{write.offset}")
        lines.append(b16encode(bytes(write.write)).decode("ascii"))
    return ("\n".join(lines) + "\n").encode("ascii")


def load_image(bundle_path: str, boardname: str) -> list[FlashWrite]:
    """Read one board's image out of a fwbundle as FlashWrites."""
    with tarfile.open(bundle_path, "r") as tar:
        member = tar.extractfile(f"./{boardname}/image.bin")
        if member is None:
            raise LookupError(f"{bundle_path} has no ./{boardname}/image.bin")
        return parse_writes_from_image(member.read())


def head_write(writes: Sequence[FlashWrite]) -> FlashWrite:
    """The FlashWrite carrying the boot fs descriptor table."""
    for write in writes:
        if write.offset == 0:
            return write
    raise LookupError("image has no descriptor table write at offset 0")


def _reader(buf) -> "callable":
    return lambda addr, size: bytes(buf[addr : addr + size])


def iter_fds(buf) -> Iterator[tuple[int, boot_fs.tt_boot_fs_fd]]:
    """Yield ``(byte offset, fd)`` for each valid FD in a descriptor table.

    Bounded to one sector, which is all the table occupies on flash.
    """
    read = _reader(buf)
    for offset in range(0, SECTOR, FD_SIZE):
        fd = boot_fs.read_fd(read, offset)
        if fd is None or fd.flags.f.invalid != 0:
            return
        yield offset, fd


def find_tag(
    writes: Sequence[FlashWrite], tag: str
) -> Optional[tuple[FlashWrite, int, boot_fs.tt_boot_fs_fd]]:
    """Locate ``tag`` in an image, returning (write, offset in write, fd)."""
    for write in writes:
        found = boot_fs.read_tag(_reader(write.write), tag)
        if found is not None:
            return write, found[0], found[1]
    return None


def body_write(
    writes: Sequence[FlashWrite], fd: boot_fs.tt_boot_fs_fd
) -> Optional[FlashWrite]:
    """The write holding ``fd``'s body, matched the way the handlers match it.

    Deliberately an exact-offset comparison, mirroring ``writeback_boardcfg``
    and ``skip_ccfgovr``.  Returns None when the body is embedded inside a
    larger write rather than starting one.
    """
    for write in writes:
        if write.offset == fd.spi_addr:
            return write
    return None


def seal_fd(fd: boot_fs.tt_boot_fs_fd) -> boot_fs.tt_boot_fs_fd:
    """Recompute an FD's own checksum after mutating it."""
    fd.fd_crc = 0
    fd.fd_crc = calculate_checksum(bytes(fd)[:-4])
    return fd


def store_fd(buf, offset: int, fd: boot_fs.tt_boot_fs_fd) -> None:
    """Write ``fd`` back into a descriptor table, resealing it first."""
    buf[offset : offset + FD_SIZE] = bytes(seal_fd(fd))


def drop_section(writes: Sequence[FlashWrite], offset: int) -> list[FlashWrite]:
    """Remove the write starting at ``offset``, leaving its descriptor alone."""
    return [write for write in writes if write.offset != offset]


def embed_sections(
    writes: Sequence[FlashWrite], addrs: Sequence[int], container_offset: int
) -> list[FlashWrite]:
    """Fold standalone body writes into one larger write starting earlier.

    The bodies keep their addresses and contents, but no write *begins* at any
    of them any more.  That is the shape which defeats the exact-offset
    matching in ``skip_ccfgovr`` and ``writeback_boardcfg`` -- and a shape real
    images already have elsewhere, since ``mainimg`` sits inside the write at
    ``0x29d000`` rather than starting one.
    """
    bodies = {}
    for addr in addrs:
        match = [write for write in writes if write.offset == addr]
        if not match:
            raise LookupError(f"no write starts at 0x{addr:x}")
        bodies[addr] = bytes(match[0].write)

    if container_offset > min(bodies):
        raise ValueError("container must start at or before the first body")
    end = max(addr + len(data) for addr, data in bodies.items())

    overlapping = [
        write
        for write in writes
        if write.offset not in bodies and container_offset <= write.offset < end
    ]
    if overlapping:
        raise ValueError(
            f"container 0x{container_offset:x}..0x{end:x} would swallow "
            f"{[hex(w.offset) for w in overlapping]}"
        )

    container = bytearray([ERASED]) * (end - container_offset)
    for addr, data in bodies.items():
        start = addr - container_offset
        container[start : start + len(data)] = data

    kept = [write for write in writes if write.offset not in bodies]
    kept.append(FlashWrite(container_offset, container))
    kept.sort(key=lambda w: w.offset)
    return kept


def write_fwbundle(
    path: str,
    boardname: str,
    writes: Sequence[FlashWrite],
    mask: list[dict],
    manifest: dict,
) -> str:
    """Repack mutated writes into a minimal fwbundle tarball."""

    def add(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    with tarfile.open(path, "w:gz") as tar:
        add(tar, "./manifest.json", json.dumps(manifest).encode())
        add(tar, f"./{boardname}/image.bin", serialize_image(writes))
        add(tar, f"./{boardname}/mask.json", json.dumps(mask).encode())
    return path


def read_manifest(bundle_path: str) -> dict:
    with tarfile.open(bundle_path, "r") as tar:
        member = tar.extractfile("./manifest.json")
        if member is None:
            raise LookupError(f"{bundle_path} has no manifest")
        return json.loads(member.read())


def read_mask(bundle_path: str, boardname: str) -> list[dict]:
    with tarfile.open(bundle_path, "r") as tar:
        member = tar.extractfile(f"./{boardname}/mask.json")
        if member is None:
            raise LookupError(f"{bundle_path} has no ./{boardname}/mask.json")
        return json.loads(member.read())
