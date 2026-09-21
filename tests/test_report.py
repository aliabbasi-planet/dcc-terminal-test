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
    assert "EXEC statement actually issued" in text
    assert "Input JSON payload" in text
    assert "Before / proposed / after evidence" in text
    assert '[{"instance_identifier":"I000000001"}]' in text


def test_section_a_renders_trace_rows_when_captured():
    result = mk(
        trace_status="CAPTURED",
        trace_rows=[{"step": "validate", "message": "instance ok"}],
    )
    text = _report([result])
    assert "Procedure trace (`fnDisplayTrace`) — CAPTURED" in text
    assert "instance ok" in text


def test_section_a_explains_trace_unavailable_rather_than_omitting_it():
    result = mk(
        trace_status="UNAVAILABLE",
        trace_reason="takes parameters this harness cannot supply",
    )
    text = _report([result])
    assert "Procedure trace (`fnDisplayTrace`) — UNAVAILABLE" in text
    assert "attempted, not" in text
    assert "cannot supply" in text


def test_trace_instrumentation_block_states_readability():
    meta = dict(META)
    meta["trace_signature"] = {
        "function": "[db].[fnDisplayTrace]",
        "exists": True,
        "is_table_valued": True,
        "parameter_count": 0,
        "call_sql": "SELECT * FROM [db].[fnDisplayTrace]()",
        "usable": True,
        "unavailable_reason": None,
    }
    text = cab_report([mk()], meta)
    assert "Trace instrumentation" in text
    assert "SELECT * FROM [db].[fnDisplayTrace]()" in text
    assert "**is** detected by the negative validation battery" in text


def test_trace_instrumentation_warns_when_not_readable():
    meta = dict(META)
    meta["trace_signature"] = {
        "function": "[db].[fnDisplayTrace]",
        "exists": True,
        "is_table_valued": True,
        "parameter_count": 1,
        "call_sql": None,
        "usable": False,
        "unavailable_reason": "takes parameters this harness cannot supply automatically",
    }
    text = cab_report([mk()], meta)
    assert "Trace not readable" in text
    assert "cannot be treated as a confirmed validation" in text


def test_section_c_not_rejected_is_inconclusive_when_trace_unreadable():
    neg = mk(
        test_key="Negative — Invalid instance identifier",
        is_negative=True,
        status="REVIEW",
        error=None,
        messages=[],
        trace_status="UNAVAILABLE",
        trace_reason="signature unknown",
    )
    text = _report([mk(), neg])
    assert "NOT-REJECTED" in text
    assert "not yet conclusive" in text


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
