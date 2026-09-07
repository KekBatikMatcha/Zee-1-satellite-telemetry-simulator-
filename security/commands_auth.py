"""HMAC-SHA256 command authentication.

Why authentication is required
------------------------------
The space link is untrusted. Anyone able to reach the uplink could inject a
command; a receiver must be able to prove a command came from an authorised
control segment and was not modified in transit.

How the shared secret is used
-----------------------------
* A canonical byte string is built from the command fields (satellite_id,
  command, parameters, timestamp, sequence).
* An HMAC-SHA256 is computed over that canonical message using the shared
  secret (``SATELLITE_COMMAND_SECRET``, supplied via environment/.env).
* Only parties knowing the secret can produce or verify a valid signature.

What HMAC protects
------------------
* Integrity: any modification to the command fields breaks the signature.
* Authentication: a valid signature proves knowledge of the shared secret
  (i.e. that the sender is the control segment).

What HMAC does NOT protect
--------------------------
* Confidentiality: the fields are still readable on the wire by anyone.
* Replay: a captured valid command can be resent; this is why command
  sequence numbers + the replay guard exist (see security/replay.py).
* Key distribution: both ends share the same secret, which must be deployed
  out-of-band and rotated.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from security.commands import CommandRequest, CommandError

AUTH_ALGORITHM = "HMAC-SHA256"
AUTH_FIELD = "auth"


def canonical_bytes(cmd: CommandRequest) -> bytes:
    """Deterministic byte string representing the command (sorted keys)."""
    message = {
        "satellite_id": cmd.satellite_id,
        "command": cmd.command,
        "parameters": cmd.parameters,
        "timestamp": cmd.timestamp,
        "sequence": cmd.sequence,
    }
    return json.dumps(message, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_command(cmd: CommandRequest, secret: str) -> dict[str, Any]:
    """Return the full signed command object ready for the uplink."""
    signature = hmac.new(
        secret.encode("utf-8"), canonical_bytes(cmd), hashlib.sha256
    ).hexdigest()
    signed = cmd.to_dict()
    signed[AUTH_FIELD] = {"algorithm": AUTH_ALGORITHM, "signature": signature}
    return signed


def verify_signature(signed: dict[str, Any], secret: str) -> tuple[bool, str]:
    """Recompute the HMAC over the command fields and compare with the wire.

    Returns (ok, reason). Constant-time comparison avoids timing leaks.
    """
    auth = signed.get(AUTH_FIELD)
    if not isinstance(auth, dict):
        return False, "missing auth block"
    if auth.get("algorithm") != AUTH_ALGORITHM:
        return False, f"unsupported auth algorithm {auth.get('algorithm')!r}"
    received = auth.get("signature")
    if not isinstance(received, str):
        return False, "missing signature"
    try:
        cmd = CommandRequest.from_dict(signed)
    except TypeError as exc:
        return False, f"cannot reconstruct command: {exc}"
    expected = hmac.new(
        secret.encode("utf-8"), canonical_bytes(cmd), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received):
        return False, "signature verification failed"
    return True, "signature OK"