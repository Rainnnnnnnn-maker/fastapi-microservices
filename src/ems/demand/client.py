"""Read telemetry through the service's public HTTP contract."""

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import quote

from pydantic import ValidationError

from ems.common import Reading

UPSTREAM_TIMEOUT_SECONDS = 5


class TelemetryNotFoundError(Exception):
    """The requested site has no readings."""


class TelemetryUpstreamError(Exception):
    """Telemetry returned an error or an invalid response."""


class TelemetryTimeoutError(Exception):
    """Telemetry did not complete within the request deadline."""


class TelemetryClient(Protocol):
    async def latest(self, site_id: str, request_id: str) -> Reading: ...


def _reading_from_payload(payload: Any, site_id: str) -> Reading:
    if hasattr(payload, "to_py"):
        payload = payload.to_py()
    if not isinstance(payload, Mapping):
        raise TelemetryUpstreamError("Telemetry returned invalid JSON data")
    # Responses include computed interval_end and average_power_kw, which are
    # derived again here so untrusted calculated values cannot affect demand.
    values = {name: payload[name] for name in Reading.model_fields if name in payload}
    try:
        reading = Reading.model_validate(values)
    except ValidationError as error:
        raise TelemetryUpstreamError("Telemetry returned an invalid reading") from error
    if reading.site_id != site_id:
        raise TelemetryUpstreamError("Telemetry returned a reading for a different site")
    return reading


def _check_status(status: int) -> None:
    if status == 404:
        raise TelemetryNotFoundError("No readings found for this site")
    if status != 200:
        raise TelemetryUpstreamError("Telemetry request failed")


class ServiceBindingTelemetryClient:
    """Use the private Cloudflare service binding, with one total deadline."""

    def __init__(self, binding: Any, api_token: str) -> None:
        self._binding = binding
        self._api_token = api_token

    async def latest(self, site_id: str, request_id: str) -> Reading:
        async def fetch_reading() -> Reading:
            path = f"/api/v1/sites/{quote(site_id, safe='')}/readings/latest"
            response = await self._binding.fetch(
                f"https://telemetry.internal{path}",
                headers={
                    "Authorization": f"Bearer {self._api_token}",
                    "x-request-id": request_id,
                },
            )
            _check_status(response.status)
            return _reading_from_payload(await response.json(), site_id)

        try:
            return await asyncio.wait_for(fetch_reading(), timeout=UPSTREAM_TIMEOUT_SECONDS)
        except TimeoutError as error:
            raise TelemetryTimeoutError("Telemetry request timed out") from error
        except TelemetryNotFoundError, TelemetryUpstreamError:
            raise
        except Exception as error:
            raise TelemetryUpstreamError("Telemetry request failed") from error


class HttpTelemetryClient:
    """CPython adapter for local services; every client is explicitly closed."""

    def __init__(self, base_url: str, api_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token

    async def latest(self, site_id: str, request_id: str) -> Reading:
        # httpx is a development dependency and is not needed by the Worker.
        import httpx

        async def fetch_reading() -> Reading:
            async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{self._base_url}/api/v1/sites/{quote(site_id, safe='')}/readings/latest",
                    headers={
                        "Authorization": f"Bearer {self._api_token}",
                        "x-request-id": request_id,
                    },
                )
                _check_status(response.status_code)
                return _reading_from_payload(response.json(), site_id)

        try:
            return await asyncio.wait_for(fetch_reading(), timeout=UPSTREAM_TIMEOUT_SECONDS)
        except (TimeoutError, httpx.TimeoutException) as error:
            raise TelemetryTimeoutError("Telemetry request timed out") from error
        except TelemetryNotFoundError, TelemetryUpstreamError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise TelemetryUpstreamError("Telemetry request failed") from error
