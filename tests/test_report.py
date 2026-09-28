"""CAB report generator output structure and wording guarantees."""

from __future__ import annotations

from dcc_console.execution import TestResult
from dcc_console.report import cab_report


def mk(**overrides) -> TestResult:
    defaults = dict(
        id="x", test_key="Bit 8 — DCC Handler Flags (instance)", bit=8,
        environment="DEV", login="svc", target_type="instance", target="I000000001",
        value="dccEnable", mode="SIMULATION", status="PASS", error=None,
        state_before="<x>1</x>", state_after="<x>1</x>", restore_point="<x>1</x>",
        change_persisted=False, transaction="ROLLED BACK (simulation)",
        sql="EXEC [cccai].[spApplyDCCEnablementConfiguration] @display_config = 8 ...",
        messages=["Trace: proposing dccEnable=true"], grids=[], grid_frames=[],
        duration_s=0.2, timestamp="2026-01-01T00:00:00",
        input_json='[{"instance_identifier":"I000000001"}]',
        proc_args=("@instance_json", "@Extra_Config_Name", "@Config_value", "@is_simulation"),
        proposed_change_observed=True,
    )
    defaults.update(overrides)
    return TestResult(**defaults)


META = {
    "server": "LU3C01DVSQL01", "database": "3CDB", "environment": "DEV",
    "login": "svc-dcc", "mode": "SIMULATION",
    "procedure_version": {
        "object": "[cccai].[spApplyDCCEnablementConfiguration]",
        "object_id": 42, "create_date": "2026-01-01", "modify_date": "2026-09-01",
        "definition_sha256": "deadbeef",
        "definition_sha256_method": "SHA-256 over the UTF-8 bytes of sys.sql_modules.definition",
        "sha256_server_utf16": "CAFEBABE",
        "sha256_server_method": "HASHBYTES('SHA2_256', m.definition) over UTF-16LE bytes",
        "definition_bytes": 12345,
        "can_view_definition": True,
        "is_encrypted": False,
        "unavailable_reason": None,
    },
}


def _report(results):
    return cab_report(results, META)


def test_report_has_all_four_sections_and_legend():
    text = _report([mk()])
    assert "Section A — Procedure execution evidence" in text
    assert "Section B — Coverage matrix" in text
    assert "Section C — Negative validation" in text
    assert "Section D — Capability & limitation analysis" in text
    assert "Verdict legend" in text


def test_report_pins_procedure_version():
    text = _report([mk()])
    assert "Procedure version under test" in text
    assert "deadbeef" in text
    assert "SHA-256 (UTF-8, harness)" in text


def test_report_shows_both_digests_and_states_methods():
    text = _report([mk()])
    assert "CAFEBABE" in text
    assert "SHA-256 (UTF-16LE, server)" in text
    assert "Digest methods" in text
    assert "expected to differ" in text
    assert "12345" in text  # definition_bytes


def test_report_does_not_overclaim_artefact_correspondence():
    text = _report([mk()])
    assert "Correspondence to the deployment artefact" in text
    assert "not** by this test harness" in text


def test_report_explains_why_digest_unavailable():
    meta = dict(META)
    meta["procedure_version"] = dict(META["procedure_version"])
    meta["procedure_version"]["definition_sha256"] = "unavailable"
    meta["procedure_version"]["can_view_definition"] = False
    meta["procedure_version"]["unavailable_reason"] = (
        "The connected login lacks VIEW DEFINITION on the procedure."
    )
    text = cab_report([mk()], meta)
    assert "Definition digest unavailable" in text
    assert "lacks VIEW DEFINITION" in text


def test_section_a_shows_execution_evidence():
    text = _report([mk()])
    assert "Database code exercised" in text
    assert "the `EXEC` actually issued" in text
    assert "Input JSON payload" in text
    assert "Before / applied change / after evidence" in text
    assert '[{"instance_identifier":"I000000001"}]' in text


