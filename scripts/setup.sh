#!/usr/bin/env bash
# Install pinned dependencies and, on macOS, realign chdb's Mach-O string tables
# (macOS 27 dyld rejects the published wheels: "mis-aligned LINKEDIT string pool").
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --python "${MLH_PYTHON:-python3.11}"
if [[ "$(uname -s)" == "Darwin" ]]; then
  site=".venv/lib/python3.11/site-packages"
  while IFS= read -r lib; do
    .venv/bin/python scripts/fix_macho_strtab_alignment.py "$lib" --check >/dev/null \
      || .venv/bin/python scripts/fix_macho_strtab_alignment.py "$lib"
  done < <(find "$site"/chdb* -type f \( -name '*.so' -o -name '*.dylib' \))
fi
.venv/bin/python -c 'import chdb; print("chdb engine", chdb.query("SELECT version()", "CSV").bytes().decode().strip())'
