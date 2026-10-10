"""Validation and parsing for Batch target spreadsheets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO

try:
    from openpyxl import load_workbook
except ModuleNotFoundError:  # The rest of Remediation works without Excel upload support.
    load_workbook = None

from .mapping import FLAG_FIXES, TRACKED_CHECK_COLUMNS

_REQUIRED_HEADERS = {
    "change_type": "Change Type",
    "instance_identifier": "instance_identifier",
    "location_no": "location_no",
    "location_desc": "location_desc",
    "country_name": "country_name",
    "bank_merchant_id": "bank_merchant_id",
    "acquirer_name": "acquirer_name",
    "terminal_id": "TerminalID",
}

_COLUMN_TO_ROW_FIELD = {
    "instance_identifier": "INSTANCE_IDENTIFIER",
    "location_no": "LOCATION_NO",
    "location_desc": "LOCATION_NAME",
    "country_name": "COUNTRY_NAME",
    "bank_merchant_id": "BANK_MERCHANT_ID",
    "acquirer_name": "ACQUIRER_NAME",
}


@dataclass(frozen=True)
class UploadedTarget:
    """A spreadsheet-requested terminal/check pair and optional source metadata."""

    terminal_identifier: str
    check_column: str
    row_number: int
    metadata: dict[str, str]


def scope_row_to_checks(row: dict, checks: tuple[str, ...]) -> dict:
    """Copy a worklist row, masking checks not explicitly requested by upload."""
    allowed = tuple(check for check in checks if check in TRACKED_CHECK_COLUMNS)
    scoped = dict(row)
    for column in TRACKED_CHECK_COLUMNS:
        if column not in allowed:
            scoped[column] = 0
    scoped["_BATCH_CHECK_COLUMNS"] = allowed
    return scoped


def _token(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _excel_cell_text(cell, *, preserve_numeric_format: bool = False) -> str:
    value = cell.value
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = str(value).strip()
    if preserve_numeric_format and isinstance(value, int | float):
        number_format = str(cell.number_format or "").split(";", 1)[0]
        if re.fullmatch(r"0+", number_format):
            text = text.zfill(len(number_format))
    return text


def normalise_location_no(value: object) -> str:
    """Compare location IDs without Excel's optional left-padding zeroes."""
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.lstrip("0") or ("0" if text else "")


def _change_type_map() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for flag in FLAG_FIXES:
        if not flag.fixable:
            continue
        names = [flag.check_column, flag.flag_name]
        if flag.fix_value and flag.fix_bit in (1, 8):
            names.append(flag.fix_value)
            if flag.fix_bit == 8:
                names.append(f"handler_{flag.fix_value}")
        for name in names:
            aliases[_token(name)] = flag.check_column
    return aliases


_CHANGE_TYPE_TO_CHECK = _change_type_map()


def parse_workbook(content: bytes) -> tuple[UploadedTarget, ...]:
    """Parse the first worksheet; fail closed on unknown checks or malformed rows."""
    if load_workbook is None:
        raise ValueError(
            "Excel upload requires openpyxl. Install project dependencies with "
            "'.venv\\Scripts\\python.exe -m pip install -r requirements.txt' "
            "and run Streamlit using that same .venv interpreter."
        )
    try:
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError(f"Could not read the Excel workbook: {exc}") from exc

    sheet = workbook.active
    rows = sheet.iter_rows()
    try:
        header_cells = next(rows)
    except StopIteration as exc:
        raise ValueError("The Excel worksheet is empty.") from exc
    header_keys = [_token(cell.value) for cell in header_cells]
    header_keys = ["terminalid" if key == "instanceterminalid" else key for key in header_keys]
    header_index = {key: index for index, key in enumerate(header_keys) if key}
    missing = [
        label for key, label in _REQUIRED_HEADERS.items() if _token(key) not in header_index
    ]
    if missing:
        raise ValueError("Missing required Excel columns: " + ", ".join(missing))

    parsed: dict[tuple[str, str], UploadedTarget] = {}
    errors: list[str] = []
    for row_number, cells in enumerate(rows, start=2):
        if not any(_excel_cell_text(cell).strip() for cell in cells):
            break
        values = {
            key: cells[header_index[_token(key)]]
            for key in _REQUIRED_HEADERS
        }
        terminal_id = _excel_cell_text(values["terminal_id"], preserve_numeric_format=True)
        raw_change = _excel_cell_text(values["change_type"])
        check_column = _CHANGE_TYPE_TO_CHECK.get(_token(raw_change))
        if not terminal_id:
            errors.append(f"row {row_number}: TerminalID is blank")
            continue
        if check_column is None:
            errors.append(f"row {row_number}: unsupported Change Type {raw_change!r}")
            continue
        metadata = {}
        for upload_column, row_field in _COLUMN_TO_ROW_FIELD.items():
            text = _excel_cell_text(values[upload_column], preserve_numeric_format=True)
            if not text:
                continue
            metadata[row_field] = (
                normalise_location_no(text) if upload_column == "location_no" else text
            )
        parsed.setdefault(
            (terminal_id, check_column),
            UploadedTarget(terminal_id, check_column, row_number, metadata),
        )
    workbook.close()
    if errors:
        raise ValueError("Invalid Excel target row(s): " + "; ".join(errors))
    if not parsed:
        raise ValueError("The Excel worksheet has no target rows.")
    return tuple(parsed.values())