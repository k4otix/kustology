# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""The two IR compatibility tags, in a module with no imports at all.

:func:`kustology.build_info` reads them here without pulling pydantic and the
whole IR module graph into a Tier 1-only process; ``_version.py`` is the other
such dependency-free source. ``kustology.ir`` re-exports both, which is the
public spelling.

``IR_SCHEMA_VERSION`` versions the IR shape, separately from the ``kustology``
package version. It moves on breaking field-shape changes, so serialized IR
JSON can carry a version tag (in a wrapper envelope, for example) and a
consumer can refuse an incompatible payload.

``SEMANTIC_HASH_SCHEME`` prefixes every ``semantic_hash`` and versions the
canonicalization rules behind it: the volatile field set, the transforms, and
the dump format. Changing any of the three ships a new tag, so a consumer can
tell a stored hash is stale. It also tags a ``similarity_sketch`` header, built
from those digests.

Both move once per release, in lockstep. One tag covers one unreleased window
however many branches land in it, so bump on the first change after a release;
bumping per branch burns tags nobody saw and gaps the released sequence. Never
reuse a tag for different rules, or a consumer compares hashes computed under
different canonicalizations. Renumbering down into an unreleased window is safe
only while nothing has consumed the intermediate value.

``tests/ir/test_schema_tags.py`` pins both, so an accidental bump fails there.
"""

IR_SCHEMA_VERSION = "0.2"

SEMANTIC_HASH_SCHEME = "kustology-sem-v2"
