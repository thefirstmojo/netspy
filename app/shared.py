#!/usr/bin/env python3
"""Shared utilities for NetSpy — tiny, no external dependencies."""
import os


def load_version() -> str:
    """Read the VERSION file from the app directory (or parent)."""
    base = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(base, "VERSION"), os.path.join(base, "..", "VERSION")):
        try:
            with open(p) as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            continue
    return "dev"
