"""Bit 16 receipt-template suggestions from healthy peer terminals.

The maintenance table records a terminal's *current* DCC receipt template, not the
one it should have, so the value a Bit 16 fix sends cannot come from the broken
row. Healthy terminals with the same profile are a strong signal instead (in the
2026-09-29 data, 62 of 95 brand/model/acquirer profiles use exactly one template
and ~95% of a profile's terminals agree on average). The fixer shows these as
*suggestions*; the operator must confirm or change the value before a dry run.

Profile values are bound as parameters; column names are declared constants.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from . import MAINTENANCE_SOURCE

_TEMPLATE = "PRINTOUT_TYPE_TEMPLATE_DCC"
_HEALTHY = (
    "PRINTOUTTYPETEMPLATEDCC_BASE = 1 AND PRINTOUTTYPETEMPLATEDCC_CHECK_C = 0 "
    f"AND {_TEMPLATE} IS NOT NULL AND {_TEMPLATE} NOT IN ('', 'None')"
)

# Peer groups from most to least specific.
_LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "healthy terminals with the same brand, model and acquirer",
        ("TERMINAL_BRAND_NAME", "TERMINAL_MODEL_NAME", "ACQUIRER_NAME"),
    ),
    (
        "healthy terminals with the same brand and model",
        ("TERMINAL_BRAND_NAME", "TERMINAL_MODEL_NAME"),
    ),
)


@dataclass(frozen=True)
class SuggestionQuery:
    label: str
    sql: str
    params: list


@dataclass(frozen=True)
class Suggestion:
    template: str
    terminals: int
    share: float  # of the peer group's terminals


def build_template_suggestion_queries(row: dict, top_n: int = 5) -> list[SuggestionQuery]:
    """Peer-group queries for one worklist row, most specific first.

    Levels whose profile values are missing on the row are skipped (``= NULL``
    would match nothing and look like "no peers").
    """
    capped = max(1, min(int(top_n), 20))
    queries: list[SuggestionQuery] = []
    for label, keys in _LEVELS:
        values = [row.get(key) for key in keys]
        if any(value is None or str(value).strip() == "" for value in values):
            continue
        where = " AND ".join(f"{key} = %s" for key in keys)
        sql = (
            f"SELECT {_TEMPLATE} AS TEMPLATE, COUNT(DISTINCT TERMINAL_IDENTIFIER) AS TERMINALS\n"  # noqa: S608
            f"FROM {MAINTENANCE_SOURCE}\n"
            f"WHERE {_HEALTHY} AND {where}\n"
            f"GROUP BY 1 ORDER BY 2 DESC LIMIT {capped}"
        )
        queries.append(SuggestionQuery(label, sql, [str(v) for v in values]))
    return queries


def build_known_templates_query(limit: int = 100) -> str:
    """Every template in use on healthy terminals, most used first (manual pick list)."""
    capped = max(1, min(int(limit), 500))
    return (
        f"SELECT {_TEMPLATE} AS TEMPLATE, COUNT(DISTINCT TERMINAL_IDENTIFIER) AS TERMINALS\n"  # noqa: S608
        f"FROM {MAINTENANCE_SOURCE}\nWHERE {_HEALTHY}\n"
        f"GROUP BY 1 ORDER BY 2 DESC LIMIT {capped}"
    )


def summarise(frame: pd.DataFrame | None) -> list[Suggestion]:
    """Turn a suggestion query result into ranked suggestions with their share."""
    if frame is None or frame.empty:
        return []
    total = int(frame["TERMINALS"].sum())
    return [
        Suggestion(
            str(row.TEMPLATE), int(row.TERMINALS), int(row.TERMINALS) / total if total else 0.0
        )
        for row in frame.itertuples(index=False)
    ]
