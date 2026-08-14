#!/usr/bin/env bash
# Build the single-file distributable, n8n-warden.pyz.
#
# zipapp bundles ./src into one executable archive with no install step and no
# dependencies — the property that makes a single file attractive, without
# forcing the source to live in one file.
set -euo pipefail

cd "$(dirname "$0")"
OUT=n8n-warden.pyz

find src -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
python3 -m zipapp src -o "$OUT" -p "/usr/bin/env python3" -c
chmod +x "$OUT"

echo "built $OUT ($(du -h "$OUT" | cut -f1))"
echo "verifying..."
./"$OUT" selftest >/dev/null && echo "  selftest passed"
./"$OUT" --version
