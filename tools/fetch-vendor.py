#!/usr/bin/env python3
"""Download the pinned front-end libraries into vendor/ for `viewer.py --offline`.

    python3 tools/fetch-vendor.py                # download + verify SHA-256
    python3 tools/fetch-vendor.py --print-hashes # download and print hashes (to re-pin)

Files whose hash does not match the one pinned below are refused (not written).
Python stdlib only.
"""
import hashlib
import sys
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor"

LIBS = {
    "marked.min.js": {
        "url": "https://cdn.jsdelivr.net/npm/marked@9.1.6/marked.min.js",
        "sha256": "6002af63485b043fa60ddaba1b34363b98d2a8b2c63b607004f3a2405a8a053a",
    },
    "force-graph.min.js": {
        "url": "https://cdn.jsdelivr.net/npm/force-graph@1.43.5/dist/force-graph.min.js",
        "sha256": "af9324ce63bb87f330643c04a9062ca17b3be32826234012197ced595eb0f83a",
    },
}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "second-brain-viewer-fetch-vendor"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def main() -> int:
    print_only = "--print-hashes" in sys.argv[1:]
    VENDOR.mkdir(exist_ok=True)
    failed = False
    for name, meta in LIBS.items():
        try:
            data = fetch(meta["url"])
        except Exception as e:  # noqa: BLE001 - report any network failure
            print(f"✗ {name}: download failed: {e}", file=sys.stderr)
            failed = True
            continue
        digest = hashlib.sha256(data).hexdigest()
        if print_only:
            print(f'{name}: "sha256": "{digest}"  ({len(data)} bytes)')
            continue
        if digest != meta["sha256"]:
            print(f"✗ {name}: SHA-256 mismatch\n    expected {meta['sha256']}\n    got      {digest}\n"
                  f"    refusing to write it. If the pin is stale, check with --print-hashes.", file=sys.stderr)
            failed = True
            continue
        (VENDOR / name).write_bytes(data)
        print(f"✓ {name}  {len(data)} bytes  sha256 {digest[:16]}…")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
