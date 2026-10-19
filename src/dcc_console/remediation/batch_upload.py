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

_HEADER_TO_ROW_FIELD = {
    "instanceidentifier": "INSTANCE_IDENTIFIER",
    "locationno": "LOCATION_NO",
    "locationdesc": "LOCATION_DESC",
    "locationname": "LOCATION_NAME",
    "country": "COUNTRY_NAME",
    "countryname": "COUNTRY_NAME",
    "region": "REGION",
    "industryname": "INDUSTRY_NAME",
    "bankmerchantid": "BANK_MERCHANT_ID",
    "merchantname": "MERCHANT_NAME",
    "customername": "CUSTOMER_NAME",
    "acquirername": "ACQUIRER_NAME",
    "dccprovidercode": "DCCPROVIDER_CODE",
    "printouttypereceipt": "PRINTOUT_TYPE_RECEIPT",
    "printouttypetemplatedcc": "PRINTOUT_TYPE_TEMPLATE_DCC",
    "printouttypedisplay": "PRINTOUT_TYPE_DISPLAY",
    "terminalbrandname": "TERMINAL_BRAND_NAME",
    "terminalmodelname": "TERMINAL_MODEL_NAME",
    "firmwareversion": "FIRMWARE_VERSION",
    "terminalid": "TERMINAL_IDENTIFIER",
    "instanceterminalid": "TERMINAL_IDENTIFIER",
    "terminalidentifier": "TERMINAL_IDENTIFIER",
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
        if flag.fix_bit == 16:
            names.append("Receipt template DCC")
        if flag.fix_value and flag.fix_bit in (1, 8):
            names.append(flag.fix_value)
            if flag.fix_bit == 8:
                names.append(f"handler_{flag.fix_value}")
        for name in names:
            aliases[_token(name)] = flag.check_column
    return aliases


_CHANGE_TYPE_TO_CHECK = _change_type_map()


def parse_workbook(content: bytes) -> tuple[UploadedTarget, ...]:
    """Read checks from column A and validate named metadata columns in the first worksheet."""
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
    header_index: dict[str, int] = {}
    for index, cell in enumerate(header_cells[1:], start=1):
        header = _token(cell.value)
        if not header:
            continue
        row_field = _HEADER_TO_ROW_FIELD.get(header)
        if row_field is None:
            raise ValueError(f"Unsupported Excel column: {cell.value!r}")
        if row_field in header_index:
            raise ValueError(f"Duplicate Excel column for {row_field}.")
        header_index[row_field] = index
    if "TERMINAL_IDENTIFIER" not in header_index:
        raise ValueError(
            "Missing required Excel column: TerminalID, InstanceTerminalID or terminal_identifier."
        )

    parsed: dict[tuple[str, str], UploadedTarget] = {}
    errors: list[str] = []
    for row_number, cells in enumerate(rows, start=2):
        if not any(_excel_cell_text(cell).strip() for cell in cells):
            break
        terminal_cell = cells[header_index["TERMINAL_IDENTIFIER"]]
        terminal_id = _excel_cell_text(terminal_cell, preserve_numeric_format=True)
        raw_change = _excel_cell_text(cells[0])
        check_column = _CHANGE_TYPE_TO_CHECK.get(_token(raw_change))
        if not terminal_id:
            errors.append(f"row {row_number}: TerminalID is blank")
            continue
        if check_column is None:
            errors.append(f"row {row_number}: unsupported Change Type {raw_change!r}")
            continue
        metadata = {}
        for row_field, index in header_index.items():
            if row_field == "TERMINAL_IDENTIFIER":
                continue
            cell = cells[index] if index < len(cells) else None
            text = _excel_cell_text(cell, preserve_numeric_format=True) if cell else ""
            if not text:
                continue
            metadata[row_field] = (
                normalise_location_no(text) if row_field == "LOCATION_NO" else text
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