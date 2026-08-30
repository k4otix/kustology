# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Pins on the two published version tags and on ``HANDLED_*`` validity.

``IR_SCHEMA_VERSION`` and ``SEMANTIC_HASH_SCHEME`` are the compatibility
contract: a consumer that stored IR JSON or a ``semantic_hash`` reads them to
decide whether its cache is still valid. CONTRIBUTING allows one bump per
release, so the failure this file prevents is a silent bump, one nobody
noticed in review. Changing either constant means changing this test in the
same commit.

The second test guards a different failure. ``HANDLED_OPERATOR_KINDS``,
``HANDLED_EXPR_KINDS``, and ``HANDLED_STATEMENT_KINDS`` hold strings compared
against ``str(node.Kind)``, so a typo or a name Kusto.Language never emits
sits in the set forever, claiming coverage the builder does not have. Every
entry must name a real class in ``Kusto.Language.Syntax``.

That check is also what keeps the audit's ``unhandled`` count meaningful.
Many SyntaxKinds are token kinds with no node class, the ``*Keyword`` members
among them; adding one to a handled set would drop it from the count while
leaving the rest of its kind in.
"""

from __future__ import annotations

from kustology.ir import IR_SCHEMA_VERSION, SEMANTIC_HASH_SCHEME, IRBuilder


def test_schema_tags_are_pinned():
    assert IR_SCHEMA_VERSION == "0.2"
    assert SEMANTIC_HASH_SCHEME == "kustology-sem-v2"


def test_handled_kinds_are_real_syntax_classes():
    import Kusto.Language.Syntax as S

    handled = (
        IRBuilder.HANDLED_OPERATOR_KINDS
        | IRBuilder.HANDLED_EXPR_KINDS
        | IRBuilder.HANDLED_STATEMENT_KINDS
    )
    missing = sorted(k for k in handled if not hasattr(S, k))
    assert missing == [], f"not classes in Kusto.Language.Syntax: {missing}"
