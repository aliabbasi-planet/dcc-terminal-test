"""Generate a signed Word (.docx) version of the CAB report.

Design goals (from the CAB request):

* **Correct tables** — the report body is rendered with *native* Word tables,
  not monospace text dumps, so it reads as a proper document.
* **Signature / authenticity** — the document embeds the SHA-256 of the exact
  same canonical Markdown the PDF and Markdown exports use. A reviewer verifies
  authenticity by regenerating the report from the same session data and
  comparing digests. This is a content-integrity guarantee, not PKI signing.
* **Protected automatic content, editable manual attachments** — the automatic
  sections are placed under Word "Restrict Editing" (read-only enforcement) and
  a trailing *Manual Attachments* region is marked as an editable range so the
  team can add subsections by hand without touching the signed body.

Honest limitation (also stated inside the document): Word's read-only
enforcement without a strong password is a **deterrent**, not tamper-proofing —
it can be removed by a determined editor. The real authenticity guarantee is the
embedded SHA-256, which changes if any automatic content is altered.
"""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from .execution import TestResult
from .report import cab_report

_MONO_FONT = "Consolas"
_CODE_SHADING = "F2F2F2"
_PERM_RANGE_ID = "1"


# ---------------------------------------------------------------------------
# low-level helpers
# ---------------------------------------------------------------------------


def _shade_paragraph(paragraph, fill: str) -> None:
    """Apply a solid background shading to a paragraph."""
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    paragraph.paragraph_format.element.get_or_add_pPr().append(shd)


def _add_rich_runs(paragraph, text: str) -> None:
    """Add runs to *paragraph*, honouring inline ``**bold**`` and ``code``."""
    # Split into segments while keeping the delimiters we care about.
    token = ""
    mode_bold = False
    mode_code = False
    i = 0
    while i < len(text):
        if text.startswith("**", i):
            if token:
                _emit_run(paragraph, token, mode_bold, mode_code)
                token = ""
            mode_bold = not mode_bold
            i += 2
            continue
        if text[i] == "`":
            if token:
                _emit_run(paragraph, token, mode_bold, mode_code)
                token = ""
            mode_code = not mode_code
            i += 1
            continue
        token += text[i]
        i += 1
    if token:
        _emit_run(paragraph, token, mode_bold, mode_code)


def _emit_run(paragraph, text: str, bold: bool, code: bool) -> None:
    run = paragraph.add_run(text)
    run.bold = bold
    if code:
        run.font.name = _MONO_FONT
        run.font.size = Pt(9)


def _unescape_cell(text: str) -> str:
    return text.replace("\\|", "|").replace("`", "").strip()


# ---------------------------------------------------------------------------
# Markdown -> docx rendering
# ---------------------------------------------------------------------------


def _flush_table(document, rows: list[list[str]]) -> None:
    """Render collected Markdown table rows as a native Word table."""
    if not rows:
        return
    header, *body = rows
    table = document.add_table(rows=1, cols=len(header))
    table.style = "Light Grid Accent 1"
    for idx, cell_text in enumerate(header):
        cell = table.rows[0].cells[idx]
        cell.text = ""
        _add_rich_runs(cell.paragraphs[0], cell_text)
        for run in cell.paragraphs[0].runs:
            run.bold = True
    for row in body:
        cells = table.add_row().cells
        for idx in range(len(header)):
            value = row[idx] if idx < len(row) else ""
            cells[idx].text = ""
            _add_rich_runs(cells[idx].paragraphs[0], value)
    document.add_paragraph()


def _flush_code(document, code_lines: list[str]) -> None:
    if not code_lines:
        return
    paragraph = document.add_paragraph()
    _shade_paragraph(paragraph, _CODE_SHADING)
    run = paragraph.add_run("\n".join(code_lines))
    run.font.name = _MONO_FONT
    run.font.size = Pt(9)


def _parse_table_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [_unescape_cell(c) for c in cells]


def _render_markdown(document, markdown: str) -> None:
    """Render the canonical CAB Markdown into the Word document."""
    table_rows: list[list[str]] = []
    code_lines: list[str] = []
    in_code = False

    def flush() -> None:
        nonlocal table_rows
        if table_rows:
            _flush_table(document, table_rows)
            table_rows = []

    for raw in markdown.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                _flush_code(document, code_lines)
                code_lines = []
                in_code = False
            else:
                flush()
                in_code = True
            continue
        if in_code:
            code_lines.append(raw)
            continue

        # Table rows accumulate until a non-table line appears.
        if stripped.startswith("|"):
            if set(stripped) <= {"|", "-", " ", ":"}:
                continue  # separator row
            table_rows.append(_parse_table_row(stripped))
            continue
        flush()

        if not stripped:
            continue
        if stripped.startswith("# ") and not stripped.startswith("## "):
            document.add_heading(stripped[2:], level=0)
        elif stripped.startswith("## "):
            document.add_heading(stripped[3:], level=1)
        elif stripped.startswith("### "):
            document.add_heading(stripped[4:], level=2)
        elif stripped == "---":
            document.add_paragraph()
        elif stripped.startswith("- "):
            paragraph = document.add_paragraph(style="List Bullet")
            _add_rich_runs(paragraph, stripped[2:])
        elif stripped.startswith("_") and stripped.endswith("_") and len(stripped) > 2:
            paragraph = document.add_paragraph()
            run = paragraph.add_run(stripped.strip("_"))
            run.italic = True
        else:
            paragraph = document.add_paragraph()
            _add_rich_runs(paragraph, stripped)

    flush()
    if in_code:
        _flush_code(document, code_lines)


