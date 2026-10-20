"""Capture original values and restore them through the DCC procedure, never client DML."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.etree import ElementTree

from defusedxml.ElementTree import fromstring

from .catalog import TEST_CATALOG, TestDefinition
from .config import PROCEDURE, RETURN_CODE_COLUMN
from .database import DatabaseConnection, ProcedureOutput

logger = logging.getLogger(__name__)

# The handler types a Bit 8 call touches, taken verbatim from the procedure's own
# selection query. Used only to READ the same rows back for verification evidence.
HANDLER_TYPE_FILTER = (
    "handlerTerminalRequester",
    "handlerTerminalRequesterCale",
    "handlerTerminalRequesterNexo",
    "handlerTerminalRequesterPayAtTable",
    "handlerTerminalRequesterServiceApi",
    "handlerTerminalStandalone",
)

_CONFIG_VALUE_RE = re.compile(r'config_value="([^"]*)"')

# How a rollback was performed (recorded on every outcome).
METHOD_COLUMN = "column"
METHOD_PROCEDURE_SCRIPT = "procedure_script"
METHOD_PROCEDURE_JSON = "procedure_json"


@dataclass
class RollbackOutcome:
    ok: bool
    rows: int
    sql: str
    trigger: str
    error: str | None = None
    at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    method: str = METHOD_COLUMN
    payload: str | None = None
    return_code: int | None = None
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "rows": self.rows,
            "sql": self.sql,
            "trigger": self.trigger,
            "error": self.error,
            "at": self.at,
            "method": self.method,
            "payload": self.payload,
            "return_code": self.return_code,
            "messages": self.messages,
        }


def restore_statement(definition: TestDefinition) -> str:
    """Legacy evidence only. Never execute this SQL for rollback."""
    verify = definition.verify
    # Identifiers come from the frozen catalogue, never from user input; the value
    # and the row key are bound parameters.
    return (
        f"UPDATE {verify.table} SET {verify.column} = CONVERT({verify.cast}, ?) "  # noqa: S608
        f"WHERE CONVERT(nvarchar(50), {verify.key}) = ?"
    )


def read_statement(definition: TestDefinition) -> str:
    """The exact SELECT used to read the verified column before/after a call."""
    verify = definition.verify
    # Identifiers are catalogue constants; the row key is a bound parameter.
    return (
        f"SELECT TOP (1) CONVERT(nvarchar(max), {verify.column}) "  # noqa: S608
        f"FROM {verify.table} "
        f"WHERE CONVERT(nvarchar(50), {verify.key}) = ?"
    )


def read_state(connection: DatabaseConnection, definition: TestDefinition, identifier: str):
    """Read the verified column for one row, or ``None`` if it cannot be read."""
    sql = read_statement(definition)
    try:
        return connection.scalar(sql, (identifier,))
    except Exception as exc:
        logger.warning("State read failed for %s: %s", identifier, exc)
        return None


def restore_state(
    connection: DatabaseConnection,
    definition: TestDefinition,
    identifier: str,
    original_value,
    trigger: str,
) -> RollbackOutcome:
    """Compatibility entry point for a captured Bit 1/2 column (not executable SQL)."""
    try:
        if definition.bit not in (1, 2):
            raise ValueError("A per-handler JSON restore point is required for Bits 8/16.")
        plan = plan_from_originals(
            connection,
            definition,
            identifier,
            "",
            [{definition.json_field: identifier, "original": original_value}],
        )
        return restore_via_payload(connection, plan, trigger)
    except (ValueError, TypeError) as exc:
        return RollbackOutcome(False, 0, "", trigger, str(exc), method=METHOD_PROCEDURE_JSON)


def restore_via_scripts(
    connection: DatabaseConnection,
    scripts: list[str],
    trigger: str,
) -> RollbackOutcome:
    """Refuse legacy scripts. SQL text must never be accepted as a restore payload."""
    return RollbackOutcome(
        False,
        0,
        "",
        trigger,
        "Legacy SQL rollback is disabled. A captured structured JSON restore point is required.",
        method=METHOD_PROCEDURE_JSON,
    )


def _xml(value: object):
    return fromstring(str(value)) if value is not None else None


def _xml_key(value: object):
    """Compare XML values independently of formatting, attribute order and boolean spelling."""
    root = _xml(value)

    def key(element):
        attrs = dict(element.attrib)
        if attrs.get("config_value") in ("1", "0"):
            attrs["config_value"] = "true" if attrs["config_value"] == "1" else "false"
        return (
            element.tag,
            tuple(sorted(attrs.items())),
            element.text if element.text and element.text.strip() else "",
            tuple(sorted((key(child) for child in element), key=repr)),
        )

    return key(root) if root is not None else None


def _scoped_xml_key(value: object, plan: dict, connection):
    """Do not confuse undoing this field with undoing unrelated subsequent fixes."""
    root = _xml(value)
    if root is None:
        return None
    bit = plan["bit"]
    if bit == 1:
        function_id = connection.scalar(
            "SELECT TOP (1) extra_function_id FROM [ccc].[extra_function] "
            "WHERE extra_function_name = ? AND ccc_config = 1",
            (plan["field"],),
        )
        if function_id is None:
            raise ValueError("Could not resolve the original extra_function_id.")
        nodes = [n for n in root.iter() if n.get("extra_function_id") == str(function_id)]
    elif bit == 8:
        nodes = [n for n in root.iter() if n.get("config_name") == plan["field"]]
    else:
        nodes = root.findall("./printout_type_template_dcc")
    return root.tag, tuple(_xml_key(ElementTree.tostring(n, encoding="unicode")) for n in nodes)


def _object_value(bit: int, original: object, field_name: str, function_id: str | None):
    if bit == 2:
        if original is None:
            return None
        if str(original) not in ("1", "2"):
            raise ValueError("Unsupported original configdownload_version; expected 1, 2 or NULL.")
        return str(original)
    root = _xml(original)
    if bit == 1:
        nodes = (
            []
            if root is None
            else [node for node in root.iter() if node.get("extra_function_id") == function_id]
        )
        if len(nodes) > 1:
            raise ValueError("Duplicate extra_function elements cannot be restored exactly.")
        return "1" if nodes else "0"
    nodes = (
        []
        if root is None
        else (
            [node for node in root.iter() if node.get("config_name") == field_name]
            if bit == 8
            else root.findall("./printout_type_template_dcc")
        )
    )
    if len(nodes) > 1:
        raise ValueError("Duplicate XML elements cannot be restored exactly.")
    if not nodes:
        return None
    value = nodes[0].get("config_value") if bit == 8 else (nodes[0].text or "")
    if value is None or value == "NULL":
        raise ValueError("The SP NULL sentinel cannot represent this existing XML value exactly.")
    if bit == 8:
        if value not in ("true", "false", "1", "0"):
            raise ValueError("Unsupported original handler flag value.")
        return "1" if value in ("true", "1") else "0"
    return value


def plan_from_originals(
    connection, definition: TestDefinition, target: str, value: str, originals: list[dict]
) -> dict:
    """Create the versioned client envelope; only records are sent to the SP."""
    field_name = (
        definition.function_name
        if definition.bit == 1
        else value
        if definition.bit == 8
        else "printout_type_template_dcc"
        if definition.bit == 16
        else "configdownload_version"
    )
    function_id = None
    if definition.bit == 1:
        function_id = connection.scalar(
            "SELECT TOP (1) extra_function_id FROM [ccc].[extra_function] "
            "WHERE extra_function_name = ? AND ccc_config = 1",
            (field_name,),
        )
        if function_id is None:
            raise ValueError("Could not resolve the location extra_function_id.")
        function_id = str(function_id)
    id_key = "handler_id" if definition.bit in (8, 16) else definition.json_field
    records = [
        {
            id_key: str(row[id_key]),
            "object_value": _object_value(definition.bit, row["original"], field_name, function_id),
        }
        for row in originals
    ]
    plan = {
        "version": 1,
        "operation": "rollback",
        "bit": definition.bit,
        "test_key": definition.key,
        "target_type": definition.target,
        "target": target,
        "field": field_name,
        "records": records,
        "originals": originals,
    }
    validate_plan(plan)
    return plan


def _read_originals(connection, definition: TestDefinition, target: str, value: str) -> list[dict]:
    """Read existing rows without requiring their current values to be restorable."""
    if definition.bit not in (1, 2, 8, 16):
        raise ValueError(f"SP JSON rollback does not support Bit {definition.bit}.")
    if definition.sp_managed:
        states = read_sp_flag_states(
            connection,
            target,
            value if definition.bit == 8 else None,
            column=definition.sp_column,
            strict=True,
        )
        originals = [
            {"handler_id": str(s["handler_id"]), "original": s["document"]} for s in states
        ]
    else:
        sql = read_statement(definition).replace("TOP (1) ", "")
        frame = connection.query(sql, (target,))
        if len(frame) != 1:
            raise ValueError("Expected exactly one existing target for the restore point.")
        original = frame.iloc[0, 0]
        originals = [{definition.json_field: target, "original": original}]
    return originals


def capture_rollback_plan(connection, definition: TestDefinition, target: str, value: str) -> dict:
    """Fail closed on read errors or missing rows, distinguishing SQL NULL from no row."""
    return plan_from_originals(
        connection,
        definition,
        target,
        value,
        _read_originals(connection, definition, target, value),
    )


def validate_plan(plan: dict) -> TestDefinition:
    """Allowlist envelope, record keys, catalogue fields and scalar data, never SQL."""
    keys = {
        "version",
        "operation",
        "bit",
        "test_key",
        "target_type",
        "target",
        "field",
        "records",
        "originals",
    }
    if not isinstance(plan, dict) or set(plan) != keys:
        raise ValueError("Invalid rollback envelope fields.")
    if (
        type(plan["version"]) is not int
        or type(plan["bit"]) is not int
        or not isinstance(plan["test_key"], str)
    ):
        raise ValueError("Invalid rollback version, bit or test key types.")
    definition = TEST_CATALOG.get(plan["test_key"])
    if (
        plan["version"] != 1
        or plan["operation"] != "rollback"
        or definition is None
        or plan["bit"] not in (1, 2, 8, 16)
        or plan["bit"] != definition.bit
        or plan["target_type"] != definition.target
    ):
        raise ValueError("Unsupported rollback operation, bit or target type.")
    if not isinstance(plan["target"], str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,100}", plan["target"]
    ):
        raise ValueError("Invalid rollback target identifier.")
    field_name = plan["field"]
    if definition.bit == 8:
        allowed = definition.value_options or ()
    else:
        allowed = (
            definition.function_name
            if definition.bit == 1
            else "printout_type_template_dcc"
            if definition.bit == 16
            else "configdownload_version",
        )
    if field_name not in allowed:
        raise ValueError("Unsupported rollback field.")
    records, originals = plan["records"], plan["originals"]
    if (
        not isinstance(records, list)
        or not records
        or not isinstance(originals, list)
        or len(records) != len(originals)
    ):
        raise ValueError("Rollback requires original values for every affected record.")
    id_key = "handler_id" if definition.sp_managed else definition.json_field
    seen = set()
    for row, original in zip(records, originals, strict=True):
        if not isinstance(row, dict) or set(row) != {id_key, "object_value"}:
            raise ValueError("Unsupported rollback record fields.")
        identifier = row[id_key]
        if not isinstance(identifier, str) or identifier in seen:
            raise ValueError("Invalid or duplicate rollback record identifier.")
        if id_key == "handler_id":
            if not identifier.isascii() or not identifier.isdigit() or int(identifier) <= 0:
                raise ValueError("handler_id must be a positive integer.")
        elif identifier != plan["target"]:
            raise ValueError("Rollback record does not match its target.")
        seen.add(identifier)
        if (
            not isinstance(original, dict)
            or set(original) != {id_key, "original"}
            or original[id_key] != identifier
            or (original["original"] is not None and not isinstance(original["original"], str))
        ):
            raise ValueError("Invalid original-value evidence.")
        value = row["object_value"]
        if value is not None and not isinstance(value, str):
            raise ValueError("object_value must be a string or JSON null.")
        if definition.bit in (1, 8) and value not in ("0", "1", None):
            raise ValueError("Invalid original boolean value.")
        if definition.bit == 1 and value is None:
            raise ValueError("Bit 1 requires original function presence (0 or 1).")
        if definition.bit == 2 and value not in ("1", "2", None):
            raise ValueError("Invalid original configdownload_version.")
        if definition.bit == 16 and value is not None and (len(value) > 200 or value == "NULL"):
            raise ValueError("Invalid original receipt template.")
    return definition


def build_rollback_call(plan: dict) -> tuple[str, tuple, str]:
    definition = validate_plan(plan)
    parameter = {
        1: "@location_json",
        2: "@terminal_json",
        8: "@instance_json",
        16: "@instance_json",
    }[definition.bit]
    records = [{k: ("NULL" if v is None else v) for k, v in row.items()} for row in plan["records"]]
    payload = json.dumps(records, ensure_ascii=False)
    sql = (
        f"DECLARE @dcc_rc INT;\nEXEC @dcc_rc = {PROCEDURE} "
        f"@display_config = ?, {parameter} = ?, @is_simulation = 0, @is_rollback = 1"
    )
    params: tuple = (definition.bit, payload)
    if definition.bit in (1, 8):
        name = "@extra_function_name" if definition.bit == 1 else "@Extra_Config_Name"
        sql += f", {name} = ?"
        params += (plan["field"],)
    sql += f";\nSELECT @dcc_rc AS {RETURN_CODE_COLUMN};"
    return sql, params, payload


def verify_rollback_plan(connection, plan: dict) -> None:
    definition = validate_plan(plan)
    current = capture_rollback_plan(connection, definition, plan["target"], plan["field"])
    id_key = "handler_id" if definition.sp_managed else definition.json_field
    before = {row[id_key]: row["original"] for row in plan["originals"]}
    after = {row[id_key]: row["original"] for row in current["originals"]}
    if before.keys() != after.keys():
        raise ValueError("Affected record identifiers changed; rollback verification failed.")
    for identifier, original in before.items():
        restored = after[identifier]
        equal = (
            original == restored
            if definition.bit == 2
            else (
                _scoped_xml_key(original, plan, connection)
                == _scoped_xml_key(restored, plan, connection)
            )
        )
        if not equal:
            raise ValueError(f"Original value was not restored exactly for {id_key} {identifier}.")


def restore_via_payload(connection, plan: dict, trigger: str) -> RollbackOutcome:
    """Validate return code and every restored record BEFORE the transaction commits."""
    sql, payload = "", None
    messages: list[str] = []
    return_code = None
    validated = False
    try:
        sql, params, payload = build_rollback_call(plan)
        definition = validate_plan(plan)
        expected = plan_from_originals(
            connection, definition, plan["target"], plan["field"], plan["originals"]
        )
        if expected["records"] != plan["records"]:
            raise ValueError("Rollback values do not match the captured original evidence.")
        # Resolve current scope first so stale/cross-instance handler IDs never reach the SP.
        current = _read_originals(connection, definition, plan["target"], plan["field"])
        key = "handler_id" if definition.sp_managed else definition.json_field
        current_ids = [r[key] for r in current]
        if len(current_ids) != len(set(current_ids)) or set(current_ids) != {
            r[key] for r in plan["records"]
        }:
            raise ValueError("Rollback record identifiers no longer match the current target.")

        def validate(output: ProcedureOutput) -> None:
            nonlocal messages, return_code, validated
            messages, return_code = output.messages, output.return_code
            if return_code != 0:
                raise ValueError(f"Rollback SP returned {return_code!r}; expected success code 0.")
            verify_rollback_plan(connection, plan)
            validated = True

        connection.call_procedure(sql, params, rollback=False, validate=validate)
        if not validated:
            raise ValueError("The connection did not perform pre-commit rollback validation.")
        return RollbackOutcome(
            True,
            len(plan["records"]),
            sql,
            trigger,
            method=METHOD_PROCEDURE_JSON,
            payload=payload,
            return_code=return_code,
            messages=messages,
        )
    except Exception as exc:
        connection.safe_rollback()
        logger.error("SP JSON rollback failed: %s", exc)
        return RollbackOutcome(
            False,
            0,
            sql,
            trigger,
            str(exc),
            method=METHOD_PROCEDURE_JSON,
            payload=payload,
            return_code=return_code,
            messages=messages,
        )


def extract_flag_value(extra_config: str, flag_name: str) -> str | None:
    """Return the ``config_value`` for a named flag inside an ``extra_config`` XML string.

    Parses defensively — no XML library, no attribute-order assumption: find the
    element that declares ``config_name="<flag>"`` and read its ``config_value``.
    Returns None if the flag is absent or the value can't be read. Value-agnostic,
    so it works for boolean flags and any other stored value alike.
    """
    if not extra_config or not flag_name:
        return None
    needle = f'config_name="{flag_name}"'
    for element in str(extra_config).split("<"):
        if needle in element:
            match = _CONFIG_VALUE_RE.search(element)
            if match:
                return match.group(1)
    return None


def handler_read_statement(column: str) -> str:
    """The shared per-handler verification SELECT; columns are strictly allowlisted."""
    if column not in ("extra_config", "receipt_config"):
        raise ValueError("Unsupported handler verification column.")
    placeholders = ", ".join(["?"] * len(HANDLER_TYPE_FILTER))
    return (
        f"SELECT h.handler_id, h.handler_name, "  # noqa: S608
        f"CONVERT(nvarchar(max), h.{column}) AS {column} "
        "FROM [cccintegrang].[handler] h "
        "INNER JOIN [cccintegrang].[instance] i ON i.instance_id = h.instance_id "
        "INNER JOIN [cccintegrang].[handler_type] ht "
        "ON ht.handler_type_id = h.handler_type_id "
        "WHERE CONVERT(nvarchar(50), i.instance_identifier) = ? "
        f"AND ht.handler_type_identifier IN ({placeholders})"
    )


def read_sp_flag_states(
    connection: DatabaseConnection,
    instance_identifier: str,
    flag_name: str | None,
    column: str = "extra_config",
    strict: bool = False,
) -> list[dict]:
    """Read the current handler-level value on every handler an sp_managed call targets.

    Uses the procedure's own handler<->instance join (``instance_id``) and the same
    ``handler_type`` filter, so we inspect exactly the rows the procedure changes.
    ``column`` is always one of the catalogue's own fixed constants (``extra_config``
    for Bit 8, ``receipt_config`` for Bit 16) — never built from user input.

    When ``flag_name`` is given, the named ``config_name``/``config_value`` pair is
    extracted from the document (Bit 8's boolean handler flags). When it is
    ``None``, the whole document is returned as-is (Bit 16's receipt template,
    which is not a named flag lookup).

    Display reads return ``[]`` and log failures. ``strict=True`` raises on failures
    or missing handler IDs so execution/recovery cannot confuse missing data with NULL.
    """
    sql = handler_read_statement(column)
    if not instance_identifier:
        return []
    try:
        frame = connection.query(sql, (instance_identifier, *HANDLER_TYPE_FILTER))
    except Exception as exc:
        logger.warning("Handler-level verification read failed: %s", exc)
        if strict:
            raise
        return []
    states: list[dict] = []
    for _, row in frame.iterrows():
        raw = row.get(column)
        value = extract_flag_value(raw, flag_name) if flag_name else raw
        states.append(
            {
                "handler_name": row.get("handler_name"),
                "flag_value": value,
            }
        )
        if "handler_id" in frame.columns:
            states[-1]["handler_id"] = str(row["handler_id"])
            states[-1]["document"] = raw
    if strict and (not states or any("handler_id" not in s for s in states)):
        raise ValueError("Could not capture stable handler_id values for every affected handler.")
    return states


def flag_states_match(prior: list[dict], restored: list[dict]) -> bool:
    """True if every handler's flag value in ``restored`` equals its value in ``prior``.

    Requires the same set of handlers on both sides; empty input is treated as
    "cannot confirm" (False) so we never claim verification we didn't perform.
    """
    if not prior or not restored:
        return False
    key = "handler_id" if all("handler_id" in s for s in prior + restored) else "handler_name"
    prior_by_handler = {s[key]: s["flag_value"] for s in prior}
    restored_by_handler = {s[key]: s["flag_value"] for s in restored}
    if set(prior_by_handler) != set(restored_by_handler):
        return False
    return all(prior_by_handler[h] == restored_by_handler[h] for h in prior_by_handler)
