"""Realign a Mach-O string table that macOS 27 dyld rejects.

chdb-core / ClickHouse macOS arm64 binaries ship with LC_SYMTAB.stroff that is
4-byte but not 8-byte aligned. macOS 27 dyld refuses to load them with
"mis-aligned LINKEDIT string pool". This is a packaging/loader problem, not a
SQL feature limitation.

The fix moves the string table forward into the padding that already exists
before the code signature, updates stroff, and re-signs ad-hoc. File size and
every other offset stay unchanged. The result is written to a new file and
atomically renamed, so hardlinked/cloned package caches are never modified.

Usage: python scripts/fix_macho_strtab_alignment.py <path-to-macho> [--check]
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

MH_MAGIC_64 = 0xFEEDFACF
LC_SYMTAB = 0x2
LC_CODE_SIGNATURE = 0x1D
ALIGNMENT = 8


@dataclass(frozen=True)
class Layout:
    symtab_cmd_offset: int
    stroff: int
    strsize: int
    codesig_dataoff: int | None


def read_layout(data: bytes) -> Layout:
    magic, _cpu, _sub, _ftype, ncmds, _size, _flags, _res = struct.unpack_from("<8I", data, 0)
    if magic != MH_MAGIC_64:
        raise ValueError(f"not a thin 64-bit little-endian Mach-O (magic={magic:#x})")
    offset = 32
    symtab = None
    codesig = None
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<2I", data, offset)
        if cmd == LC_SYMTAB:
            _symoff, _nsyms, stroff, strsize = struct.unpack_from("<4I", data, offset + 8)
            symtab = (offset, stroff, strsize)
        elif cmd == LC_CODE_SIGNATURE:
            codesig = struct.unpack_from("<I", data, offset + 8)[0]
        offset += cmdsize
    if symtab is None:
        raise ValueError("LC_SYMTAB not found")
    return Layout(symtab[0], symtab[1], symtab[2], codesig)


def realign(path: Path) -> str:
    data = bytearray(path.read_bytes())
    layout = read_layout(bytes(data))
    if layout.stroff % ALIGNMENT == 0:
        return f"already aligned (stroff={layout.stroff})"
    pad = ALIGNMENT - layout.stroff % ALIGNMENT
    new_stroff = layout.stroff + pad
    limit = layout.codesig_dataoff if layout.codesig_dataoff is not None else len(data)
    if new_stroff + layout.strsize > limit:
        raise ValueError(
            f"no padding to shift string table: need {pad} bytes, "
            f"strtab end={layout.stroff + layout.strsize}, limit={limit}"
        )
    strtab = bytes(data[layout.stroff : layout.stroff + layout.strsize])
    data[layout.stroff : new_stroff] = b"\0" * pad
    data[new_stroff : new_stroff + layout.strsize] = strtab
    struct.pack_into("<I", data, layout.symtab_cmd_offset + 16, new_stroff)

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        subprocess.run(["codesign", "--force", "--sign", "-", str(tmp)], check=True, capture_output=True)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return f"realigned stroff {layout.stroff} -> {new_stroff} and re-signed ad-hoc"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1])
    if "--check" in argv[2:]:
        layout = read_layout(path.read_bytes())
        aligned = layout.stroff % ALIGNMENT == 0
        print(f"{path}: stroff={layout.stroff} aligned={aligned}")
        return 0 if aligned else 1
    print(f"{path}: {realign(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
