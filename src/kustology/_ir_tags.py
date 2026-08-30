# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""The two IR compatibility tags, in a module with no imports at all.

They live here rather than in ``kustology.ir`` so :func:`kustology.build_info`
can report them without pulling pydantic and the whole IR module graph into a
Tier 1-only process. Both are re-exported from ``kustology.ir``, which is the
public spelling: ``kustology.ir.IR_SCHEMA_VERSION`` and
``kustology.ir.SEMANTIC_HASH_SCHEME``. This is the counterpart of
``_version.py`` -- the two dependency-free single-source modules ``build_info``
reads.

``IR_SCHEMA_VERSION`` is the IR shape's own version, distinct from the
``kustology`` package version. It moves on breaking field-shape changes, so
serialized IR JSON can carry a version tag (for example, via a wrapper
envelope) and consumers can refuse to load an incompatible payload.

``SEMANTIC_HASH_SCHEME`` prefixes every ``semantic_hash`` and declares the
version of the canonicalization rules behind it (volatile field set +
transforms + dump format), so a future change ships a new tag instead of
silently invalidating stored hashes. It also tags a ``similarity_sketch``
header, since a sketch is only as durable as the digests under it.

Both move once per release, in lockstep, and neither is bumped per branch:
one tag covers one unreleased window, however many branches land in it.
Bumping per branch would burn tags nobody ever saw and leave gaps in the
released sequence that a later reader has to go digging to explain. Bump on
the first change *after* a release, not on every change.

The one thing never to do is reuse a tag for different rules: a stored hash
whose prefix stops implying its canonicalization is exactly the silent wrong
answer the prefix exists to prevent. Renumbering down into an unreleased
window is only safe while nothing has consumed the intermediate value.

``tests/ir/test_schema_tags.py`` pins both, so an accidental bump fails there.
"""

IR_SCHEMA_VERSION = "0.2"

SEMANTIC_HASH_SCHEME = "kustology-sem-v2"
