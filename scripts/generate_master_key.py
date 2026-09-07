#!/usr/bin/env python3
"""Create a Fernet master key without printing or overwriting secrets."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from cryptography.fernet import Fernet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("secrets/master.key"))
    parser.add_argument("--force", action="store_true", help="replace an existing key (destroys access to old encrypted data)")
    args = parser.parse_args()

    output = args.output.expanduser()
    if output.exists() and not args.force:
        raise SystemExit(f"key already exists: {output} (use --force only when rotating deliberately)")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(Fernet.generate_key())
    os.chmod(output, 0o600)
    print(f"created Fernet master key: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
