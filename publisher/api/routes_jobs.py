"""Publishing jobs submission, querying, and cancellation endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from publisher.config import PublisherSettings
from publisher.models import PublishJob, generate_id, utc_now
from publisher.schemas import (
    JobCreateRequest,
    JobCreateResponse,
    JobDetailResponse,
    JobListResponse,
    PublishMode,
)
from publisher.security import require_api_auth

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


def get_db(request: Request) -> object:
    return request.app.state.session_factory


def get_settings(request: Request) -> PublisherSettings:
    return request.app.state.settings


@router.post(
    "",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_auth)],
)
async def create_job(
    request_data: JobCreateRequest,
    session_factory: Annotated[object, Depends(get_db)],
    settings: Annotated[PublisherSettings, Depends(get_settings)],
) -> JobCreateResponse:
    # 1. Resolve effective mode if not specified
    effective_mode: PublishMode = request_data.mode or (
        settings.wechat_mp_default_mode
        if request_data.platform == "wechat_mp"
        else settings.xhs_default_mode
    )

    async with session_factory() as session:
        # 2. Idempotency check via client_request_id
        stmt = select(PublishJob).where(
            PublishJob.client_request_id == request_data.client_request_id
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            return JobCreateResponse(
                id=existing.id,
                status=existing.status,
                effective_mode=existing.mode,
            )

        # 3. Create and enqueue new job
        job_id = generate_id("job")
        job = PublishJob(
            id=job_id,
            client_request_id=request_data.client_request_id,
            platform=request_data.platform,
            mode=effective_mode,
            status="queued",
            publish_phase=None,
            content=request_data.content.model_dump(),
            media=[m.model_dump() for m in request_data.media],
            source_url=request_data.source_url,
            topics=request_data.topics,
            attempt_count=0,
            max_attempts=2,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(job)
        await session.commit()

        return JobCreateResponse(
            id=job.id,
            status=job.status,
            effective_mode=job.mode,
        )


@router.get(
    "",
    response_model=JobListResponse,
    dependencies=[Depends(require_api_auth)],
)
async def list_jobs(
    session_factory: Annotated[object, Depends(get_db)],
    platform: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> JobListResponse:
    async with session_factory() as session:
        query = select(PublishJob)
        count_query = select(func.count(PublishJob.id))

        if platform:
            query = query.where(PublishJob.platform == platform)
            count_query = count_query.where(PublishJob.platform == platform)
        if status_filter:
            query = query.where(PublishJob.status == status_filter)
            count_query = count_query.where(PublishJob.status == status_filter)

        total = (await session.execute(count_query)).scalar_one()

        offset = (page - 1) * page_size
        query = query.order_by(PublishJob.created_at.desc()).offset(offset).limit(page_size)
        items = (await session.execute(query)).scalars().all()

        return JobListResponse(
            items=[JobDetailResponse.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
        )


@router.get(
    "/{job_id}",
    response_model=JobDetailResponse,
    dependencies=[Depends(require_api_auth)],
)
async def get_job(
    job_id: str,
    session_factory: Annotated[object, Depends(get_db)],
) -> JobDetailResponse:
    async with session_factory() as session:
        job = await session.get(PublishJob, job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Publish job '{job_id}' not found",
            )
        return JobDetailResponse.model_validate(job)


@router.post(
    "/{job_id}/cancel",
    response_model=JobDetailResponse,
    dependencies=[Depends(require_api_auth)],
)
async def cancel_job(
    job_id: str,
    session_factory: Annotated[object, Depends(get_db)],
) -> JobDetailResponse:
    async with session_factory() as session:
        job = await session.get(PublishJob, job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Publish job '{job_id}' not found",
            )
        if job.status not in ("queued", "waiting_auth"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot cancel job in '{job.status}' status (only 'queued' or 'waiting_auth' can be cancelled)",
            )

        job.status = "cancelled"
        job.finished_at = utc_now()
        job.updated_at = utc_now()
        await session.commit()
        return JobDetailResponse.model_validate(job)
