"""Print a fresh high-entropy token for an operator to store as a secret.

The value goes to stdout exactly once and is never read back from anywhere:

    python -m mcp_http_transport.generate_token > /path/outside/the/repo/token

It deliberately does not write a file itself, so it cannot silently leave a
secret in the working tree.
"""

from __future__ import annotations

import argparse

from .auth import generate_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bytes", type=int, default=32, help="entropy in bytes (default 32)")
    args = parser.parse_args(argv)
    if args.bytes < 16:
        parser.error("--bytes must be at least 16")
    print(generate_token(args.bytes))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())