def test_section_a_shows_verification_and_rollback_sql():
    text = _report([mk()])
    # The catalogue-derived read + compensating UPDATE must appear as real SQL.
    assert "Verification read" in text
    assert "Compensating rollback statement" in text
    assert "UPDATE" in text and "SELECT TOP (1)" in text


def test_section_a_shows_database_message_stream():
    text = _report([mk(messages=["Trace: proposing dccEnable=true"])])
    assert "Database message stream" in text
    assert "Trace: proposing dccEnable=true" in text


def test_sp_managed_report_shows_procedure_rollback_script_as_authoritative():
    result = mk(
        mode="LIVE",
        status="PASS",
        change_persisted=False,
        sp_managed=True,
        sp_rollback_scripts=[
            "UPDATE [cccintegrang].[handler] SET extra_config = '<x/>' WHERE handler = 'h1'"
        ],
    )
    text = _report([result])
    # The procedure's own script is presented as the authoritative rollback...
    assert "the procedure's OWN returned script" in text
    assert "UPDATE [cccintegrang].[handler]" in text
    # ...and the real related-table source is explained.
    assert "Procedure-managed bit" in text
    assert "[cccintegrang].[handler].extra_config" in text


def test_sp_managed_report_shows_real_before_after_and_change_persisted_yes():
    result = mk(
        mode="LIVE",
        status="PASS",
        change_persisted=False,
        sp_managed=True,
        value="dccEnableCompletion",
        sp_rollback_scripts=["UPDATE [cccintegrang].[handler] SET extra_config = '<x/>'"],
        sp_prior_states=[{"handler_name": "handler-A", "flag_value": "false"}],
        sp_after_states=[{"handler_name": "handler-A", "flag_value": "true"}],
        sp_restored_states=[{"handler_name": "handler-A", "flag_value": "false"}],
        sp_flag_verified=True,
        rollback_log=[{"trigger": "manual", "ok": True, "at": "2026-01-01T00:00:00", "rows": 1}],
    )
    text = _report([result])
    # The real handler-level values appear directly in Before/After, not package_config.
    assert "| Before (restore point) | false |" in text
    assert "| After | true |" in text
    # And the change is reported as persisted, since it genuinely changed.
    assert "| Change persisted | yes |" in text
    # The now-redundant separate handler table is gone.
    assert "Handler-level verification" not in text
    assert "✓ **Verified:**" in text


def test_section_a_renders_trace_rows_when_captured():
    result = mk(
        trace_status="CAPTURED",
        trace_rows=[{"line": 1, "message": "instance ok"}],
    )
    text = _report([result])
    assert "Procedure trace — `fnDisplayTrace` PRINT output (CAPTURED)" in text
    assert "instance ok" in text


def test_section_a_explains_empty_trace_rather_than_omitting_it():
    result = mk(
        trace_status="EMPTY",
        trace_reason=None,
    )
    text = _report([result])
    assert "Procedure trace — `fnDisplayTrace` PRINT output (EMPTY)" in text
    assert "no PRINT/trace messages" in text


def test_trace_instrumentation_block_confirms_print_formatter():
    meta = dict(META)
    meta["trace_signature"] = {
        "function": "[db].[fnDisplayTrace]",
        "exists": True,
        "is_scalar": True,
        "parameter_count": 2,
        "confirmed_print_formatter": True,
        "capture_mechanism": "cursor.messages (SQL Server PRINT / info stream)",
        "unavailable_reason": None,
    }
    text = cab_report([mk()], meta)
    assert "Trace instrumentation" in text
    assert "scalar string formatter" in text
    assert "cursor.messages" in text
    assert "still" in text and "negative validation battery" in text


def test_trace_instrumentation_warns_when_not_scalar():
    meta = dict(META)
    meta["trace_signature"] = {
        "function": "[db].[fnDisplayTrace]",
        "exists": True,
        "is_scalar": False,
        "parameter_count": 2,
        "confirmed_print_formatter": False,
        "capture_mechanism": "cursor.messages (SQL Server PRINT / info stream)",
        "unavailable_reason": "exists but is not reported as a scalar function",
    }
    text = cab_report([mk()], meta)
    assert "Formatter not confirmed as scalar" in text
    assert "Message capture still occurs" in text


