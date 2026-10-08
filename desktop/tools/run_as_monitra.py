"""Run the desktop app from source under the name Windows should print on notifications.

    python tools/run_as_monitra.py            # from desktop/

``python main.py`` now does the same by itself (see `core/dev_identity.py`, which holds
the logic); this command remains for running it explicitly and for its tests.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

DESKTOP = Path(__file__).resolve().parent.parent
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

from core.dev_identity import (  # noqa: E402,F401
    APP_NAME,
    NOTIFICATION_HEADER_NAME,
    RUNNER_DIR,
    RUNNER_NAME,
    VERSION,
    _version_parts,
    build_version_resource,
    prepare_runner,
    read_file_description,
    runner_environment,
    stamp,
)


def main(argv: Optional[List[str]] = None) -> int:
    if sys.platform != "win32":
        print("run_as_monitra.py is for Windows: a notification is named after the program's file description there.")
        return 2
    args = list(sys.argv[1:] if argv is None else argv)
    runner = prepare_runner()
    print(f"running main.py as {runner} ({read_file_description(runner)})", flush=True)
    return subprocess.call([str(runner), str(DESKTOP / "main.py"), *args], cwd=str(DESKTOP), env=runner_environment())


if __name__ == "__main__":
    sys.exit(main())
