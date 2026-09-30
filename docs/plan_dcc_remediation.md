# DCC Remediation & Analytics — Implementation Plan

Status: **APPROVED — phases 1-3 + 8 delivered** (read-only identify/analytics on
`feature/dcc-remediation`). See §13 for what is live vs deferred.
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
| Persistence target | **Per-user schema, configurable** (default `DEV_CORE_AAB.DCC_REMEDIATION`) | Each user points at their own `DEV_CORE_<x>`; see §14 |
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

---

## 13. Implementation status (this iteration)

**Delivered and verified against Snowflake (`DEV_CORE_AAB.DCC_REMEDIATION`):**
- Schema executed live: `FLAG_REFERENCE` (14 seeded rows), `HEALTH_DAILY_SNAPSHOT`,
  `APP_FIX_LOG`, and views `V_CURRENT_BROKEN` / `V_FIX_HISTORY` / `V_REMEDIATION_KPIS`.
- Snapshot loaded from `CORTEX_TERMINAL_MAINTENANCE`: **12,654** broken terminals
  today; `V_CURRENT_BROKEN` returns them as `ACTIONABLE`. Refresh MERGE proven
  idempotent (re-run updated 12,654 / inserted 0).
- New package `src/dcc_console/remediation/`: `mapping.py`, `snapshot.py`,
  `worklist.py`, `analytics.py`, `fixlog.py` (pure, unit-tested — 25 tests),
  `sf_connection.py` (SSO), `tab.py` (read-only identify/analyse/filter + refresh).
- Third tab **"DCC Remediation"** wired into `app.py`. `snowflake-connector-python`
  added to requirements. Suite: **210 pass**, ruff clean.

**Correction found during validation (schema hardened):** the original
`001_schema.sql` omitted two *fixable* checks — `PRINTOUTTYPETEMPLATEDCC_CHECK_C`
(Bit 16) and `CONFIGDOWNLOAD_VERSION_CHECK_C` (Bit 2). Both are now columns in the
snapshot and in the refresh; the file is corrected. Without this, ~1,400
template/config-only broken terminals were invisible to the worklist.

**Grain decision (recorded):** the maintenance table is ~1.7 rows/terminal, but
terminal→instance is 1:1 among broken rows, so the snapshot is grouped to
`TERMINAL_IDENTIFIER` with `MAX` of each normalized broken flag (broken if any
source row is broken). Location-scoped fixes (Bit 1) will group by `LOCATION_NO`
in the fixer. Broken = `BASE = 1 AND COALESCE(CHECK, 1) = 1` (in scope, failing/unknown).

**Deferred (needs confirmations before building — unchanged from §2/§7):**
- Phase 6 live one-by-one fixer (needs SQL Server auth method + live-fix RBAC
  allowlist), reusing `execution.run_test` / `apply_rollback` and `fixlog.build_insert`.
- Phase 5 recovery-integrity P0s (§7.2). Phase 7 unified SSO login page.
- Phase 9 logging/alerts/runbook/version stamp/health checks.
- `sql/remediation/003_refresh_snapshot.sql` is generated from `snapshot.py`
  (do not hand-edit). `runbook_recovery.md` not yet written (phase 9).

---

## 14. Multi-tenant (per-user DEV_CORE) — delivered

Per your choice, the schema is no longer hardcoded. Each user points the tab at
their **own** `DEV_CORE_<x>` and initialises it in one click.

- **`RemediationObjects(database, schema)`** (in `remediation/__init__.py`) holds the
  fully-qualified names and **validates** database/schema as bare SQL identifiers
  (interpolated, not bound) — an injection guard. Built from the live connection.
