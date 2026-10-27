"""Encryption key for chDB named collections (remote credentials).

The key lives OUTSIDE the data directory (default ~/.config/mini-loghouse/nc.key)
so a copied data dir alone does not reveal passwords. It is created once with
O_EXCL + 0600 and is never regenerated automatically: a missing or different key
puts the app into recovery mode instead of silently orphaning stored secrets.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


class KeyError_(Exception):
    """Key file problem that requires operator action (recovery mode)."""


@dataclass(frozen=True)
class KeyMaterial:
    key_hex: str = ""

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(("mlh-nc:" + self.key_hex).encode()).hexdigest()[:16]

    def __repr__(self) -> str:  # never print the key
        return f"KeyMaterial(fingerprint={self.fingerprint})"


def _check_private(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise KeyError_(f"key file permissions must be 0600 (found {oct(mode)})")


def create_key(path: Path) -> KeyMaterial:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    key_hex = secrets.token_hex(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key_hex + "\n")
    return KeyMaterial(key_hex)


def load_key(path: Path) -> KeyMaterial:
    if not path.exists():
        raise KeyError_("key file is missing")
    _check_private(path)
    key_hex = path.read_text().strip()
    if not _KEY_RE.fullmatch(key_hex):
        raise KeyError_("key file content is not a 128-bit hex key")
    return KeyMaterial(key_hex)


def load_or_create(path: Path, expected_fingerprint: str | None) -> KeyMaterial:
    """Create the key only for a brand-new installation (no fingerprint recorded)."""
    if not path.exists() and expected_fingerprint is None:
        return create_key(path)
    material = load_key(path)
    if expected_fingerprint is not None and material.fingerprint != expected_fingerprint:
        raise KeyError_("key fingerprint does not match the one recorded for this data directory")
    return material
