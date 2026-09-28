# DCC Remediation & Analytics — Implementation Plan

Status: **DRAFT for approval**. No branch/code created until this is agreed.
Author: Cortex Code · Target branch: `feature/dcc-remediation` (from `main`)
New code location: `src/dcc_console/remediation/` (new package; existing tabs untouched)

---

## 1. Goal & scope

Add a **third capability** to the app: identify broken DCC terminals from the
Snowflake presentation table, filter/analyse them, and remediate them **one by
one** through a guided, evidence-first workflow — kept **separate from, but
related to**, the existing CAB test tabs.

### In scope
- New **"DCC Remediation"** tab with two clearly separated steps:
  - **(A) Identify & filter** broken terminals (read-only, from Snowflake).
  - **(B) Fix one-by-one** (live pre-check → show config → confirm → apply via
    the SQL Server procedure → verify → log → next).
- **Analytics** over broken vs fixed terminals by date, handler/non-handler,
  brand, model, merchant, acquirer, region, country, industry.
- **Persistence** of fixes the app applies, so previously-fixed terminals are
  not re-applied while the daily source is still catching up.
- **Multi-user login** (per-user Snowflake SSO + per-user SQL Server login),
  session-scoped, no secret persistence.
- Minimum **recovery-integrity fixes** required to make a live bulk fixer safe.

### Out of scope (explicitly excluded per instruction)
- ❌ Excel/CSV campaign ingestion (`SP_INGEST_ALL_CAMPAIGNS`, `RECON_STAGE`, `.xlsx`).
- ❌ Revenue / profit allocation (`DCC_V3_FIX_EPISODE_PROFIT`,
  `DCC_V3_FIX_DAILY_TRACKER`, `DCC_V3_FIX_PROFIT_SUMMARY`,
  `PROD_CORE.TRANSFORMATION.TRN_DWH_DCC_REVENUE`).
- ❌ `DCC_V3_CAMPAIGN_FIX_LOG` (Excel-sourced).
- The existing **"Broken Terminals" (SQL Server direct)** tab is **kept as-is**
  (renamed concept: *DCC Health Tracker*); this plan does **not** replace it.

### Reused from the supplied SQL (only these three)
1. `DCC_V3_HEALTH_DAILY_SNAPSHOT` MERGE logic from
   `PROD_PRESENTATION.CORTEX.CORTEX_TERMINAL_MAINTENANCE` — the identification engine.
2. The snapshot **day-over-day diff** (`PREV=1 AND value=0`) — detects that a fix
   landed on another day (the "query other days" requirement).
3. `DCC_V3_FLAG_REFERENCE` — flag → label → type metadata.

---

## 2. Decisions (from review) & standing recommendations

| Topic | Decision | Note |
|---|---|---|
| Fix mode | **Live fixes now** | Against my recommendation; see §7 non-negotiables |
| Analytics refresh | **App-triggered** | Idempotent per day; see §5 caveats |
| Persistence target | **New dedicated schema** `DEV_CORE_AAB.DCC_REMEDIATION` | `DEV_CORE_PLACEHOLDER` does not exist |
| Snowflake auth | **SSO / externalbrowser**, user picks role + warehouse | No secrets stored |

Open items to confirm during review (do not block writing this doc):
- **SQL Server auth** for the fix path: SQL login vs AD/Windows. *(Recommend: named per-user SQL login with EXECUTE on the SP only.)*
- **Fixable scope**: which checks get a Fix button vs flag-only (see §6).
- **Who may run a live fix**: app-level allowlist/role (see §7.4).
- **Refresh role**: which Snowflake role runs the snapshot MERGE (needs read on `PROD_PRESENTATION.CORTEX` + write on the new schema).

---

## 3. Architecture — separation of concerns

