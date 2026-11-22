#!/usr/bin/env python3
"""NetSpy application settings - <CONFIG_DIR>/settings.yaml.

The server list lives in servers.yaml; everything else that the Settings tab
can change lives here. The file is written by the web process and stays
human-editable, and environment variables always win over it.

Stored shape:

    mcp:
      enabled: true      # false = the MCP listener is not started at all
      token: "..."       # empty = listener not started

Resolution (resolve_mcp) - same fallback logic as the server list:
    token   : MCP_TOKEN env -> mcp.token in this file -> "" (listener off)
    enabled : mcp.enabled from this file -> default: on as soon as a token
              exists (keeps composes from before v0.7.47 working untouched)
    port    : MCP_PORT env -> 8092

Like servers.yaml the file is written 0666 so it stays editable on the host.
"""
from __future__ import annotations

import json
import os

from mcp_server import DEFAULT_PORT

SETTINGS_FILE = "settings.yaml"

HEADER = (
    "# NetSpy - application settings (written by the Settings tab, "
    "also human-editable)\n"
    "# Environment variables win over this file: MCP_TOKEN beats mcp.token,\n"
    "# MCP_PORT beats the default port.\n"
    "#\n"
    "# mcp.enabled: false = the read-only MCP endpoint (POST /mcp) is not started\n"
    "# mcp.token          = required token; empty = endpoint stays off\n"
)


def settings_path(config_dir: str) -> str:
    return os.path.join(config_dir, SETTINGS_FILE)


def _parse(text: str):
    """YAML first, then JSON (with or without the leading # header)."""
    try:
        import yaml
        return yaml.safe_load(text)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - broken YAML is not a crash
        return None
    for candidate in (text,
                      "\n".join(l for l in text.splitlines()
                                if not l.lstrip().startswith("#"))):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def load_settings(config_dir: str) -> dict:
    """Settings as a dict; {} when the file is missing, empty or broken."""
    try:
        with open(settings_path(config_dir), encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return {}
    data = _parse(txt)
    return data if isinstance(data, dict) else {}


def save_settings(config_dir: str, data: dict) -> str:
    """Write settings.yaml atomically.

    0666 like servers.yaml: the file has to stay editable on the host (the
    container runs as root with cap_drop ALL, so 0600 would lock everyone else
    out). The token inside is as plain as the MCP_TOKEN env var - the file is
    written to the appdata folder, not to the web root.
    """
    os.makedirs(config_dir, exist_ok=True, mode=0o777)
    path = settings_path(config_dir)
    tmp = path + ".tmp"
    payload = data if isinstance(data, dict) else {}
    try:
        import yaml
        body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    except ImportError:
        body = json.dumps(payload, indent=2)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(HEADER + body)
    os.chmod(tmp, 0o666)
    os.replace(tmp, path)
    return path


def update_settings(config_dir: str, changes: dict) -> str:
    """Merge changes into the file (top-level keys, dicts one level deep)."""
    data = load_settings(config_dir)
    for key, value in (changes or {}).items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            merged = dict(data[key])
            merged.update(value)
            data[key] = merged
        else:
            data[key] = value
    return save_settings(config_dir, data)


def resolve_mcp(config_dir: str, env=None) -> dict:
    """Effective MCP settings: env first, then settings.yaml.

    Returns {'enabled': bool, 'token': str, 'token_source': 'env'|'file'|'',
             'port': int}.
    """
    env = os.environ if env is None else env
    raw = load_settings(config_dir).get("mcp")
    stored = raw if isinstance(raw, dict) else {}

    env_token = str(env.get("MCP_TOKEN") or "").strip()
    file_token = str(stored.get("token") or "").strip()
    token = env_token or file_token
    source = "env" if env_token else ("file" if file_token else "")

    enabled = stored.get("enabled")
    if not isinstance(enabled, bool):
        enabled = bool(token)   # pre-0.7.47 behaviour: token present = on

    try:
        port = int(str(env.get("MCP_PORT") or "").strip() or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT

    return {"enabled": enabled, "token": token, "token_source": source, "port": port}
