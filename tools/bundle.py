#!/usr/bin/env python3
"""Phase-2 entrypoint. See execution task SM-005."""
import argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    parser.parse_args()
    raise SystemExit("Not implemented: complete execution task SM-005")


if __name__ == "__main__":
    main()

