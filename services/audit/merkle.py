"""Merkle tree over a batch of audit events.

The existing hash chain (services/audit/hash_chain.py) gives per-event
tamper evidence — each event references the prior event's hash, so any
mutation breaks the chain at that point.

The Merkle root adds a single short hash that summarizes an entire
period's events. When signed by a Cloud KMS HSM (Validated mode), it
becomes a periodically-anchored, non-repudiable commitment that the
chain at a specific point in time held a specific set of events.

Construction: standard binary Merkle tree with SHA-256.
- Leaf = sha256(`leaf:` || event.this_event_hash) — domain-separated
  from interior nodes so the tree can't be confused with a longer
  flat hash.
- Internal node = sha256(`node:` || left || right)
- For an odd number of nodes at any level, the last node is duplicated
  (the convention Bitcoin uses; collision-safe for our purposes).

The root is 32 bytes; we hex-encode it for storage.
"""

from __future__ import annotations

import hashlib


_LEAF_PREFIX = b"leaf:"
_NODE_PREFIX = b"node:"


def leaf_hash(event_chain_hash: str) -> bytes:
    """Compute the leaf hash for an event, given its `this_event_hash`."""
    h = hashlib.sha256()
    h.update(_LEAF_PREFIX)
    h.update(event_chain_hash.encode("ascii"))
    return h.digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    h = hashlib.sha256()
    h.update(_NODE_PREFIX)
    h.update(left)
    h.update(right)
    return h.digest()


def compute_root(event_chain_hashes: list[str]) -> bytes:
    """Compute the Merkle root over an ordered list of event chain hashes.

    Raises ValueError on an empty input (there's no meaningful root for
    zero events).
    """
    if not event_chain_hashes:
        raise ValueError("cannot compute Merkle root over zero events")
    level: list[bytes] = [leaf_hash(h) for h in event_chain_hashes]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])  # duplicate the last node
        next_level: list[bytes] = []
        for i in range(0, len(level), 2):
            next_level.append(_node_hash(level[i], level[i + 1]))
        level = next_level
    return level[0]


def compute_root_hex(event_chain_hashes: list[str]) -> str:
    return compute_root(event_chain_hashes).hex()