```
                 ┌─────────────────────────────────────────────┐
   Snowflake     │ PROD_PRESENTATION.CORTEX.CORTEX_TERMINAL_MAINT│  (daily ~06:00, read-only)
  (discovery)    └───────────────┬─────────────────────────────┘
                                 │  snapshot MERGE (app-triggered, idempotent/day)
                 ┌───────────────▼─────────────────────────────┐
                 │ DEV_CORE_AAB.DCC_REMEDIATION                 │
                 │  • HEALTH_DAILY_SNAPSHOT  (broken rows only) │
                 │  • APP_FIX_LOG            (authoritative)    │
                 │  • FLAG_REFERENCE         (dim)              │
                 │  • V_* analytics views                       │
                 └───────────────┬─────────────────────────────┘
                                 │  read worklist / write fix log
   App (Streamlit)  ┌────────────▼───────────┐   live pre-check / apply / verify
   Tab 3 Remediation│ (A) Identify & filter  │────────────────────────────────┐
                    │ (B) Fix one-by-one     │                                │
                    └────────────────────────┘                                ▼
                                                        SQL Server cccai.spApplyDCCEnablementConfiguration
                                                        (the SAME hardened execute/verify/rollback core)
```

**Key principle:** Snowflake tells us *where to look* (worklist); SQL Server is
*the source of truth* for whether to act and is the only place a change is made
or rolled back. The stale (≤24h) snapshot is never used to decide a write.

---

## 4. Data model (minimal, optimized — 2 tables + 1 dim + views)

Schema: `DEV_CORE_AAB.DCC_REMEDIATION`

### 4.1 `HEALTH_DAILY_SNAPSHOT`
One row per **in-scope broken** terminal per day (not all 52k — only broken/
in-scope, ~15k/day). Clustered by `SNAPSHOT_DATE`.
- Keys: `(SNAPSHOT_DATE, TERMINAL_IDENTIFIER)`
- Identity columns: `INSTANCE_IDENTIFIER`, `LOCATION_NO` (needed to map fixes to bits)
- Dimensions: country, region, industry, merchant, customer, acquirer, brand, model, firmware
- The `*_CHECK_*` flags + `IS_DCC_BROKEN`
- Rationale: storing broken-only keeps the table small and query-fast; full-estate
  history isn't needed for remediation and would bloat storage.

### 4.2 `APP_FIX_LOG` (authoritative "we fixed this")
One row per fix the **app** applied.
- Keys: `(FIX_ID)` surrogate; natural index on `(TERMINAL_IDENTIFIER, FLAG_COLUMN)`
- `INSTANCE/TERMINAL/LOCATION` identifiers, `BIT`, `FLAG_COLUMN`, `VALUE_SENT`
- `MODE` (LIVE/SIM), `APPLIED_AT`, `APPLIED_BY` (SSO user), `SERVER`, `DATABASE`
- `STATE_BEFORE`, `STATE_AFTER`, `VERIFIED` (bool), `ROLLBACK_SCRIPT`, `OUTCOME`
- This is the **don't-re-fix** source: the worklist excludes anything with a
  recent successful `APP_FIX_LOG` row until the snapshot confirms it.

### 4.3 `FLAG_REFERENCE` (dim)
`FLAG_COLUMN → FLAG_NAME, FLAG_TYPE, FIX_BIT, TARGET_ID_KIND, FIXABLE(bool)`.
Drives the check→bit→identifier mapping and whether a Fix button appears.

### 4.4 Views (no extra physical tables)
- `V_CURRENT_BROKEN` — latest snapshot minus recently-app-fixed.
- `V_FIX_HISTORY` — app fixes + snapshot-detected fixes, by date/flag/dimension.
- `V_REMEDIATION_KPIS` — broken counts, fixed counts, still-broken, by dimension.

No profit tables, no episode-profit, no daily profit tracker.

---

## 5. Analytics refresh (app-triggered, per your choice)
- A **"Refresh snapshot"** button runs the `HEALTH_DAILY_SNAPSHOT` MERGE for
  `CURRENT_DATE` (idempotent — safe to press repeatedly).
