#!/usr/bin/env python3
"""Shared utilities for NetSpy — tiny, no external dependencies."""
import fcntl
import os
import socket
import struct


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


_VIRTUAL_PREFIX = ("docker", "veth", "virbr", "tap", "dummy", "br-", "tun", "vnet")


def local_ipv4() -> list:
    """Non-loopback IPv4 addresses of this host, interface order.

    Virtual interfaces are skipped (docker bridges, veth pairs, libvirt, taps):
    the start-up banner should show the address a user can actually open, not
    the seven 172.x bridges of a busy host. Real bridges like Unraid's `br0`
    stay, because their name has no dash.

    stdlib only (SIOCGIFADDR ioctl), so the image keeps its zero-dependency
    property. Used by the start-up banner: the first entry is the address a
    user is most likely to open in the browser. Never raises - logging must not
    be able to break the start-up.
    """
    addrs = []
    try:
        for _, name in socket.if_nameindex():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                req = struct.pack("256s", name.encode("utf-8")[:15])
                ip = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), 0x8915, req)[20:24])
            except OSError:
                continue
            finally:
                sock.close()
            if name.startswith(_VIRTUAL_PREFIX):
                continue
            if not ip.startswith("127.") and ip not in addrs:
                addrs.append(ip)
    except Exception:  # noqa: BLE001 - best effort only
        pass
    return addrs
