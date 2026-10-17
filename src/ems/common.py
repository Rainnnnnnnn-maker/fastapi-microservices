"""HTTP conventions and the small public reading contract shared by both services."""

import json
import logging
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
)

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")]
bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger("ems")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler(sys.stdout))
logger.propagate = False


class Reading(BaseModel):
    """One site's imported energy for a completed, fixed 30-minute interval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site_id: Identifier
    interval_start: AwareDatetime
    energy_kwh: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)

    @field_validator("interval_start")
    @classmethod
    def aligned_interval(cls, value: datetime) -> datetime:
        try:
            value = value.astimezone(UTC)
            value + timedelta(minutes=30)
        except OverflowError as error:
            raise ValueError("interval is outside the supported datetime range") from error
        if value.minute % 30 or value.second or value.microsecond:
            raise ValueError("interval_start must align to a UTC 30-minute boundary")
        return value

    @computed_field
    @property
    def interval_end(self) -> datetime:
        return self.interval_start + timedelta(minutes=30)

    @computed_field
    @property
    def average_power_kw(self) -> float:
        return self.energy_kwh * 2


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    service: str


async def require_api_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    env = request.scope.get("env")
    expected = getattr(env, "API_TOKEN", None) if env is not None else request.app.state.api_token
    if not expected:
        raise HTTPException(503, "API_TOKEN is not configured")
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), str(expected).encode()
    ):
        raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})


def make_app(service_name: str) -> FastAPI:
    app = FastAPI(title=f"EMS {service_name}", version="0.1.0")
    app.state.api_token = None

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        supplied_id = request.headers.get("x-request-id", "")
        request_id = (
            supplied_id
            if 0 < len(supplied_id) <= 64
            and all(c.isascii() and (c.isalnum() or c in "-_") for c in supplied_id)
            else str(uuid4())
        )
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        logger.info(
            json.dumps(
                {
                    "service": service_name,
                    "request_id": request_id,
                    "method": request.method,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
        )
        return response

    @app.get("/healthz", response_model=Health, tags=["health"])
    async def healthz() -> Health:
        return Health(service=service_name)

    return app
