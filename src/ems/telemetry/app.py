"""HTTP API for completed half-hour electricity measurements."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status

from ems.common import Identifier, Reading, make_app, require_api_token
from ems.telemetry.repository import ConflictError, D1ReadingRepository, ReadingRepository


async def get_repository(request: Request) -> ReadingRepository:
    repository = request.app.state.repository
    if repository is not None:
        return repository
    env = request.scope.get("env")
    db = getattr(env, "DB", None) if env is not None else None
    if db is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "DB binding is not configured")
    return D1ReadingRepository(db)


Repository = Annotated[ReadingRepository, Depends(get_repository)]
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_token)])


@router.post("/readings", status_code=status.HTTP_201_CREATED, response_model=Reading)
async def create_reading(reading: Reading, repository: Repository) -> Reading:
    now = datetime.now(UTC)
    if reading.interval_start > now or reading.interval_end > now:
        raise HTTPException(
            422,
            "Only completed 30-minute intervals can be registered",
        )
    try:
        await repository.save(reading)
    except ConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return reading


@router.get("/sites/{site_id}/readings/latest", response_model=Reading)
async def latest_reading(site_id: Identifier, repository: Repository) -> Reading:
    reading = await repository.latest(site_id)
    if reading is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No readings found for this site")
    return reading


def create_app(
    repository: ReadingRepository | None = None, api_token: str | None = None
) -> FastAPI:
    app = make_app("telemetry")
    app.state.repository = repository
    app.state.api_token = api_token
    app.include_router(router)
    return app
