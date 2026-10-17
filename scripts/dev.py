"""Start both independent local HTTP servers, stop both on Ctrl-C or process exit."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    if not os.environ.get("API_TOKEN"):
        sys.exit("Set API_TOKEN in .env; start with: uv run --env-file .env python scripts/dev.py")
    root = Path(__file__).resolve().parents[1]
    processes: list[subprocess.Popen] = []

    def stop(_signum=None, _frame=None):
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        for service, port in [("telemetry", 8001), ("demand", 8002)]:
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        f"ems.local:{service}",
                        "--factory",
                        "--app-dir",
                        "src",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--log-level",
                        "info",
                    ],
                    cwd=root,
                )
            )
        while all(process.poll() is None for process in processes):
            time.sleep(0.2)
        return next((process.returncode for process in processes if process.returncode), 0)
    finally:
        stop()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
