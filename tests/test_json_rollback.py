"""Structured rollback contract: per-record originals, no DML, pre-commit verification."""

from __future__ import annotations

import copy
import json
from xml.etree import ElementTree

import pandas as pd
import pytest
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from dcc_console.catalog import TEST_CATALOG
from dcc_console.database import ProcedureOutput
from dcc_console.execution import apply_rollback, run_test
from dcc_console.journal import RESTORE_SP_JSON
from dcc_console.recovery import recover_entry
from dcc_console.rollback import (
    METHOD_PROCEDURE_JSON,
    build_rollback_call,
    capture_rollback_plan,
    restore_via_payload,
    validate_plan,
)

DEFINITIONS = {d.bit: d for d in TEST_CATALOG.values()}


class Sql:
    """Models the documented SP interface; assertions reject any direct write."""

    server = "uat-sql"
    database = "3CDB"
    connection = object()

    def __init__(self, bit, originals):
        self.bit = bit
        self.rows = copy.deepcopy(originals)
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.return_code = 0
        self.fail_handler = None
        self.no_op = False
        self.unreadable = False
        self.journal = None
        self.pending_before_call = None

    def scalar(self, sql, params=()):
        if "[ccc].[extra_function]" in sql:
            return "51"
        if "package_config" in sql:
            return "<context/>"
        return self.rows[0]["original"]

    def query(self, sql, params=()):
        if self.unreadable:
            raise RuntimeError("read denied")
        if self.bit in (1, 2):
            return pd.DataFrame([[row["original"]] for row in self.rows], dtype=object)
        column = "receipt_config" if "receipt_config" in sql else "extra_config"
        return pd.DataFrame(
            [
                {
                    "handler_id": row["handler_id"],
                    "handler_name": f"h{row['handler_id']}",
                    column: row["original"],
                }
                for row in self.rows
            ],
            dtype=object,
        )

    def execute_write(self, *args, **kwargs):
        raise AssertionError("Direct DML must not be used.")

    def execute_batch(self, *args, **kwargs):
        raise AssertionError("Returned SQL must not be executed.")

    def safe_rollback(self):
        self.rollbacks += 1

    def call_procedure(self, sql, params, rollback, validate=None):
        self.calls.append((sql, params))
        if self.journal:
            self.pending_before_call = self.journal.pending_entries()
        before = copy.deepcopy(self.rows)
        try:
            if "@is_rollback = 1" in sql:
                for row, requested in zip(self.rows, json.loads(params[1]), strict=True):
                    if self.fail_handler is not None and row.get("handler_id") == self.fail_handler:
                        raise RuntimeError("second handler failed")
                    if self.no_op:
                        continue
                    value = requested["object_value"]
                    value = None if value == "NULL" else value
                    if self.bit == 2:
                        row["original"] = value
                    else:
                        tag = (
                            "extra_function"
                            if self.bit == 1
                            else ("extra_config" if self.bit == 8 else "printout_type_template_dcc")
                        )
                        root_tag = "receipt_config" if self.bit == 16 else tag
                        root = fromstring(row["original"] or f"<{root_tag}/>")
                        for child in list(root):
                            if (
                                (self.bit == 1 and child.get("extra_function_id") == "51")
                                or (self.bit == 8 and child.get("config_name") == params[2])
                                or (self.bit == 16 and child.tag == tag)
                            ):
                                root.remove(child)
                        if self.bit == 1 and value == "1":
                            ElementTree.SubElement(root, tag, {"extra_function_id": "51"})
                        elif self.bit == 8 and value is not None:
                            ElementTree.SubElement(
                                root,
                                tag,
                                {
                                    "config_name": params[2],
                                    "config_value": "true" if value == "1" else "false",
                                },
                            )
                        elif self.bit == 16 and value is not None:
                            ElementTree.SubElement(root, tag).text = value
                        row["original"] = ElementTree.tostring(root, encoding="unicode")
            elif not rollback:
                if self.bit == 2:
                    self.rows[0]["original"] = "2"
                elif self.bit == 1:
                    self.rows[0]["original"] = (
                        '<extra_function><extra_function extra_function_id="51"/></extra_function>'
                    )
                elif self.bit == 8:
                    for row in self.rows:
                        row["original"] = (
                            '<extra_config><extra_config config_name="dccEnable" '
                            'config_value="true"/></extra_config>'
                        )
                elif self.bit == 16:
                    for row in self.rows:
                        row["original"] = (
                            "<receipt_config><printout_type_template_dcc>PlanetEurope"
                            "</printout_type_template_dcc></receipt_config>"
                        )
                else:
                    raise AssertionError("Unsupported apply.")
            output = ProcedureOutput([], ["completed"], self.return_code)
            if validate:
                validate(output)
            if rollback:
                self.rows = before
                self.rollbacks += 1
            else:
                self.commits += 1
            return output
        except Exception:
            self.rows = before
            self.rollbacks += 1
            raise


