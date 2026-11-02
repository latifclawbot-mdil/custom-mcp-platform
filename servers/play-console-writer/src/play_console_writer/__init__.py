"""Isolated non-financial Google Play Console write MCP server.

This package is a *separate* server from the read-only ``play_console_mcp``.
It exposes a fixed, named set of non-financial write operations that map to real
Android Publisher v3 endpoints. There is no generic "call any endpoint" tool:
every operation carries its own hardcoded path template, its own typed JSON
schema (``additionalProperties: false``), and its own validation.

Design invariants enforced in code and asserted in tests:

* **Explicit method allowlist.** :data:`ALLOWED_WRITE_METHODS` is the only set of
  upstream methods reachable. It contains no financial method: no price setter,
  no refund, no purchase acknowledgement/consumption/cancellation, no
  activate/deactivate/archive of a purchase option, no ``users.*`` / ``grants.*``
  (which can escalate to financial permissions), and no
  ``externaltransactions.createexternaltransaction`` (financial bookkeeping).
* **No generic request escape hatch.** :meth:`GoogleWriteClient.request` takes a
  method *id* that must be in the allowlist; it never accepts a caller-supplied
  path or verb.
* **Fixed host and package scope.** Every call goes to
  ``https://androidpublisher.googleapis.com`` and every package name is
  validated against a mandatory allowlist that fails closed.
* **No auto-retry of unsafe writes.** Only read-back verification GETs are
  retried on 429/5xx; staged writes are single-shot.
"""

__version__ = "0.1.0"