- Guard: read `CORTEX_TERMINAL_MAINTENANCE` `LAST_ALTERED`; if unchanged since the
  last snapshot, warn "source not refreshed since <ts>" and skip needless work.
- Caveat recorded: app-triggered means the app's Snowflake role needs write on
  `DCC_REMEDIATION` + read on `PROD_PRESENTATION.CORTEX` + a warehouse. Confirm the
  refresh role in review. (A scheduled task remains the better long-term option.)

---

## 6. Fix → bit → identifier mapping

| Check column | Bit | Target identifier | Fixable in tab? |
|---|---|---|---|
| `HANDLER_DCCENABLE_CHECK_O` (+ AUTH/COMPLETION/NFC/NFCSINGLETAP) | 8 | `INSTANCE_IDENTIFIER` | ✅ |
| `DCCXPRESSCO_CHECK_O`, `DCCXPRESSCODT_CHECK_O` | 1 | `LOCATION_NO` | ✅ |
| `PRINTOUTTYPETEMPLATEDCC_CHECK_C` | 16 | `INSTANCE_IDENTIFIER` | ✅ (pending Bit 16 Undo fix) |
| `CONFIGDOWNLOAD_VERSION_CHECK_C` | 2 | `TERMINAL_IDENTIFIER` | ✅ |
| `FIRMWARE_VERSION_CHECK` | 4 | `TERMINAL_IDENTIFIER` | ⚠️ flag-only (different SP, heavier rollout) |
| `DCCMERCHANT_NO_CHECK_O` | — | — | ⚠️ flag-only (not fixable by this SP) |
| `LOCATION_DCCENABLED_CHECK_C` | — | — | ⚠️ confirm semantics (returns 0 broken) |

The maintenance table carries `INSTANCE_IDENTIFIER`, `TERMINAL_IDENTIFIER`, and
`LOCATION_NO` on every row, so each fixable check maps to the correct identifier
without guessing joins. (Reminder: instance ≠ terminal ID space — verified 0/58,877.)

---

## 7. Safety, DR, logging, sysops (the "reinforced / failure-proof" requirement)

### 7.1 Every live fix (non-negotiable)
1. **Live pre-check** on SQL Server; skip if already correct → mark "awaiting refresh".
2. **Journal restore point BEFORE** the call (per-fix), phase-tracked
   (`called → committed → verifying → done`).
3. **Apply** via the existing hardened `execution.run_test` / `apply_rollback` core.
4. **Immediate verify** from SQL Server (handler-level read) — record before/after.
5. **Write `APP_FIX_LOG`** with outcome, verified flag, rollback script, user.