- All five query builders now **take `objs`** instead of a module constant.
- **`remediation/ddl.py`** generates the `CREATE SCHEMA` + tables + views and the
  `FLAG_REFERENCE` seed for any target, from the same `mapping` (so columns/rows
  can't drift). The tab exposes **"Initialise / verify my schema"** (idempotent)
  and detects when core tables are missing.
- The connection panel now collects **Database** + **Schema** (defaults from
  `SNOWFLAKE_DATABASE`/`SNOWFLAKE_SCHEMA` env, else `DEV_CORE_AAB`/`DCC_REMEDIATION`).

Per-user prerequisites (RBAC, outside the app): each user's role needs **read** on
`PROD_PRESENTATION.CORTEX.CORTEX_TERMINAL_MAINTENANCE` (+ its `INFORMATION_SCHEMA`)
and **create/write** on their own `DEV_CORE_<x>`.

**Known tradeoff (superseded by §15):** per-user schemas meant **siloed `APP_FIX_LOG`s**,
so the re-fix guard was per-schema. §15 moves the fix log (and the operator allowlist)
to one **shared** schema; each user keeps their own snapshot and views.

---

## 15. One-by-one live fixing — delivered (2026-09-29)

### Decisions (recorded)
| Question | Decision |
|---|---|
| Where may *Apply live* run? | **DEV/UAT only** in this phase. On PROD: live pre-check + dry run only. Enforced twice (`fixer.live_gate` and `fixer.run_step`). |
| Who may run live fixes? | A **named group**, listed in the shared `FIX_OPERATORS` table, checked against the SSO session's `CURRENT_USER()`. |
| Fix log | **Shared**: `DEV_CORE_AAB.DCC_REMEDIATION.APP_FIX_LOG` (configurable per user: "Shared fix log" field / `DCC_SHARED_SCHEMA`). |
| Bit 16 template | **Suggested** from healthy peers (brand+model+acquirer, then brand+model, then all known templates) and **confirmed by the operator**. |
| Bit 2 value | `ECB DCC` (all 39,674 healthy terminals have it; broken ones have `Standard`). |
| Bit 1 value | Always `Add` — any other value makes `build_call` send `@add = 0`, i.e. *remove*. Tested for all three functions. |

### The flow (per check, one at a time)
Select a worklist row → **Pre-check** (live SQL-Server read; an already-correct target
is logged, never re-applied) → **Dry run** (`@is_simulation = 1` + rolled back) →
**Apply live** (operator + console Mode armed + fresh pre-check and dry run of the
*exact* target/value + reviewed EXEC) → automatic **verify** (fresh live read) → logged
(one row per worklist terminal the call covers) → **Roll back** if needed → **Next terminal**.
A Bit 8/16 call changes a whole instance and a Bit 1 call a whole location — the panel
lists every listed terminal the call covers. UAT/DEV rehearsals may use a substitute
target when the PROD identifier does not exist there (logged with both IDs).

### Correctness fixes found while building it
- **Worklist view** read each terminal's newest row across *all* dates, so a terminal
  repaired since an earlier snapshot never left the list; and it joined fixes by terminal
  only (a second fixed check would duplicate the row; one fixed check hid the others).
  Now: latest snapshot date only; per-check resolution; only **PROD** outcomes count
  (rehearsals never hide PROD work); a later rollback re-opens a check; times compared in
  UTC (`SOURCE_LAST_ALTERED` is now stored as UTC).
- **`call_procedure` committed a failed live call** (its `finally` committed whenever
  `rollback=False`). It now commits only on success and rolls back on any error.
- **§7.2 recovery P0s** — all three fixed: SP-script journal entries recover without bound
  parameters (journal `restore_kind`, JSON scripts, old journals migrated); recovery
  checks environment **+ server + database**; a successful procedure rollback is no longer
  offered again. Also: "Restore ALL" now restores every entry (it stopped after the first),
  and a column restore refuses to write NULL.
- **Kept fixes vs crash recovery**: a verified, logged remediation fix is marked `KEPT` in the
  journal, so "Restore ALL" can never silently undo it. The Results panel's bulk rollback
  skips remediation results; they roll back only from the DCC Remediation tab (logged).
- A pre-check "already correct" only settles the worklist when it proves the check itself
  (Bit 8 flag true on every handler, Bit 2 on ECB DCC) — never for Bit 1/16.

### Runbook — operators and access
Add or remove a live-fix operator (owner of the shared schema):
```sql
INSERT INTO DEV_CORE_AAB.DCC_REMEDIATION.FIX_OPERATORS (USER_NAME, NOTES)
VALUES ('<SNOWFLAKE_USER_NAME>', 'approved by <who>, <date>');
UPDATE DEV_CORE_AAB.DCC_REMEDIATION.FIX_OPERATORS SET ACTIVE = FALSE
WHERE USER_NAME = '<SNOWFLAKE_USER_NAME>';
```
Teammates on **DATA_SCIENTIST** (the owner role) already have access. For another role:
```sql
GRANT USAGE ON DATABASE DEV_CORE_AAB TO ROLE <ROLE>;
GRANT USAGE ON SCHEMA DEV_CORE_AAB.DCC_REMEDIATION TO ROLE <ROLE>;
GRANT SELECT, INSERT ON TABLE DEV_CORE_AAB.DCC_REMEDIATION.APP_FIX_LOG TO ROLE <ROLE>;
GRANT SELECT ON TABLE DEV_CORE_AAB.DCC_REMEDIATION.FIX_OPERATORS TO ROLE <ROLE>;
```
Honest limits: the allowlist is an **app-level guardrail**, not a security boundary — the
app runs on each user's machine and anyone holding the owner role can edit the table. The
real control is each SQL-Server login's `EXECUTE` right on the procedure per environment
(checked by the readiness probes). A dedicated owner role for the shared schema is the
recommended next step.

### Still open
- **Validate on SQL Server** (no access from here): UAT rehearsal of one target per bit
  (1, 2, 8, 16): pre-check → dry run → live → verify → roll back, checking the journal
  and `APP_FIX_LOG`. This is also the first real test of Bit 16 undo.
- Bit 1 pre-check detects the function by whole-name match in `extra_function`; confirm
  the node format against real UAT data during the rehearsal.
- The old **Broken Terminals** tab still reads `instance.package_config` (not the handler
  rows the procedure edits) and matches 0 rows — retire it once this tab is adopted.

## 16. PROD enablement + durable fix registry + agent (2026-09-30)

Rebased onto `main @ d499f88` (the Bit 1/2 rollback fix from PR #4). That fix makes
`execution.apply_rollback` re-read the generic verify column and set `change_persisted`
for non-SP-managed bits (1, 2); the remediation fixer inherits it because it rolls back
through the same core.

### Decisions (recorded)
| Question | Decision |
|---|---|
| May *Apply live* run on PROD? | **Yes**, behind a stronger gate on top of the DEV/UAT rules. `fixer.LIVE_ENVIRONMENTS` now includes PROD. |
| PROD authorization | A **PROD-approved operator** (`CAN_PROD` on `FIX_OPERATORS`) **plus a typed change/CAB reference**, plus the existing armed Mode + fresh pre-check/dry-run + reviewed EXEC. Enforced in `fixer.live_gate` and again in `fixer.run_step`. (Two-person approval was considered; deferred as future work.) |
| Verify failure on Apply | **Warn only** (all environments): a fresh read that does not confirm the change shows a loud warning and enables Roll back; the fix is **not** registered. No auto-rollback. |
| Durable "fixed" registry | New shared-schema table **`DCC_FIX_REGISTRY`**, one row per `(ENVIRONMENT, TERMINAL_IDENTIFIER, CHECK_COLUMN)`. Upserted on a verified live fix (or an authoritative already-OK pre-check); **hard-deleted** on a confirmed rollback. `APP_FIX_LOG` stays the immutable audit trail. |
| Rollback registry delete | Only after a fresh SQL-Server read **confirms** the change reverted (`fixer.rollback_confirmed`). If it can't be confirmed, the row is kept and a warning shown. |
| Agent in the tab | The `PROD_PRESENTATION.CORTEX.DCC_TERMINAL_MAINTENANCE_AGENT` is embedded as **scoped presets + free chat**, scoped to the selected terminal, read-only, using the operator's own SSO session token. |

### Why a registry (survives the daily refresh)
The worklist view used to derive "fixed" by replaying `APP_FIX_LOG`. It now reads
`DCC_FIX_REGISTRY` instead (`V_CURRENT_BROKEN.fixed` CTE): `ENVIRONMENT='PROD'`,
`STATUS='FIXED'`, and `FIXED_AT_UTC > SOURCE_LAST_ALTERED` — the same freshness guard,
now materialised in a durable table so a verified fix is **not re-flagged** by the next
daily `MERGE`, and a **rollback re-opens** it by deleting the row. Upgrading an existing
schema backfills the registry from `APP_FIX_LOG` (`registry.build_backfill_from_log`,
insert-only), so nothing regresses. `FIXED_AT`/`FIXED_AT_UTC` are set server-side.

### The flow (per check), updated
Pre-check → dry run → **Apply live** (DEV/UAT, or PROD with `CAN_PROD` + change ref) →
automatic **verify** by a fresh read → logged to `APP_FIX_LOG` → **registered** in
`DCC_FIX_REGISTRY` when verified → **Roll back** (confirms reversion by a fresh read,
then **de-registers**). Reporting states verified/unverified, the environment, terminals
covered, and whether the registry was updated.

### Schema changes (idempotent; applied by "Initialise / verify my schema")
- `FIX_OPERATORS.CAN_PROD BOOLEAN` (nullable migration; NULL = no PROD rights).
- `APP_FIX_LOG.CHANGE_REF VARCHAR(120)` (migration).
- New `DCC_FIX_REGISTRY` (+ backfill from the log).
- `V_CURRENT_BROKEN` rewritten to read the registry; `V_REMEDIATION_KPIS` adds
  `REGISTERED_FIXES`. Readiness (`build_objects_present_query` / `schema_status`) now
  also checks the registry table and `CAN_PROD`.
- SQL snapshots regenerated via `scripts/generate_remediation_sql.py`.

### Security / DR
- PROD is gated by `CAN_PROD` **and** a change reference (both recorded on every PROD
  fix-log row and registry row); the app-level allowlist remains a guardrail, not a
  security boundary (the SQL-Server login's `EXECUTE` right per environment is the real
  control). The agent call reuses the operator's SSO session — no new secret.
- The registry stores each fix's compensating `SP_ROLLBACK_SCRIPT` + change ref, so a
  verified PROD fix can be rolled back later even from a fresh session. Substitute
  rehearsals remain **off PROD** (`fixer.substitute_allowed`).

### Runbook — PROD approval
```sql
-- Grant PROD approval to an existing operator (owner of the shared schema):
UPDATE DEV_CORE_AAB.DCC_REMEDIATION.FIX_OPERATORS SET CAN_PROD = TRUE
WHERE USER_NAME = '<SNOWFLAKE_USER_NAME>';
```
For a non-owner role, in addition to the §15 grants:
```sql
GRANT SELECT, INSERT, DELETE ON TABLE DEV_CORE_AAB.DCC_REMEDIATION.DCC_FIX_REGISTRY TO ROLE <ROLE>;
GRANT USAGE ON CORTEX AGENT PROD_PRESENTATION.CORTEX.DCC_TERMINAL_MAINTENANCE_AGENT TO ROLE <ROLE>;
-- plus USAGE on the agent's Cortex Analyst semantic view, per its owner.
```

### Still open (this iteration)
- **Live schema upgrade + rehearsal not yet run from here.** Click "Initialise / verify
  my schema" (or run `sql/remediation/001_schema.sql`) to create `DCC_FIX_REGISTRY`, add
  `CAN_PROD`/`CHANGE_REF` and rebuild the views; then rehearse one target per bit on UAT,
  and one gated PROD dry-run, before the first real PROD apply.
- Two-person PROD approval and an agent-reachability probe are possible follow-ups.
