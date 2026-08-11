#!/usr/bin/env python3
"""Phase-2 entrypoint. Refuses silent derivation until SM-005 is completed."""
import argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    parser.parse_args()
    raise SystemExit("Not implemented: complete execution task SM-005; do not hand-write derived models")


if __name__ == "__main__":
    main()

