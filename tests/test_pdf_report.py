from __future__ import annotations

from dcc_console.execution import TestResult
from dcc_console.pdf_report import _sanitize, generate_cab_pdf


def make_result() -> TestResult:
    return TestResult(
        id="1-test",
        test_key="Bit 1 — DCC Xpress CO (location extra_function)",
        bit=1,
        environment="DEV",
        login="tester",
        target_type="location",
        target="000001",
        value="Add",
        mode="SIMULATION",
        status="PASS",
        error=None,
        state_before="<extra_function/>",
        state_after="<extra_function/>",
        restore_point="<extra_function/>",
        change_persisted=False,
        transaction="ROLLED BACK (simulation)",
        sql="EXEC procedure",
        messages=[],
        grids=[],
        grid_frames=[],
        duration_s=0.1,
        timestamp="2026-01-01T00:00:00",
        input_json='[{"location_no":"000001"}]',
        proc_args=("@location_json", "@add"),
        proposed_change_observed=True,
    )


def test_sanitize_replaces_report_symbols_and_non_latin_text():
    text = _sanitize("Before → after — ✓ • café Ω")
    assert text == "Before -> after -- [v] * café ?"


def test_generate_pdf_returns_hash_and_writes_output(tmp_path):
    output = tmp_path / "report.pdf"
    pdf_bytes, content_hash = generate_cab_pdf(
        [make_result()],
        meta={"environment": "DEV", "server": "sql", "login": "tester"},
        output_path=output,
    )
    assert pdf_bytes.startswith(b"%PDF")
    assert len(content_hash) == 64
    assert output.read_bytes() == pdf_bytes


def test_generate_pdf_hash_changes_with_report_metadata():
    result = make_result()
    first_bytes, first_hash = generate_cab_pdf(
        [result], meta={"environment": "DEV", "generated_at": "2026-01-01T00:00:00"}
    )
    second_bytes, second_hash = generate_cab_pdf(
        [result], meta={"environment": "UAT", "generated_at": "2026-01-01T00:00:00"}
    )
    assert first_bytes != second_bytes
    assert first_hash != second_hash
