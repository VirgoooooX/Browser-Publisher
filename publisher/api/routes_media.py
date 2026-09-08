"""Media asset upload and management endpoints."""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import select

from publisher.config import PublisherSettings
from publisher.models import MediaAsset, generate_id, utc_now
from publisher.schemas import MediaUploadResponse
from publisher.security import require_console_auth

router = APIRouter(
    prefix="/v1/media",
    tags=["media"],
    dependencies=[Depends(require_console_auth)],
)

ALLOWED_MIME_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MiB


def get_db(request: Request) -> object:
    return request.app.state.session_factory


def get_settings(request: Request) -> PublisherSettings:
    return request.app.state.settings


@router.post(
    "",
    response_model=MediaUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_media(
    file: Annotated[UploadFile, File(description="Image file (JPG, PNG, WebP)")],
    session_factory: Annotated[object, Depends(get_db)],
    settings: Annotated[PublisherSettings, Depends(get_settings)],
) -> MediaUploadResponse:
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {content_type}. Only JPG, PNG, and WebP images are accepted.",
        )

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file uploaded"
        )
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum size of {MAX_FILE_SIZE // (1024 * 1024)} MiB",
        )

    checksum = hashlib.sha256(content).hexdigest()
    ext = ALLOWED_MIME_TYPES[content_type]

    async with session_factory() as session:
        # Check if identical checksum already exists
        stmt = select(MediaAsset).where(MediaAsset.checksum == checksum)
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            return MediaUploadResponse(
                media_id=existing.id,
                file_name=existing.file_name,
                content_type=existing.content_type,
                size_bytes=existing.size_bytes,
            )

        media_id = generate_id("med")
        safe_filename = f"{media_id}{ext}"
        save_path = settings.media_dir / safe_filename
        save_path.write_bytes(content)

        asset = MediaAsset(
            id=media_id,
            file_name=file.filename or safe_filename,
            file_path=str(save_path.resolve()),
            content_type=content_type,
            size_bytes=len(content),
            checksum=checksum,
            created_at=utc_now(),
        )
        session.add(asset)
        await session.commit()

        return MediaUploadResponse(
            media_id=asset.id,
            file_name=asset.file_name,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
        )