### 7.2 Recovery-integrity fixes brought in-scope (required for live bulk)
- Fix recovery parameter-mismatch (parameterless SP scripts vs `?` params).
- Fix journal server/database check on recovery.
- Fix "rollback available stays true after success".
- (These are the P0s from PR #2 review that make crash recovery actually work.)

### 7.3 Disaster recovery
- Journal on a **persistent, backed-up path** (not container-ephemeral).
- **Recovery banner** on startup lists pending entries; recovery validates
  environment **+ server + database** before restoring.
- Written **manual recovery runbook**.

### 7.4 Safeguarding / RBAC
- **App-level allowlist** for who may run live fixes (SSO identity).
- SQL Server login limited to **EXECUTE on the SP** + minimum table rights.
- Snowflake role read-only on `PROD_PRESENTATION`, write only on `DCC_REMEDIATION`.
- Live PROD fix requires typed confirmation showing the exact EXEC to be run.

### 7.5 Logging / mid-operation failure tracking
- **Structured JSON logs** with a correlation id per fix, threaded
  pre-check → apply → verify → log.
- Central log sink + retention (not stderr only).
- **Alerts**: rollback failed, verify mismatch, journal entry stuck in a
  non-terminal phase beyond N minutes.

### 7.6 Sysops
- Show **build/version (git SHA)** in the UI.
- Health + readiness checks for **both** connections.
- Containerised deploy; CI gates on tests + ruff.

---

## 8. Multi-user login (per your SSO choice)
- **Login page** collects: Snowflake account/SSO (externalbrowser), role,
  warehouse; and SQL Server server/database + login.
- On submit → open **session-scoped** connections, validate both, then reveal tabs.
- **No secrets persisted**; connections live only in `st.session_state` for the
  session. SQL Server credential handling per the auth method confirmed in review.
- A failed/expired connection degrades gracefully and forces re-login without
  crashing an in-flight fix (the journal protects any committed change).

---

## 9. Module layout (new package, existing code untouched)
```
src/dcc_console/remediation/
  __init__.py
  snapshot.py      # HEALTH_DAILY_SNAPSHOT MERGE (Snowflake) + refresh guard
  worklist.py      # V_CURRENT_BROKEN read + filters + stale-guard vs APP_FIX_LOG
  mapping.py       # check → bit → identifier → fixable (from FLAG_REFERENCE)
  fixlog.py        # APP_FIX_LOG writer/reader
  analytics.py     # KPI/history view queries
  sf_connection.py # Snowflake SSO session connection (separate from pyodbc)
sql/remediation/
  001_schema.sql   # DCC_REMEDIATION schema + 3 objects + views (no profit)
  002_seed_flag_reference.sql
src/dcc_console/ui/
  remediation_tab.py  # Tab 3: (A) identify/filter, (B) one-by-one fix
docs/
  plan_dcc_remediation.md   # this file
  runbook_recovery.md       # manual DR steps
tests/
  test_remediation_*.py
```

---

## 10. Testing
- Unit: mapping, stale-guard exclusion, worklist filters, fixlog writes, snapshot
  MERGE SQL compiles.
- Fix-path: live pre-check skips already-correct; verify mismatch surfaces;
  journal phase transitions; recovery of a script-based entry.
- No network in tests (fake connections), mirroring existing suite.

---

## 11. Delivery phases
1. **Schema + FLAG_REFERENCE** in `DCC_REMEDIATION` (SQL, reviewed before run).
2. **Snapshot refresh** + `V_CURRENT_BROKEN` + read path.
3. **Tab 3 (A) Identify & filter** (read-only) — usable value immediately.
4. **APP_FIX_LOG** + stale-guard.
5. **Recovery-integrity fixes** (§7.2).
6. **Tab 3 (B) one-by-one fix** (live, gated by §7.1) + verify + log.
7. **Multi-user SSO login page**.
8. **Analytics views + KPI panels**.
9. **Logging/alerts, runbook, version stamp, health checks**.

---

## 12b. Baseline note (branch created from `main` @ `cf1f245`)

Branched after PR #3 (`bugfix/package_config`) merged. Relevant deltas:
- Catalog now declares `sp_column` and `sp_value_is_flag_name`; Bit 8 **and** Bit 16
  are `sp_managed=True`. **`mapping.py` reuses these fields** rather than
  hardcoding the check→bit→column mapping.
- Still open on `main` (so §7.2 remains in scope): recovery parameter mismatch
  (`sections.py` `_recover_entry` passes `(original_value, target)` unconditionally)
  and recovery guard checks environment name only, not server/database.
- Updated `main` baseline: 185 tests pass, ruff clean.

## 12. Honest caveats
- I have **no SQL Server access** here; the fix/verify/recovery paths are built
  and unit-tested but must be validated against `3CDB` by you/David.
- Snapshot semantics `_CHECK_C` vs `_CHECK_O` and the daily refresh time need
  confirmation from the table owner before KPIs are published.
- "Live fixes now" is recorded against my recommendation; §7.2 is the minimum I
  will not omit. Overruling §7.2 must be an explicit, recorded decision.
- Data-engineering best practice would schedule §5 as a task; app-triggered is
  per your choice and carries the runtime-grants tradeoff noted.
