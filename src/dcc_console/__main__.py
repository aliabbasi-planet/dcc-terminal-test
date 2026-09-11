"""Launch the console with ``python -m dcc_console``."""

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
        "8501",
        "--server.address",
        "0.0.0.0",  # noqa: S104 - required so the container port mapping can reach the app
        "--server.headless",
        "true",
    ]
    return stcli.main()


if __name__ == "__main__":
    raise SystemExit(main())
