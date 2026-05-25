#!/usr/bin/env python3
"""Run the backend checks that are stable enough for pull-request CI."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

BACKEND_TEST_MODULES = [
    "backend.tests.test_stream_checking_mode",
    "backend.tests.test_dispatcharr_fetch_pressure_config",
    "backend.tests.test_api_schemas",
    "backend.tests.test_stream_stats_utils",
    "backend.tests.test_blank_detection",
    "backend.tests.test_bitrate_detection",
    "backend.tests.test_stream_check_utils",
]


def run(cmd: list[str], *, env: dict[str, str]) -> None:
    print(f"+ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def main() -> int:
    env = os.environ.copy()
    existing_path = env.get("PYTHONPATH")
    pythonpath = str(BACKEND)
    if existing_path:
        pythonpath = pythonpath + os.pathsep + existing_path
    env["PYTHONPATH"] = pythonpath

    run([sys.executable, "-m", "compileall", "backend/apps"], env=env)
    run([sys.executable, "-m", "unittest", *BACKEND_TEST_MODULES], env=env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