def originals(bit):
    if bit == 1:
        return [{"location_no": "000216", "original": "<extra_function/>"}]
    if bit == 2:
        return [{"terminal_identifier": "99990097", "original": "1"}]
    if bit == 8:
        return [
            {
                "handler_id": "292",
                "original": '<extra_config><extra_config config_name="dccEnable" '
                'config_value="false"/></extra_config>',
            },
            {"handler_id": "6521", "original": "<extra_config/>"},
        ]
    return [
        {"handler_id": "292", "original": "<receipt_config/>"},
        {
            "handler_id": "6521",
            "original": "<receipt_config><printout_type_template_dcc>ElavonEurope"
            "</printout_type_template_dcc></receipt_config>",
        },
        {
            "handler_id": "9502",
            "original": "<receipt_config><printout_type_template_dcc>PlanetMexico_ING_5000"
            "</printout_type_template_dcc></receipt_config>",
        },
    ]


def snapshot(bit):
    sql = Sql(bit, originals(bit))
    target = {1: "000216", 2: "99990097", 8: "I1", 16: "I1"}[bit]
    value = {1: "Add", 2: "ECB DCC", 8: "dccEnable", 16: "Add:PlanetEurope"}[bit]
    plan = capture_rollback_plan(sql, DEFINITIONS[bit], target, value)
    return sql, plan


@pytest.mark.parametrize("bit", [1, 2, 8, 16])
def test_sp_contract_binds_originals_and_verifies_before_commit(bit):
    sql, plan = snapshot(bit)
    statement, params, payload = build_rollback_call(plan)
    assert statement.count("?") == len(params)
    assert "@is_rollback = 1" in statement and "@is_simulation = 0" in statement
    assert "UPDATE " not in statement
    assert plan["target"] not in statement
    assert json.loads(payload)[0]["object_value"] == {1: "0", 2: "1", 8: "0", 16: "NULL"}[bit]
    sql.call_procedure("apply", (bit,), rollback=False)
    sql.commits = 0
    outcome = restore_via_payload(sql, plan, "manual")
    assert outcome.ok, outcome.error
    assert outcome.method == METHOD_PROCEDURE_JSON
    assert outcome.return_code == 0
    assert outcome.rows == len(plan["records"])
    assert sql.commits == 1
    assert outcome.payload == payload


def test_bit16_captures_different_templates_by_handler_id_not_instance():
    _, plan = snapshot(16)
    _, params, _ = build_rollback_call(plan)
    assert json.loads(params[1]) == [
        {"handler_id": "292", "object_value": "NULL"},
        {"handler_id": "6521", "object_value": "ElavonEurope"},
        {"handler_id": "9502", "object_value": "PlanetMexico_ING_5000"},
    ]


@pytest.mark.parametrize("bit", [2, 8, 16])
def test_null_original_is_explicit_not_a_failed_read(bit):
    sql, plan = snapshot(bit)
    for row in sql.rows:
        row["original"] = None
    plan = capture_rollback_plan(sql, DEFINITIONS[bit], plan["target"], plan["field"])
    assert all(row["object_value"] is None for row in plan["records"])
    assert all(row["object_value"] == "NULL" for row in json.loads(build_rollback_call(plan)[2]))
    sql.call_procedure("apply", (bit,), rollback=False)
    sql.commits = 0
    applied = copy.deepcopy(sql.rows)
    outcome = restore_via_payload(sql, plan, "manual")
    if bit == 2:
        assert outcome.ok
    else:
        # This fake SP leaves an empty XML root rather than restoring SQL NULL.
        assert not outcome.ok
        assert "not restored exactly" in outcome.error
        assert sql.commits == 0
        assert sql.rows == applied


