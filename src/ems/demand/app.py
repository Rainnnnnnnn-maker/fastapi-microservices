"""Assess completed half-hour demand against a site's contract limit."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel

from ems.common import Identifier, make_app, require_api_token
from ems.demand.client import (
    ServiceBindingTelemetryClient,
    TelemetryClient,
    TelemetryNotFoundError,
    TelemetryTimeoutError,
    TelemetryUpstreamError,
)


class DemandAssessment(BaseModel):
    site_id: Identifier
    interval_start: AwareDatetime
    interval_end: AwareDatetime
    demand_kw: float
    contract_kw: float
    excess_kw: float
    utilization_percent: float
    status: Literal["within_limit", "exceeded"]


async def get_telemetry_client(request: Request) -> TelemetryClient:
    if request.app.state.client is not None:
        return request.app.state.client
    env = request.scope.get("env")
    binding = getattr(env, "TELEMETRY", None) if env is not None else None
    if binding is None:
        raise HTTPException(503, "TELEMETRY binding is not configured")
    # require_api_token already verified this credential. Forward the same
    # credential to the private service so both hops enforce authentication.
    authorization = request.headers.get("authorization", "")
    token = authorization.partition(" ")[2]
    if not token:
        raise HTTPException(503, "Telemetry API token is not configured")
    return ServiceBindingTelemetryClient(binding, token)


Client = Annotated[TelemetryClient, Depends(get_telemetry_client)]
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_token)])


@router.get(
    "/sites/{site_id}/demand",
    response_model=DemandAssessment,
    description=(
        "Compare the latest completed 30-minute average power with contract_kw. "
        "A value equal to the contract limit is classified as within_limit. "
        "Readings older than 90 minutes after interval_end are rejected."
    ),
)
async def assess_demand(
    site_id: Identifier,
    contract_kw: Annotated[float, Query(ge=0.001, le=1_000_000, allow_inf_nan=False)],
    request: Request,
    client: Client,
) -> DemandAssessment:
    try:
        reading = await client.latest(site_id, request.state.request_id)
    except TelemetryNotFoundError as error:
        raise HTTPException(404, "No readings found for this site") from error
    except TelemetryTimeoutError as error:
        raise HTTPException(504, "Telemetry request timed out") from error
    except TelemetryUpstreamError as error:
        raise HTTPException(502, "Telemetry request failed") from error
    age = datetime.now(UTC) - reading.interval_end
    if age < timedelta(0):
        raise HTTPException(502, "Telemetry returned an incomplete interval")
    if age > timedelta(minutes=90):
        raise HTTPException(409, "Latest reading is older than 90 minutes")
    demand_kw = reading.average_power_kw
    return DemandAssessment(
        site_id=site_id,
        interval_start=reading.interval_start,
        interval_end=reading.interval_end,
        demand_kw=demand_kw,
        contract_kw=contract_kw,
        excess_kw=max(0, demand_kw - contract_kw),
        utilization_percent=round(demand_kw / contract_kw * 100, 2),
        status="exceeded" if demand_kw > contract_kw else "within_limit",
    )


def create_app(client: TelemetryClient | None = None, api_token: str | None = None) -> FastAPI:
    app = make_app("demand")
    app.state.client = client
    app.state.api_token = api_token
    app.include_router(router)
    return app
