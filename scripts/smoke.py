"""Exercise ingestion, persistence and a real HTTP call between the services."""

import argparse
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry-url", default="http://127.0.0.1:8001")
    parser.add_argument("--demand-url", default="http://127.0.0.1:8002")
    args = parser.parse_args()
    token = os.environ.get("API_TOKEN")
    if not token:
        parser.error("API_TOKEN is required; use uv run --env-file .env python scripts/smoke.py")
    now = datetime.now(UTC)
    start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) - timedelta(
        minutes=30
    )
    site_id = f"smoke-{uuid4().hex[:12]}"
    reading = {"site_id": site_id, "interval_start": start.isoformat(), "energy_kwh": 55.0}
    headers = {"Authorization": f"Bearer {token}", "x-request-id": str(uuid4())}
    with httpx.Client(headers=headers, timeout=15) as client:
        for base in (args.telemetry_url, args.demand_url):
            response = client.get(f"{base.rstrip('/')}/healthz")
            response.raise_for_status()
        response = client.post(f"{args.telemetry_url.rstrip('/')}/api/v1/readings", json=reading)
        response.raise_for_status()
        latest = client.get(
            f"{args.telemetry_url.rstrip('/')}/api/v1/sites/{site_id}/readings/latest"
        )
        latest.raise_for_status()
        if latest.json()["average_power_kw"] != 110:
            raise RuntimeError("Telemetry calculation did not return 110 kW")
        result = client.get(
            f"{args.demand_url.rstrip('/')}/api/v1/sites/{site_id}/demand",
            params={"contract_kw": 100},
        )
        result.raise_for_status()
        assessment = result.json()
        if assessment["status"] != "exceeded" or assessment["excess_kw"] != 10:
            raise RuntimeError(f"Unexpected demand assessment: {assessment}")
        print(f"PASS: {site_id}: 55 kWh / 0.5 h = 110 kW; contract 100 kW; excess 10 kW")


if __name__ == "__main__":
    main()
