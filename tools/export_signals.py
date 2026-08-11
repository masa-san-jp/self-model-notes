#!/usr/bin/env python3
"""Phase-4 entrypoint. Deny by default until consent gates are implemented."""
import argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    parser.add_argument("--purpose", required=True)
    parser.parse_args()
    raise SystemExit("Export denied: complete SM-007 and SM-008 before exporting any signals")


if __name__ == "__main__":
    main()

