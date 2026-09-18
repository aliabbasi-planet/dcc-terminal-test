# dcc-terminal_test-console

A single-page Streamlit console for exercising the SQL Server stored procedure
`[cccai].[spApplyDCCEnablementConfiguration]` against **DEV**, **UAT**, and **PROD**, with
transaction-safe simulation, a real rollback path for live runs, CAB-grade evidence
reports, and crash-resistant disaster recovery.

This repository is published as `dcc-terminal_test-console` and contains the
interactive console and test harness used for campaign orchestration, CAB validation
reporting, and safe rollback testing.

---

## Contents

- [Highlights](#highlights)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [How a test runs](#how-a-test-runs)
- [CAB validation reports](#cab-validation-reports)
- [Rollback model](#rollback-model)
- [Disaster recovery](#disaster-recovery)
- [Project layout](#project-layout)
- [Development](#development)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)

---

## Highlights

| Capability | Detail |
|---|---|
| Three environments | DEV, UAT, and PROD each keep their own server, database, username and password |
| Pre-flight gate | Verifies the procedure, `db.fnDisplayTrace`, and `EXECUTE` grants before unlocking tests |
| Simulation mode | `@is_simulation = 1` plus an enclosing transaction that is always rolled back |
| Live mode | Requires typing the environment name, captures a restore point, commits, then offers rollback |
| Rollback | Automatic on live failure, manual per result, bulk for everything outstanding, forced re-write available |
| Configuration campaigns | Separate tab for running selected location, terminal, and instance tests sequentially, with campaign reports and campaign-only rollback |
| Negative validation battery | 7 deliberately-invalid input cases that must be rejected by the procedure |
| CAB reports | Markdown and signed PDF with SHA-256 integrity hash, coverage matrix, and per-test evidence |
| Disaster recovery | SQLite restore journal persists original values to disk before live UAT/PROD runs |
| Exports | Results as JSON, session ledger as Markdown, CAB report as Markdown or signed PDF |

Supported configuration bits: **1** DCC Xpress CO · **2** Config Download Version ·
**4** Firmware Package · **8** DCC Handler Flags · **16** DCC Receipt Template.

---

## Quick start

### Option A — Docker (recommended)

No ODBC driver installation required; the image ships Microsoft ODBC Driver 18.

```bash
git clone <repository-url>
cd dcc-terminal_test-console
cp .env.example .env
docker compose up -d
```

On Windows use `copy .env.example .env`.

Open <http://localhost:8501>.

### Option B — Local Python

Requires Python 3.10+ and an ODBC driver for SQL Server.

```bash
python -m venv venv
venv\Scripts\activate
pip install -e .
copy .env.example .env
python -m dcc_console
```

On Linux and macOS use `source venv/bin/activate` and `cp .env.example .env`.

Open <http://localhost:8501>.

Missing ODBC driver on Windows:

```powershell
winget install Microsoft.msodbcsql18
```

or run `scripts\Install-OdbcDriver.ps1`.

### Running on a custom port

```powershell
.\venv\Scripts\python.exe -m streamlit run src\dcc_console\app.py `
  --server.port 8502 --server.headless=true --server.address=127.0.0.1
```

---

## Configuration

`.env` holds only non-secret values. **Database usernames and passwords are entered
in the app** and are never written to disk, logs or exports.

```dotenv
DEV_DB_SERVER=LU3C01DVSQL01.devnet.local
DEV_DB_NAME=3CDB

UAT_DB_SERVER=lu3co1tvsqlc03r.devnet.local
UAT_DB_NAME=3CDB

PROD_DB_SERVER=
PROD_DB_NAME=3CDB

STREAMLIT_SERVER_PORT=8502
```

---

## How a test runs

1. **Select an environment** in the sidebar and connect with that environment's credentials.
2. **Pre-flight readiness** probes five conditions and captures the procedure's SHA-256
   definition hash. Tests stay locked until all pass, or until you tick the explicit override.
3. **Choose a mode.** Live mode additionally requires typing `DEV`, `UAT`, or `PROD`.
4. **Pick a target** — a terminal from the loaded list, or an instance or location.
5. **Run.** The harness:
   - reads the declared verification column, which becomes the **restore point**;
   - for live runs on UAT/PROD, writes the restore point to the disaster-recovery journal;
   - calls the procedure inside a transaction, draining every result set and server message;
   - rolls back (simulation) or commits (live);
   - re-reads the column and compares.

### Configuration campaigns

Use the **Campaigns** tab to select one or more supported configuration tests. The tab
provides an explicit target picker for every selected test type: active terminals for
terminal tests, locations for location tests, and instances for instance tests. The
console runs every selected test for each target of its matching type in sequence, then
shows per-campaign pass, fail, review, blocked, and pending-rollback totals.

Enable **Include negative validation battery** to append 7 deliberately-invalid cases
(non-existent instance, terminal, location; unknown flag; unknown function; unknown
version; empty JSON). Each must be rejected by the procedure for the campaign to be
CAB-grade. Negative cases are always forced to `@is_simulation = 1`.

For a live campaign, **Roll back campaign live changes** restores every test row in that
campaign which still differs from its captured restore point.

### Verdicts

The console distinguishes *the procedure executed safely* from *the configuration changed*:

| Verdict | Meaning |
|---|---|
| `SIMULATED-OK` | Procedure executed under `@is_simulation=1`; transaction rolled back; nothing persisted |
| `APPLIED` | Live run committed and the verified column changed — configuration actually applied |
| `REVIEW` | Live run completed without error but the verified column did not change |
| `REJECTED-AS-EXPECTED` | A deliberately invalid input was rejected — validation path works |
| `NOT-REJECTED` | A deliberately invalid input was **not** rejected — a finding to review |
| `BLOCKED` | Login lacked `EXECUTE`; the procedure body never ran |
| `FAIL` | The call raised an unexpected SQL error |

---

## CAB validation reports

The console generates Change Advisory Board evidence in two formats, both derived
entirely from recorded run data — no figure is hand-entered.

### Report sections

| Section | Content |
|---|---|
| **A** — Procedure execution evidence | Exact `EXEC` statement, input JSON, before/after verified column, procedure preview output, rollback verification |
| **B** — Coverage matrix | All 13 configuration areas with covered/blocked/not-tested state |
| **C** — Negative validation | Each invalid-input case with the rejection evidence captured |
| **D** — Capability & limitation analysis | Confirmed tested, placeholder-exercised, blocked, not tested, assumptions |
| **E** — Rollback evidence | Full before → apply → restore cycle proving rollback works |
| **Report integrity** | SHA-256 content hash for tamper detection |

### Signed PDF

The PDF version embeds the SHA-256 digest of the report content in the document
metadata and prints it in the footer of every page. To verify a report was not
modified, re-generate it from the same session data and compare digests.

This is **content-integrity verification**, not certificate-based digital signing.
It proves the content is unmodified relative to a known hash; it does not prove
authorship via PKI.

### Procedure version pinning

Pre-flight readiness captures `object_id`, `create_date`, `modify_date`, and a
SHA-256 hash of the procedure body from `sys.sql_modules`. This pins the exact
procedure build that was validated. The connected login needs `VIEW DEFINITION`
permission for the hash to be computed.

---

## Rollback model

Simulation needs no rollback — the transaction is already discarded. Live runs get
several layers, all replaying the value captured **before** the test:

| Layer | Trigger |
|---|---|
| Automatic | A live call errors *and* the row already changed |
| Manual | **Roll back this change** on any live result with a persisted change |
| Bulk | **Roll back every outstanding live change** at the top of the results section |
| Campaign | **Roll back this campaign's live changes** for one campaign's rows |
| Forced | Re-writes the restore point even when no difference was detected |
| Recovery | Restores from the disk journal after an app crash (UAT/PROD only) |

Every live result renders the rollback panel unconditionally — including the restore
point and the full rollback history — so the control is never hidden.

The restore statement is fixed in shape:

```sql
UPDATE <catalog.table>
SET    <catalog.column> = CONVERT(<catalog.cast>, ?)
WHERE  CONVERT(nvarchar(50), <catalog.key>) = ?
```

Table, column, key and cast come only from `catalog.py`. The value and row key are
bound parameters, so a rollback cannot be redirected at another row or table.

### What rollback is not

Rollback is a **compensating write**, not a transaction undo. The sequence on a live
failure is:

```
COMMIT (new value written)
  → error detected
    → UPDATE (old value written back)
      → COMMIT
```

There is always a brief window where the new value exists in the database before it
is reversed. If TMS pushes a config download to a terminal during that window, the
terminal picks up the new setting; the database rollback does not undo that.

---

## Disaster recovery

If the app or machine crashes mid-campaign, Streamlit session state is lost. Live
changes are already committed but the restore points would be gone.

The console defends against this with a **SQLite restore journal**.

### How it works

```
BEFORE live test on UAT/PROD:
  journal.record_restore_point()   ← SQLite WAL commit (survives crash)
  call_procedure()                 ← if the app dies here...
  connection.commit()              ← ...change is in the DB, journal has the original

ON NEXT APP LAUNCH:
  Red banner: "N uncommitted live change(s) found in UAT"
  Connect to the affected environment → click Restore → original value recovered
```

### Design

| Decision | Reason |
|---|---|
| SQLite, not a log file | ACID guarantees; survives partial writes and power loss |
| WAL journal mode | Writes survive even if the process is killed mid-write |
| DEV excluded | Recovery overhead is not worth it on a dev server; avoids stale restores |
| `~/.dcc_console/restore_journal.db` | Outside the repo; persists across branches and clones |
| Auto-purge after 30 days | Resolved entries do not accumulate |
| Environment validation | Cannot roll back UAT entries while connected to PROD |

### Journal lifecycle

| Status | Meaning |
|---|---|
| `PENDING` | Live test started, original value saved, not yet rolled back |
| `NO_CHANGE` | Procedure ran but did not change the verified column |
| `RESOLVED` | Manual, automatic, campaign, or recovery rollback succeeded |

### Important limitation

Each test commits individually inside the campaign loop. There is no outer
transaction wrapping a whole campaign. If a campaign of 3 instances crashes on the
third, instances 1 and 2 remain committed — the journal is what makes them
recoverable.

---

## Project layout

```
.
├── Dockerfile                 # Ships ODBC Driver 18; non-root runtime user
├── docker-compose.yml         # Console container
├── Makefile                   # install / run / test / lint / docker targets
├── pyproject.toml             # Packaging, pinned deps, pytest and ruff config
├── requirements.txt           # Runtime deps for pip-only installs
├── requirements-dev.txt       # Test and lint deps
├── .env.example               # Non-secret template
├── docs/
│   └── reports/               # Generated CAB and test reports
├── scripts/
│   ├── Install-OdbcDriver.ps1
│   └── Invoke-SqlScript.ps1
├── sql/
│   └── DccEnablementConfiguration_UatSimulation.sql
├── src/dcc_console/
│   ├── app.py                 # Page composition
│   ├── campaign.py            # Campaign planning and factual reporting
│   ├── catalog.py             # Test definitions and verified columns
│   ├── config.py              # Environment settings
│   ├── coverage.py            # 13-area coverage matrix computation
│   ├── database.py            # pyodbc wrapper, explicit transaction control
│   ├── execution.py           # build_call, run_test, classify, apply_rollback
│   ├── journal.py             # SQLite disaster-recovery restore journal
│   ├── negatives.py           # Negative validation battery
│   ├── pdf_report.py          # Signed PDF generator with SHA-256 integrity
│   ├── readiness.py           # Pre-flight probes, grant script, procedure hash
│   ├── reference.py           # Instance, location and terminal queries
│   ├── report.py              # CAB report generator (Sections A–E)
│   ├── rollback.py            # Restore statement and verification
│   ├── state.py               # Session-state helpers
│   └── ui/                    # sidebar.py, sections.py
└── tests/                     # Catalogue, statement, coverage and rollback tests
```

The core modules are Streamlit-free, so they are unit-testable without a browser
session. Only `app.py` and `ui/` import Streamlit.

---

## Development

```bash
make dev     # editable install with dev extras
make test    # pytest
make lint    # ruff check plus format check
make fmt     # auto-fix
```

Without `make`:

```bash
pip install -e ".[dev]"
python -m pytest
python -m ruff check src tests
```

The suite covers catalogue integrity, parameterisation of every generated statement
including an injection attempt, verdict classification, coverage matrix computation,
negative-case verdicts, CAB report structure, and rollback availability. No database
connection is required to run it.

---

## Security notes

- Credentials are entered per session and never persisted, logged or exported.
- Every SQL value is a bound parameter; identifiers come only from `catalog.py`.
- Live mode needs a checkbox *and* the typed environment name.
- A failed live run auto-restores its captured pre-test value.
- The restore journal stores original column values on the local disk in
  `~/.dcc_console/` — treat that directory as sensitive on shared machines.
- The container runs as a non-root user (`uid 10001`).
- `.env` is git-ignored; only `.env.example` is committed.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ModuleNotFoundError: No module named 'fpdf'` | Run `pip install -r requirements.txt` or `pip install -e .` |
| `IM002 Data source name not found` | No ODBC driver. Use Docker, or `winget install Microsoft.msodbcsql18` |
| `EXECUTE permission was denied on 'fnDisplayTrace'` | Run the grant script shown in section 1 as a DBA |
| Every test returns ⛔ BLOCKED | Same permission issue; the procedure body never executes |
| Live run returns 🟡 REVIEW | The procedure accepted the call but changed nothing — check the target and value |
| Procedure SHA-256 shows `unavailable` | The login lacks `VIEW DEFINITION`; grant it and re-run readiness |
| `ERR_CONNECTION_REFUSED` on port 8501 | The server stopped. Re-run `python -m dcc_console` and keep the terminal open |
| Port already in use | `Get-NetTCPConnection -LocalPort 8501 \| Select OwningProcess -Unique \| ForEach { Stop-Process -Id $_.OwningProcess -Force }` |
| UAT or PROD fields are empty | Set the matching `*_DB_SERVER` value in `.env` |
| Red crash-recovery banner on startup | A previous live run on UAT/PROD was not rolled back. Connect to that environment and restore. |
