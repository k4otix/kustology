# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Pin ``_analyze_guarded``'s contract without importing pydantic.

Tier 1 (:mod:`kustology.services`) has no pydantic dependency, so this module
imports only from it -- a regression that pulled in the IR would show up here
as a collection-time ``ImportError`` on a bare install.
"""

import pytest

from kustology.services import ANALYZE_FAILED_CODE, _analyze_guarded


@pytest.mark.parametrize("exc_type", [MemoryError, RecursionError])
def test_resource_exhaustion_propagates(exc_type):
    def boom():
        raise exc_type()

    with pytest.raises(exc_type):
        _analyze_guarded(boom, lambda: "unbound")


def test_a_binder_crash_keeps_message_short_and_puts_the_trace_in_detail():
    trace = "Index was outside the bounds of the array.\n   at Kusto.Language.Binding.Binder.NodeBinder..."

    def boom():
        raise IndexError(trace)

    code, failure = _analyze_guarded(boom, lambda: "unbound")
    assert code == "unbound"
    assert failure["code"] == ANALYZE_FAILED_CODE
    assert "\n" not in failure["message"] and len(failure["message"]) < 300
    assert failure["detail"] == trace
