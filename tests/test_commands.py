# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Control-command detection on Tier 1."""

from kustology import parse


def test_a_dotted_command_reports_its_kind():
    q = parse(".drop table Victim")
    assert q.is_command
    assert q.command_kinds == frozenset({"DropTable"})


def test_a_nested_execute_script_reports_both_kinds():
    q = parse(".execute database script <| .drop table X")
    assert q.command_kinds == frozenset({"ExecuteDatabaseScript", "DropTable"})


def test_a_query_is_not_a_command():
    q = parse("T | count")
    assert not q.is_command
    assert q.command_kinds == frozenset()


def test_query_accessors_do_not_describe_a_command():
    """`tables` in `.show tables` is a command argument, not a table reference."""
    q = parse(".show tables | project TableName")
    assert q.is_command
    assert q.command_kinds == frozenset({"ShowTables"})
    assert q.get_referenced_tables() == set()
