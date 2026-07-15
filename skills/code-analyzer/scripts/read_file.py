#!/usr/bin/env python3
"""Read a file and print its contents.

Usage:
    python read_file.py <path>           # via command-line argument
    FILE_PATH=<path> python read_file.py  # via environment variable

Environment variables:
    FILE_PATH   - Path to file to read
"""
import sys
import os

# Try command-line argument first, then environment variable
path = ""
if len(sys.argv) > 1:
    path = sys.argv[1]
else:
    path = os.environ.get("FILE_PATH", "")

if not path:
    print("Error: no file path provided (use: python read_file.py <path>)", file=sys.stderr)
    sys.exit(1)

try:
    content = open(path, encoding="utf-8").read()
    print(content, end="")
except FileNotFoundError:
    print(f"Error: file not found: {path}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
