"""Bit 16 receipt-template rules transcribed from PrintoutTypeTemplateDCC_check.xlsx."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from . import MAINTENANCE_SOURCE

_BRAND_ALIASES = {
    "PAX": "PAX",
    "INGENICO": "INGENICO",
    "ATOS": "ATOSWORLDLINE",
    "ATOSWORLDLINE": "ATOSWORLDLINE",
    "WORLDLINE": "ATOSWORLDLINE",
    "VERIFONE": "VERIFONE",
}

# Workbook allowlist. Where the workbook lists two templates, both remain selectable.
_ALLOWED_TEMPLATES: dict[tuple[str, str], tuple[str, ...]] = {
    ("PLANETPAYMENT", "PAX"): ("PLANET_PAX",),
    ("PLANETPAYMENT", "INGENICO"): ("PLANET_ING_5000", "PLANET_ING_LANE3000"),
    ("PLANETPAYMENT", "ATOSWORLDLINE"): ("PLANET",),
    ("PLANETPAYMENT", "VERIFONE"): ("PLANET_VERIFONE",),
    ("ELAVON", "PAX"): ("ELAVONEUROPE_PAX",),
    ("ELAVON", "ATOSWORLDLINE"): ("ELAVONEUROPE",),
    ("ELAVON", "INGENICO"): ("ELAVONEUROPE_ING_5000",),
    ("FCC", "ATOSWORLDLINE"): ("GLOBALBLUE",),
    ("FCC", "INGENICO"): ("GLOBALBLUE_ING_5000",),
    ("FCC", "VERIFONE"): ("GLOBALBLUEE355",),
    ("FCC", "PAX"): ("GLOBALBLUE_PAX",),
    ("SIXPAYMENT", "ATOSWORLDLINE"): ("SIX_WORDLINE",),
    ("SIXPAYMENT", "INGENICO"): ("SIX_ING_5000", "SIX_ING_5000_UK"),
    ("SIXPAYMENT", "PAX"): ("SIX_PAX",),
    ("SIXMULTIPAY", "PAX"): ("SIX_PAX",),
    ("FEXCO", "PAX"): ("FEXCOEUROPE_PAX",),
    ("FEXCO", "ATOSWORLDLINE"): ("FEXCOEUROPE_WORLDLINE",),
    ("FEXCO", "INGENICO"): ("FEXCOEUROPE_ING",),
}


def _normalise(value: object) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def build_provider_lookup_query(rows: Iterable[Mapping[str, object]]) -> tuple[str, list[str]]:
    """Build a bound read for DCC provider codes by terminal identifier."""
    terminal_ids = sorted(
        {str(row.get("TERMINAL_IDENTIFIER")) for row in rows if row.get("TERMINAL_IDENTIFIER")}
    )
    if not terminal_ids:
        return "", []
    placeholders = ", ".join("%s" for _ in terminal_ids)
    return (
        f"SELECT TERMINAL_IDENTIFIER, MAX(DCCPROVIDER_CODE) AS DCC_PROVIDER_CODE, "
        f"COUNT(DISTINCT DCCPROVIDER_CODE) AS DCC_PROVIDER_COUNT\n"
        f"FROM {MAINTENANCE_SOURCE}\n"
        f"WHERE TERMINAL_IDENTIFIER IN ({placeholders})\n"
        "GROUP BY TERMINAL_IDENTIFIER",
        terminal_ids,
    )


def allowed_templates(row: Mapping[str, object]) -> tuple[str, ...]:
    """Return workbook-approved templates for a terminal's provider and brand."""
    provider_key = _normalise(row.get("DCCPROVIDER_CODE"))
    brand_key = _normalise(row.get("TERMINAL_BRAND_NAME"))
    brand = _BRAND_ALIASES.get(brand_key, brand_key)
    return _ALLOWED_TEMPLATES.get((provider_key, brand), ())


def common_allowed_templates(rows: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
    """Return templates approved for every terminal covered by an instance fix."""
    iterator = iter(rows)
    first = next(iterator, None)
    if first is None:
        return ()
    common = list(allowed_templates(first))
    for row in iterator:
        allowed = set(allowed_templates(row))
        common = [template for template in common if template in allowed]
        if not common:
            break
    return tuple(common)
