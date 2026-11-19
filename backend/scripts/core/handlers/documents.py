from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pymongo.errors import PyMongoError
from redis.exceptions import RedisError

from backend.scripts.common.dependencies import current_user, document_service
from backend.scripts.core.models.documents import (
    DocumentCreate,
    DocumentPage,
    DocumentPatch,
    DocumentStatus,
    DocumentView,
)
from backend.scripts.core.services.documents import DocumentService
from backend.scripts.core.utils.versioning import public_document

router = APIRouter()


@router.post("/documents", response_model=DocumentView, status_code=201, tags=["documents"])
async def submit_document(
    body: DocumentCreate,
    response: Response,
    user_id: str = Depends(current_user),
    service: DocumentService = Depends(document_service),
):
    document, created = await service.submit(body, user_id)
    if not created:
        response.status_code = 200
    return public_document(document)


@router.get("/documents/by-ref/{client_doc_ref}", response_model=DocumentView, tags=["documents"])
async def get_document_by_ref(
    client_doc_ref: str,
    user_id: str = Depends(current_user),
    service: DocumentService = Depends(document_service),
):
    document = await service.repository.get_by_ref(client_doc_ref)
    if document is None or document["user_id"] != user_id:
        raise HTTPException(404, "Document not found")
    return public_document(document)


@router.get("/documents/{document_id}", response_model=DocumentView, tags=["documents"])
async def get_document(
    document_id: str,
    user_id: str = Depends(current_user),
    service: DocumentService = Depends(document_service),
):
    document = await service.get_owned(document_id, user_id)
    return public_document(document)


@router.patch("/documents/{document_id}", response_model=DocumentView, tags=["documents"])
async def patch_document(
    document_id: str,
    body: DocumentPatch,
    user_id: str = Depends(current_user),
    service: DocumentService = Depends(document_service),
):
    document = await service.patch(document_id, body, user_id)
    return public_document(document)


@router.get("/users/{user_id}/documents", response_model=DocumentPage, tags=["users"])
async def list_documents(
    user_id: str,
    owner: str = Depends(current_user),
    service: DocumentService = Depends(document_service),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=20, ge=1, le=100),
    status: DocumentStatus | None = None,
):
    if user_id != owner:
        raise HTTPException(404, "User not found")
    documents = await service.repository.list_owned(owner, page, page_size, status)
    return {
        "page": page,
        "page_size": page_size,
        "items": [public_document(item) for item in documents],
    }


@router.get("/health", tags=["operations"])
async def health(response: Response, service: DocumentService = Depends(document_service)):
    checks = {}
    try:
        await service.repository.documents.database.command("ping")
        checks["mongodb"] = "ok"
    except PyMongoError:
        checks["mongodb"] = "unavailable"
    try:
        await service.redis.client.ping()
        checks["redis"] = "ok"
    except RedisError:
        checks["redis"] = "unavailable"
    healthy = all(value == "ok" for value in checks.values())
    response.status_code = 200 if healthy else 503
    return {"status": "ok" if healthy else "degraded", **checks}
