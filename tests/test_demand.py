import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from ems.common import Reading
from ems.demand.app import create_app
from ems.demand.client import (
    ServiceBindingTelemetryClient,
    TelemetryNotFoundError,
    TelemetryTimeoutError,
    TelemetryUpstreamError,
)

HEADERS = {"Authorization": "Bearer test-token", "x-request-id": "request-123"}
PATH = "/api/v1/sites/factory-01/demand"


def fresh_reading(energy_kwh=100):
    now = datetime.now(UTC)
    start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    return Reading(
        site_id="factory-01", interval_start=start - timedelta(minutes=30), energy_kwh=energy_kwh
    )


class FakeClient:
    def __init__(self, reading=None, error=None):
        self.reading = reading if reading is not None else fresh_reading()
        self.error = error
        self.calls = []

    async def latest(self, site_id, request_id):
        self.calls.append((site_id, request_id))
        if self.error:
            raise self.error
        return self.reading


@pytest.mark.parametrize(
    ("energy_kwh", "contract_kw", "demand_kw", "excess_kw", "percent", "status"),
    [
        (100, 250, 200, 0, 80, "within_limit"),
        (100, 150, 200, 50, 133.33, "exceeded"),
        (100, 200, 200, 0, 100, "within_limit"),
        (0, 200, 0, 0, 0, "within_limit"),
        (0.1, 0.3, 0.2, 0, 66.67, "within_limit"),
    ],
)
def test_half_hour_energy_is_converted_to_kw(
    energy_kwh, contract_kw, demand_kw, excess_kw, percent, status
):
    upstream = FakeClient(fresh_reading(energy_kwh))
    with TestClient(create_app(upstream, api_token="test-token")) as client:
        response = client.get(PATH, params={"contract_kw": contract_kw}, headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["demand_kw"] == demand_kw
    assert data["excess_kw"] == excess_kw
    assert data["utilization_percent"] == percent
    assert data["status"] == status
    assert upstream.calls == [("factory-01", "request-123")]
    assert response.headers["x-request-id"] == "request-123"


@pytest.mark.parametrize("contract_kw", [0, -1, 1e-320, 0.0001, 1_000_001, "nan", "inf"])
def test_invalid_contract_limit_is_rejected(contract_kw):
    with TestClient(create_app(FakeClient(), api_token="test-token")) as client:
        response = client.get(PATH, params={"contract_kw": contract_kw}, headers=HEADERS)
    assert response.status_code == 422


@pytest.mark.parametrize(("age_minutes", "expected_status"), [(180, 409), (-60, 502)])
def test_stale_and_future_readings_are_rejected(age_minutes, expected_status):
    reading = fresh_reading()
    reading = reading.model_copy(
        update={"interval_start": reading.interval_start - timedelta(minutes=age_minutes)}
    )
    with TestClient(create_app(FakeClient(reading), api_token="test-token")) as client:
        response = client.get(PATH, params={"contract_kw": 200}, headers=HEADERS)
    assert response.status_code == expected_status


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (TelemetryNotFoundError(), 404),
        (TelemetryUpstreamError(), 502),
        (TelemetryTimeoutError(), 504),
    ],
)
def test_upstream_errors_are_mapped(error, expected_status):
    with TestClient(create_app(FakeClient(error=error), api_token="test-token")) as client:
        response = client.get(PATH, params={"contract_kw": 200}, headers=HEADERS)
    assert response.status_code == expected_status


def test_authentication_and_missing_binding_fail_closed():
    with TestClient(create_app(api_token="test-token")) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get(PATH, params={"contract_kw": 200}).status_code == 401
        assert client.get(PATH, params={"contract_kw": 200}, headers=HEADERS).status_code == 503
    with TestClient(create_app(FakeClient())) as client:
        assert client.get(PATH, params={"contract_kw": 200}, headers=HEADERS).status_code == 503


def test_service_binding_http_contract_connects_both_services(tmp_path):
    from ems.telemetry.app import create_app as create_telemetry_app
    from ems.telemetry.repository import SQLiteReadingRepository

    telemetry_app = create_telemetry_app(
        SQLiteReadingRepository(tmp_path / "readings.sqlite3"), api_token="test-token"
    )

    class Response:
        def __init__(self, response):
            self.status = response.status_code
            self._response = response

        async def json(self):
            return self._response.json()

    class Binding:
        def __init__(self):
            self.request_id = None

        async def fetch(self, url, headers):
            self.request_id = headers["x-request-id"]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=telemetry_app)
            ) as client:
                return Response(await client.get(url, headers=headers))

    reading = fresh_reading(125)
    with TestClient(telemetry_app) as telemetry:
        registered = telemetry.post(
            "/api/v1/readings",
            json=reading.model_dump(mode="json", exclude_computed_fields=True),
            headers=HEADERS,
        )
    assert registered.status_code == 201
    binding = Binding()
    upstream = ServiceBindingTelemetryClient(binding, "test-token")
    with TestClient(create_app(upstream, api_token="test-token")) as demand:
        response = demand.get(PATH, params={"contract_kw": 200}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["demand_kw"] == 250
    assert response.json()["excess_kw"] == 50
    assert binding.request_id == "request-123"


@pytest.mark.parametrize("payload", [{}, [], {"site_id": "other-site"}])
def test_service_binding_rejects_invalid_upstream_readings(payload):
    class Response:
        status = 200

        async def json(self):
            return payload

    class Binding:
        async def fetch(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(TelemetryUpstreamError):
        client = ServiceBindingTelemetryClient(Binding(), "test-token")
        asyncio.run(client.latest("factory-01", "r1"))


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, TelemetryNotFoundError), (401, TelemetryUpstreamError), (500, TelemetryUpstreamError)],
)
def test_service_binding_translates_http_errors(status, error):
    class Response:
        def __init__(self):
            self.status = status

    class Binding:
        async def fetch(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(error):
        client = ServiceBindingTelemetryClient(Binding(), "test-token")
        asyncio.run(client.latest("factory-01", "r1"))


def test_service_binding_deadline_includes_response_body(monkeypatch):
    monkeypatch.setattr("ems.demand.client.UPSTREAM_TIMEOUT_SECONDS", 0.01)

    class Response:
        status = 200

        async def json(self):
            # Headers arrived, but the body never finishes.
            await asyncio.Future()

    class Binding:
        async def fetch(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(TelemetryTimeoutError):
        client = ServiceBindingTelemetryClient(Binding(), "test-token")
        asyncio.run(client.latest("factory-01", "r1"))
