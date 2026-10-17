import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ems.telemetry.app import create_app
from ems.telemetry.repository import D1ReadingRepository, SQLiteReadingRepository

HEADERS = {"Authorization": "Bearer test-token"}
READING = {
    "site_id": "factory-01",
    "interval_start": "2025-01-01T00:00:00Z",
    "energy_kwh": 100.0,
}


@pytest.fixture
def client(tmp_path):
    repository = SQLiteReadingRepository(tmp_path / "data" / "readings.sqlite3")
    with TestClient(create_app(repository, api_token="test-token")) as client:
        yield client


def test_auth_is_required_but_health_is_public(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "telemetry"}
    assert client.post("/api/v1/readings", json=READING).status_code == 401
    assert (
        client.get(
            "/api/v1/sites/factory-01/readings/latest",
            headers={"Authorization": "Bearer wrong-token"},
        ).status_code
        == 401
    )


def test_register_and_get_latest_with_calculated_power(client):
    missing = client.get("/api/v1/sites/factory-01/readings/latest", headers=HEADERS)
    assert missing.status_code == 404
    response = client.post("/api/v1/readings", json=READING, headers=HEADERS)
    assert response.status_code == 201
    assert response.json()["average_power_kw"] == 200.0
    assert response.json()["interval_end"] == "2025-01-01T00:30:00Z"
    latest = client.get("/api/v1/sites/factory-01/readings/latest", headers=HEADERS)
    assert latest.json() == response.json()
    assert latest.headers.get("x-request-id")


def test_retries_are_idempotent_and_conflicting_value_is_rejected(client):
    first = client.post("/api/v1/readings", json=READING, headers=HEADERS)
    repeated = client.post("/api/v1/readings", json=READING, headers=HEADERS)
    assert repeated.status_code == 201
    assert repeated.json() == first.json()
    conflict = client.post("/api/v1/readings", json={**READING, "energy_kwh": 101}, headers=HEADERS)
    assert conflict.status_code == 409
    latest = client.get("/api/v1/sites/factory-01/readings/latest", headers=HEADERS)
    assert latest.json() == first.json()


def test_latest_uses_interval_order_and_separates_sites(client):
    for start in ["2025-01-02T00:00:00Z", "2025-01-01T00:00:00Z"]:
        response = client.post(
            "/api/v1/readings", json={**READING, "interval_start": start}, headers=HEADERS
        )
        assert response.status_code == 201
    latest = client.get("/api/v1/sites/factory-01/readings/latest", headers=HEADERS)
    assert latest.json()["interval_start"] == "2025-01-02T00:00:00Z"
    other_site = client.get("/api/v1/sites/factory-02/readings/latest", headers=HEADERS)
    assert other_site.status_code == 404


def test_timezone_variants_share_the_same_interval(client):
    first = client.post("/api/v1/readings", json=READING, headers=HEADERS)
    equivalent = client.post(
        "/api/v1/readings",
        json={**READING, "interval_start": "2025-01-01T09:00:00+09:00"},
        headers=HEADERS,
    )
    assert equivalent.status_code == 201
    assert equivalent.json() == first.json()


@pytest.mark.parametrize(
    "changes",
    [
        {"energy_kwh": -1},
        {"energy_kwh": 1000001},
        {"site_id": "../factory"},
        {"interval_start": "2025-01-01T00:15:00Z"},
        {"interval_start": "2025-01-01T00:00:00"},
        {"interval_start": "0001-01-01T00:00:00+01:00"},
        {"interval_start": "9999-12-31T23:30:00Z"},
        {"unexpected": "value"},
    ],
)
def test_invalid_measurements_are_rejected(client, changes):
    response = client.post("/api/v1/readings", json={**READING, **changes}, headers=HEADERS)
    assert response.status_code == 422


def test_future_and_incomplete_intervals_are_rejected(client):
    now = datetime.now(UTC)
    current_interval = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    for interval in [
        current_interval,
        current_interval + timedelta(days=1),
        datetime(9999, 12, 31, 23, 30, tzinfo=UTC),
    ]:
        response = client.post(
            "/api/v1/readings",
            json={**READING, "interval_start": interval.isoformat()},
            headers=HEADERS,
        )
        assert response.status_code == 422


def test_missing_database_configuration_fails_closed():
    with TestClient(create_app(api_token="test-token")) as client:
        response = client.post("/api/v1/readings", json=READING, headers=HEADERS)
    assert response.status_code == 503


@pytest.mark.parametrize("wrapped", [False, True])
def test_d1_accepts_both_sdk_dicts_and_raw_javascript_rows(wrapped):
    class JavaScriptRow:
        def to_py(self):
            return READING

    class Statement:
        def bind(self, *_parameters):
            return self

        async def first(self):
            return JavaScriptRow() if wrapped else READING

    class Database:
        def prepare(self, _sql):
            return Statement()

    reading = asyncio.run(D1ReadingRepository(Database()).latest("factory-01"))
    assert reading.site_id == "factory-01"
    assert reading.average_power_kw == 200
