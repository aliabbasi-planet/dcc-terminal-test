"""Launch the health tracker with ``python -m dcc_health_tracker``."""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.web import cli as stcli


def main() -> int:
    app_path = Path(__file__).with_name("app.py")
    sys.argv = [
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        "8503",
        "--server.address",
        "127.0.0.1",
        "--server.headless",
        "true",
    ]
    return stcli.main()


if __name__ == "__main__":
    raise SystemExit(main())
