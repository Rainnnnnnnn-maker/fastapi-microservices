"""CPython-only entrypoints. Cloudflare entrypoints never import this module."""

import os

from ems.demand.app import create_app as create_demand_app
from ems.demand.client import HttpTelemetryClient
from ems.telemetry.app import create_app as create_telemetry_app
from ems.telemetry.repository import SQLiteReadingRepository


def telemetry():
    return create_telemetry_app(
        SQLiteReadingRepository(".data/telemetry.sqlite3"), api_token=os.environ.get("API_TOKEN")
    )


def demand():
    token = os.environ.get("API_TOKEN")
    return create_demand_app(
        client=HttpTelemetryClient(os.environ.get("TELEMETRY_URL", "http://127.0.0.1:8001"), token),
        api_token=token,
    )
