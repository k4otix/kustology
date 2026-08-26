# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Analysis and traversal helpers for the Kusto.Language AST.

:mod:`~kustology.utils.analysis` holds the analyzers,
:mod:`~kustology.utils.schema_state` the schema-to-``GlobalState``
translation, and :mod:`~kustology.utils.walker` the traversal primitives.
Only :func:`node_name` and :func:`node_text` are re-exported here.
"""

from .walker import node_name, node_text

__all__ = ["node_name", "node_text"]