def test_empty_template_is_distinct_from_missing_template():
    sql, plan = snapshot(16)
    sql.rows[0]["original"] = "<receipt_config><printout_type_template_dcc/></receipt_config>"
    plan = capture_rollback_plan(sql, DEFINITIONS[16], "I1", "Add:X")
    assert plan["records"][0]["object_value"] == ""
    assert restore_via_payload(sql, plan, "manual").ok


@pytest.mark.parametrize("code", [None, 1, -1])
def test_missing_or_failed_return_code_never_commits(code):
    sql, plan = snapshot(2)
    sql.return_code = code
    outcome = restore_via_payload(sql, plan, "manual")
    assert not outcome.ok and "expected success code 0" in outcome.error
    assert sql.commits == 0


def test_failed_second_handler_rolls_back_the_whole_operation():
    sql, plan = snapshot(16)
    sql.rows[0]["original"] = (
        "<receipt_config><printout_type_template_dcc>New</printout_type_template_dcc>"
        "</receipt_config>"
    )
    before = copy.deepcopy(sql.rows)
    sql.fail_handler = "6521"
    outcome = restore_via_payload(sql, plan, "campaign")
    assert not outcome.ok
    assert sql.rows == before
    assert sql.commits == 0


def test_success_code_without_restoring_values_is_failure_and_not_committed():
    sql, plan = snapshot(2)
    sql.rows[0]["original"] = "2"
    sql.no_op = True
    outcome = restore_via_payload(sql, plan, "manual")
    assert not outcome.ok
    assert sql.rows[0]["original"] == "2"
    assert sql.commits == 0


def test_changed_handler_scope_never_calls_sp():
    sql, plan = snapshot(16)
    sql.rows.pop()
    outcome = restore_via_payload(sql, plan, "manual")
    assert not outcome.ok and "identifiers" in outcome.error
    assert sql.calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(operation="UPDATE"),
        lambda p: p.update(bit=4),
        lambda p: p.update(field="receipt_config; DROP TABLE x"),
        lambda p: p.update(target="I1'; DROP TABLE x"),
        lambda p: p.update(sql="UPDATE handler SET x=1"),
        lambda p: p["records"][0].update(sql="UPDATE handler SET x=1"),
        lambda p: p["records"][0].update(handler_id="1 OR 1=1"),
        lambda p: p["records"].append(p["records"][0]),
    ],
)
def test_rejects_unknown_fields_identifiers_and_sql(mutation):
    sql, plan = snapshot(16)
    mutation(plan)
    outcome = restore_via_payload(sql, plan, "manual")
    assert not outcome.ok and sql.calls == []


def test_template_content_is_bound_data_not_executable_sql():
    sql, _ = snapshot(16)
    text = "X'; UPDATE users SET a=1; --"
    sql.rows[0]["original"] = (
        "<receipt_config><printout_type_template_dcc>"
        f"{text}</printout_type_template_dcc></receipt_config>"
    )
    plan = capture_rollback_plan(sql, DEFINITIONS[16], "I1", "Add:X")
    statement, _, payload = build_rollback_call(plan)
    assert text not in statement and text in payload


def test_invalid_xml_and_null_sentinel_collision_cannot_capture_a_plan():
    sql, _ = snapshot(16)
    for document in (
        "<receipt_config>",
        "<receipt_config><printout_type_template_dcc>NULL</printout_type_template_dcc>"
        "</receipt_config>",
        '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///not-read">]><receipt_config>&e;</receipt_config>',
    ):
        sql.rows[0]["original"] = document
        with pytest.raises((ValueError, ElementTree.ParseError, DefusedXmlException)):
            capture_rollback_plan(sql, DEFINITIONS[16], "I1", "Add:X")


def test_missing_and_unreadable_targets_are_not_mistaken_for_sql_null():
    sql, _ = snapshot(2)
    sql.rows = []
    with pytest.raises(ValueError, match="exactly one"):
        capture_rollback_plan(sql, DEFINITIONS[2], "T1", "ECB DCC")
    sql.unreadable = True
    with pytest.raises(RuntimeError, match="read denied"):
        capture_rollback_plan(sql, DEFINITIONS[2], "T1", "ECB DCC")