# ---------------------------------------------------------------------------
# protection + manual attachments
# ---------------------------------------------------------------------------


def _enforce_read_only(document) -> None:
    """Turn on Word 'Restrict Editing' read-only enforcement (deterrent only)."""
    settings = document.settings.element
    for existing in settings.findall(qn("w:documentProtection")):
        settings.remove(existing)
    protection = OxmlElement("w:documentProtection")
    protection.set(qn("w:edit"), "readOnly")
    protection.set(qn("w:enforcement"), "1")
    settings.append(protection)


def _perm_element(tag: str):
    element = OxmlElement(tag)
    element.set(qn("w:id"), _PERM_RANGE_ID)
    if tag == "w:permStart":
        element.set(qn("w:edGrp"), "everyone")
    return element


def _add_manual_attachments(document) -> None:
    """Append the editable Manual Attachments region (outside the signed body)."""
    document.add_heading(
        "Manual Attachments (editable, not covered by the signature)", level=1
    )

    note = document.add_paragraph()
    run = note.add_run(
        "Everything ABOVE this heading is the automatically generated, signed report and "
        "is protected read-only. The SHA-256 on the cover verifies ONLY that automatic "
        "content. This Manual Attachments region is an editable range: add supporting "
        "subsections by hand below. Content added here is intentionally outside the "
        "signature and does not affect the digest."
    )
    run.italic = True

    # Mark the start of the editable range immediately after the note.
    note._p.addnext(_perm_element("w:permStart"))

    subsections = [
        (
            "A. Change record / ticket references",
            "Paste the CAB ticket, change number and approvers here.",
        ),
        (
            "B. Manual test notes",
            "Record any manual verification performed outside this tool.",
        ),
        (
            "C. Supporting screenshots / attachments",
            "Embed screenshots or reference attached files here.",
        ),
        ("D. Reviewer sign-off", "Name, role, date and decision."),
    ]
    last = note
    for title, hint in subsections:
        document.add_heading(title, level=2)
        placeholder = document.add_paragraph()
        placeholder_run = placeholder.add_run(hint)
        placeholder_run.italic = True
        placeholder_run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
        last = placeholder

    # Close the editable range after the final placeholder.
    last._p.addnext(_perm_element("w:permEnd"))


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def _cover(document, meta: dict, content_hash: str) -> None:
    title = document.add_heading("DCC Enablement Configuration", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle = document.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub_run = subtitle.add_run("CAB Validation Report")
    sub_run.bold = True
    sub_run.font.size = Pt(14)

    facts = document.add_paragraph()
    facts.alignment = WD_ALIGN_PARAGRAPH.CENTER
    facts.add_run(
        f"Environment: {meta.get('environment', '?')}    Server: {meta.get('server', '?')}\n"
        f"Generated: {meta.get('generated_at', '?')}    Login: {meta.get('login', '?')}"
    )

    integrity = document.add_paragraph()
    _shade_paragraph(integrity, _CODE_SHADING)
    run = integrity.add_run(f"Report integrity SHA-256: {content_hash}")
    run.font.name = _MONO_FONT
    run.font.size = Pt(9)
    run.bold = True

    caveat = document.add_paragraph()
    caveat_run = caveat.add_run(
        "Authenticity is guaranteed by the SHA-256 above, computed over the canonical "
        "report content (identical to the Markdown and PDF exports). Re-generate from the "
        "same session data and compare digests to verify. The read-only protection applied "
        "to this document is a deterrent, not cryptographic tamper-proofing."
    )
    caveat_run.italic = True
    document.add_paragraph()


def generate_cab_docx(
    results: list[TestResult],
    meta: dict | None = None,
    rollback_evidence: list | None = None,
    output_path: str | Path | None = None,
) -> tuple[bytes, str]:
    """Generate a signed Word CAB report.

    Returns ``(docx_bytes, content_sha256)``. If *output_path* is given, the
    document is also written to disk.
    """
    meta = dict(meta or {})
    if "generated_at" not in meta:
        meta["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # The signature is over the canonical Markdown — the same content the PDF and
    # Markdown exports use — so all three artefacts share one authenticity model.
    markdown = cab_report(results, meta, rollback_evidence)
    content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    meta["content_hash"] = content_hash
    markdown = cab_report(results, meta, rollback_evidence)

    document = Document()
    document.core_properties.title = "DCC CAB Validation Report"
    document.core_properties.author = f"DCC Test Console ({meta.get('login', 'unknown')})"
    document.core_properties.subject = f"Environment: {meta.get('environment', '?')}"
    document.core_properties.keywords = f"SHA256:{content_hash}"
    document.core_properties.comments = f"Content SHA-256: {content_hash}"

    _cover(document, meta, content_hash)
    _render_markdown(document, markdown)
    _add_manual_attachments(document)
    _enforce_read_only(document)

    buffer = io.BytesIO()
    document.save(buffer)
    docx_bytes = buffer.getvalue()
    if output_path:
        Path(output_path).write_bytes(docx_bytes)
    return docx_bytes, content_hash


__all__ = ["generate_cab_docx"]
