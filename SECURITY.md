# Security Policy

## Reporting a vulnerability

Please report security issues privately via GitHub's **private vulnerability
reporting** (Repository → Security → *Report a vulnerability*), or — if that
is unavailable — open a GitHub issue **without** posting exploit details
publicly.

Include, if possible:

- affected version (VERSION file / image tag)
- a short description of the issue
- steps to reproduce

## Known limitations

- Traffic between dashboard and agents is **plain HTTP** (single shared token,
  no TLS). Do **not** expose it outside a trusted local network.
- See the Security section in the README for the full threat model.
