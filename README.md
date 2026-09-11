# dcc-terminal_test-console

A single-page Streamlit console for exercising the SQL Server stored procedure
`[cccai].[spApplyDCCEnablementConfiguration]` against **DEV**, **UAT**, and **PROD**, with
transaction-safe simulation, a real rollback path for live runs, and complete
factual session reports for download.

This repository is published as `dcc-terminal_test-console` and contains the
interactive console and test harness used for campaign orchestration and safe
rollback testing.

---

## Contents

- [Highlights](#highlights)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [How a test runs](#how-a-test-runs)
- [Rollback model](#rollback-model)
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
| Configuration campaigns | Separate tab for running selected location, terminal, and instance tests sequentially, with a campaign report and campaign-only rollback |
| Exports | Results as JSON and a factual session report as Markdown |

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
pip install .
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
```

---

## How a test runs

1. **Select an environment** in the sidebar and connect with that environment's credentials.
2. **Pre-flight readiness** probes five conditions. Tests stay locked until all pass,
   or until you tick the explicit override.
3. **Choose a mode.** Live mode additionally requires typing `DEV`, `UAT`, or `PROD`.
4. **Pick a target** — a terminal from the loaded list, or an instance or location.
5. **Run.** The harness:
   - reads the declared verification column, which becomes the **restore point**;
   - calls the procedure inside a transaction, draining every result set and server message;
   - rolls back (simulation) or commits (live);
   - re-reads the column and compares.

### Configuration campaigns

Use the **Campaigns** tab to select one or more supported configuration tests. The tab
provides an explicit target picker for every selected test type: active terminals for
terminal tests, locations for location tests, and instances for instance tests. The
console runs every selected test for each target of its matching type in sequence, then
shows per-campaign pass, fail, review, blocked, and pending-rollback totals.

Use **Download campaign report** to export its target-by-target factual result table.
For a live campaign, **Roll back campaign live changes** restores every test row in that
campaign which still differs from its captured restore point. Individual result rollback
buttons and the global outstanding rollback control remain available as additional safety
controls.

### Verdicts

| Status | Meaning |
|---|---|
| 🟢 PASS | Simulation left the column unchanged, or a live run persisted the change |
| 🔴 FAIL | The call errored, or a simulation leaked a persistent change |
| 🟡 REVIEW | A live run completed but changed nothing — the procedure accepted the call without acting |
| ⛔ BLOCKED | The login lacked `EXECUTE`, so the procedure body never ran |

---

## Rollback model

Simulation needs no rollback — the transaction is already discarded. Live runs get
several layers, all replaying the value captured **before** the test:

| Layer | Trigger |
|---|---|
| Automatic | A live call errors *and* the row already changed |
| Manual | **Roll back this change** on any live result with a persisted change |
| Bulk | **Roll back every outstanding live change** at the top of the results section |
| Forced | Re-writes the restore point even when no difference was detected |

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

---

## Project layout

```
.
├── Dockerfile                 # Ships ODBC Driver 18; non-root runtime user
├── docker-compose.yml         # Console container
├── Makefile                   # install / run / test / lint / docker targets
├── pyproject.toml             # Packaging, pinned deps, pytest and ruff config
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
│   ├── catalog.py             # Test definitions and verified columns
│   ├── config.py              # Environment settings
│   ├── database.py            # pyodbc wrapper, transaction control
│   ├── execution.py           # build_call, run_test, classify, apply_rollback
│   ├── readiness.py           # Pre-flight probes and grant script
│   ├── reference.py           # Instance, location and terminal queries
│   ├── rollback.py            # Restore statement and verification
│   ├── state.py               # Session-state helpers
│   └── ui/                    # sidebar.py, sections.py
└── tests/                     # Catalogue, statement and rollback tests
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
including an injection attempt, verdict classification, and rollback availability.
No database connection is required to run it.

---

## Security notes

- Credentials are entered per session and never persisted, logged or exported.
- Every SQL value is a bound parameter; identifiers come only from `catalog.py`.
- Live mode needs a checkbox *and* the typed environment name.
- A failed live run auto-restores its captured pre-test value.
- The container runs as a non-root user (`uid 10001`).
- `.env` is git-ignored; only `.env.example` is committed.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `IM002 Data source name not found` | No ODBC driver. Use Docker, or `winget install Microsoft.msodbcsql18` |
| `EXECUTE permission was denied on 'fnDisplayTrace'` | Run the grant script shown in section 1 as a DBA |
| Every test returns ⛔ BLOCKED | Same permission issue; the procedure body never executes |
| Live run returns 🟡 REVIEW | The procedure accepted the call but changed nothing — check the target and value |
| `ERR_CONNECTION_REFUSED` on port 8501 | The server stopped. Re-run `python -m dcc_console` and keep the terminal open |
| UAT or PROD fields are empty | Set the matching `*_DB_SERVER` value in `.env` |

## Terminal Commmand
.\venv\Scripts\python.exe -m streamlit run src\dcc_console\app.py --server.port 8502 --server.headless=true --server.address=127.0.0.1