def test_rollback_preserves_unrelated_subsequent_xml_fixes():
    sql, plan = snapshot(8)
    sql.rows[0]["original"] = sql.rows[0]["original"].replace(
        "</extra_config>", '<entry config_name="otherFlag" config_value="true"/></extra_config>'
    )
    assert restore_via_payload(sql, plan, "manual").ok
    assert "otherFlag" in sql.rows[0]["original"]


@pytest.fixture
def journal(tmp_path, monkeypatch):
    import dcc_console.journal as journal_module

    monkeypatch.setattr(journal_module, "_JOURNAL_DIR", tmp_path)
    monkeypatch.setattr(journal_module, "_JOURNAL_DB", tmp_path / "journal.db")
    instance = journal_module.RestoreJournal()
    monkeypatch.setattr("dcc_console.execution.get_journal", lambda: instance)
    yield instance
    instance.close()


def test_live_apply_persists_plan_before_call_then_recovers_after_restart(journal):
    sql, _ = snapshot(2)
    sql.journal = journal
    result = run_test(sql, DEFINITIONS[2], "99990097", "ECB DCC", False, "UAT", "user")
    assert result.status == "PASS", result.error
    saved = sql.pending_before_call[0]
    assert saved["restore_kind"] == RESTORE_SP_JSON
    assert json.loads(saved["restore_sql"]) == result.rollback_plan
    assert result.rollback_available
    outcome = recover_entry(saved, sql, "UAT", journal)
    assert outcome.ok, outcome.message
    assert sql.rows[0]["original"] == "1"
    assert journal.get_entry(saved["id"])["status"] == "RESOLVED"


@pytest.mark.parametrize("trigger", ["manual", "campaign", "bulk", "automatic"])
def test_shared_apply_rollback_marks_verified_and_prevents_replay(journal, trigger):
    sql, _ = snapshot(2)
    result = run_test(sql, DEFINITIONS[2], "99990097", "ECB DCC", False, "UAT", "user")
    outcome = apply_rollback(sql, DEFINITIONS[2], result, trigger=trigger)
    assert outcome.ok, outcome.error
    assert not result.rollback_available
    assert result.sp_rolled_back
    assert journal.get_entry(result.journal_id)["status"] == "RESOLVED"
    count = len(sql.calls)
    assert not apply_rollback(sql, DEFINITIONS[2], result).ok
    assert len(sql.calls) == count


def test_simulation_and_missing_snapshot_never_write_or_offer_rollback(journal):
    sql, _ = snapshot(2)
    result = run_test(sql, DEFINITIONS[2], "99990097", "ECB DCC", True, "UAT", "user")
    assert not result.rollback_available
    assert not apply_rollback(sql, DEFINITIONS[2], result).ok
    assert journal.pending_entries() == []
    sql.rows = []
    sql.scalar = lambda *args: None
    count = len(sql.calls)
    result = run_test(sql, DEFINITIONS[2], "missing", "ECB DCC", False, "UAT", "user")
    assert result.status == "FAIL" and result.rollback_capture_error
    assert len(sql.calls) == count


def test_journal_and_registry_json_are_not_truncated(journal):
    sql, plan = snapshot(16)
    plan["originals"][0]["original"] = "<receipt_config>" + " " * 17000 + "</receipt_config>"
    validate_plan(plan)
    identifier = journal.record_restore_point(
        environment="UAT",
        server=sql.server,
        database_name=sql.database,
        login="user",
        test_key=DEFINITIONS[16].key,
        bit=16,
        target_type="instance",
        target="I1",
        config_value="Add:X",
        original_value=None,
        restore_sql="",
        rollback_plan=plan,
    )
    assert json.loads(journal.get_entry(identifier)["restore_sql"]) == plan


