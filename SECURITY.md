# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security vulnerabilities. Instead,
report privately through GitHub's
["Report a vulnerability"](https://github.com/AgLyx3/jupyter-notebook-ai-adapter/security/advisories/new)
flow, or email the maintainer at yixinli.a@gmail.com.

Include steps to reproduce and the potential impact. You can expect an initial
acknowledgement within a few days.

## Scope and threat model

This is a **local, single-user development tool** that binds to loopback and
keeps one active notebook in process memory. It is **not** an operating-system
sandbox:

- The Claude CLI and any executed notebook code run with the current user's
  permissions and can read files, use credentials, access the network, or start
  processes.
- Risk classification is heuristic; approval does not make code safe.
- Agent write permission is turn-scoped and enforced at the workspace boundary,
  but context selection is an attention signal, not a confidentiality control.

See the "Security Limits" section of the [README](README.md) for the full
description. Use the editor only with notebooks and agent instructions you trust.