def test_section_c_not_rejected_is_conclusive_because_stream_is_captured():
    neg = mk(
        test_key="Negative — Invalid instance identifier",
        is_negative=True,
        status="REVIEW",
        error=None,
        messages=[],
        trace_status="EMPTY",
        trace_reason=None,
    )
    text = _report([mk(), neg])
    assert "NOT-REJECTED" in text
    assert "PRINT/message stream is captured on every call" in text


def test_section_c_not_rejected_is_conclusive_when_trace_readable():
    neg = mk(
        test_key="Negative — Invalid instance identifier",
        is_negative=True,
        status="REVIEW",
        error=None,
        messages=[],
        trace_status="EMPTY",
        trace_reason=None,
    )
    text = _report([mk(), neg])
    assert "genuine observation rather than a gap in instrumentation" in text


def test_section_c_shows_trace_sourced_evidence():
    neg = mk(
        test_key="Negative — Invalid instance identifier",
        is_negative=True,
        status="PASS",
        error=None,
        messages=[],
        trace_status="CAPTURED",
        trace_rows=[{"msg": "instance does not exist"}],
    )
    text = _report([mk(), neg])
    assert "REJECTED-AS-EXPECTED" in text
    assert "(from trace)" in text


def test_simulation_wording_never_claims_a_persisted_change():
    text = _report([mk()])
    assert "SIMULATED-OK" in text
    # The disclaimer that distinguishes execution from a persisted change must appear.
    assert "not that a config changed" in text
    assert "nothing was persisted" in text


def test_coverage_matrix_lists_all_thirteen_individual_areas():
    text = _report([mk()])
    for label in (
        "DCCXpressCO",
        "DCCXpressCODT",
        "DCCXpressCOFallback",
        "Config Download Version",
        "Firmware Package",
        "dccEnable",
        "dccEnableAuth",
        "dccEnableCompletion",
        "dccEnableNfc",
        "dccEnableNfcSingleTap",
        "dccEnableRefund",
        "dccFlagsEnabled",
        "DCC Receipt Template",
    ):
        assert label in text


def test_negative_section_renders_negative_rows():
    negative = mk(
        id="n1", test_key="Negative — Invalid instance identifier", target="I000099999",
        status="PASS", error="Error 50000: instance not found", is_negative=True,
    )
    text = _report([mk(), negative])
    assert "REJECTED-AS-EXPECTED" in text
    assert "Invalid instance identifier" in text


def test_section_d_flags_assumptions_for_inferred_areas():
    results = [
        mk(value="dccFlagsEnabled"),
        mk(test_key="Bit 1 — DCC Xpress CO Delayed Terminal (location extra_function)", bit=1,
           target_type="location", target="0000000", value="Add"),
    ]
    text = _report(results)
    assert "Assumptions requiring procedure-owner confirmation" in text
    assert "dccFlagsEnabled" in text


def test_not_rejected_negatives_produce_a_finding():
    neg = mk(
        id="n1", test_key="Negative — Invalid instance identifier", target="I000099999",
        status="REVIEW", error=None, is_negative=True,
    )
    text = _report([mk(), neg])
    assert "NOT-REJECTED" in text
    assert "Finding" in text
    assert "no rejection marker was detected" in text


def test_placeholder_areas_get_disclosure_in_section_d():
    results = [
        mk(test_key="Bit 4 — Firmware Package (terminal)", bit=4, target_type="terminal",
           target="T1", value="DCC_Standard"),
    ]
    text = _report(results)
    assert "placeholder" in text.lower()
    assert "Exercised with placeholder values" in text


def test_missing_version_meta_is_reported_not_crashed():
    text = cab_report([mk()], {"server": "S", "database": "D"})
    assert "Procedure version evidence was not captured" in text