@pytest.mark.parametrize("bit", [1, 2, 8, 16])
def test_live_full_cycle_and_evidence_for_every_supported_bit(journal, bit):
    from dcc_console.execution import run_rollback_evidence_test

    sql, plan = snapshot(bit)
    value = {1: "Add", 2: "ECB DCC", 8: "dccEnable", 16: "Add:PlanetEurope"}[bit]
    evidence = run_rollback_evidence_test(
        sql, DEFINITIONS[bit], plan["target"], value, "UAT", "user"
    )
    assert evidence.change_detected
    assert evidence.rollback_successful, evidence.error
    assert evidence.full_cycle_proven
    assert not evidence.live_result.rollback_available
    assert journal.get_entry(evidence.live_result.journal_id)["status"] == "RESOLVED"
    assert len(sql.calls) == 2
    assert "@is_rollback = 0" in sql.calls[0][0]
    assert "@is_rollback = 1" in sql.calls[1][0]


@pytest.mark.parametrize("bit", [8, 16])
def test_handler_originals_are_journaled_before_live_apply(journal, bit):
    sql, plan = snapshot(bit)
    sql.journal = journal
    value = "dccEnable" if bit == 8 else "Add:PlanetEurope"
    result = run_test(sql, DEFINITIONS[bit], plan["target"], value, False, "UAT", "user")
    saved = sql.pending_before_call[0]
    assert json.loads(saved["restore_sql"]) == plan
    assert result.status == "PASS", result.error
    outcome = recover_entry(saved, sql, "UAT", journal)
    assert outcome.ok, outcome.message
    assert journal.get_entry(result.journal_id)["status"] == "RESOLVED"


def test_rollback_refuses_another_server_without_a_write(journal):
    sql, plan = snapshot(2)
    result = run_test(sql, DEFINITIONS[2], plan["target"], "ECB DCC", False, "UAT", "user")
    sql.server = "prod-sql"
    count = len(sql.calls)
    outcome = apply_rollback(sql, DEFINITIONS[2], result)
    assert not outcome.ok and "original server/database" in outcome.error
    assert len(sql.calls) == count


def test_corrupted_values_are_refused_before_calling_sp():
    sql, plan = snapshot(16)
    plan["records"][1]["object_value"] = "WrongOriginal"
    outcome = restore_via_payload(sql, plan, "manual")
    assert not outcome.ok and "captured original evidence" in outcome.error
    assert sql.calls == []


@pytest.mark.parametrize("bit", [2, 8, 16])
def test_restore_does_not_require_current_values_to_be_representable(bit):
    sql, plan = snapshot(bit)
    sql.rows[0]["original"] = {
        2: "3",
        8: '<extra_config><extra_config config_name="dccEnable" '
        'config_value="invalid"/></extra_config>',
        16: "<receipt_config><printout_type_template_dcc>NULL"
        "</printout_type_template_dcc></receipt_config>",
    }[bit]
    outcome = restore_via_payload(sql, plan, "manual")
    assert outcome.ok, outcome.error
    assert sql.commits == 1


def test_verified_snapshot_not_best_effort_context_read_drives_evidence(journal, monkeypatch):
    sql, plan = snapshot(2)
    monkeypatch.setattr(sql, "scalar", lambda *args, **kwargs: None)
    result = run_test(sql, DEFINITIONS[2], plan["target"], "ECB DCC", False, "UAT", "user")
    assert result.state_before == "1"
    assert result.status == "PASS"
    outcome = apply_rollback(sql, DEFINITIONS[2], result)
    assert outcome.ok, outcome.error
    assert result.state_after == "1"


def test_duplicate_handler_names_do_not_merge_bit16_evidence(journal, monkeypatch):
    sql, plan = snapshot(16)
    query = sql.query

    def same_names(*args, **kwargs):
        frame = query(*args, **kwargs)
        frame["handler_name"] = "same-name"
        return frame

    monkeypatch.setattr(sql, "query", same_names)
    result = run_test(
        sql, DEFINITIONS[16], plan["target"], "Add:PlanetEurope", False, "UAT", "user"
    )
    assert len(result.sp_flag_rows) == 3
    assert {r["handler_id"] for r in result.sp_flag_rows} == {"292", "6521", "9502"}
    assert apply_rollback(sql, DEFINITIONS[16], result).ok
    assert len(result.sp_flag_rows) == 3
    assert "handler_id=6521" in result.sp_display_after
