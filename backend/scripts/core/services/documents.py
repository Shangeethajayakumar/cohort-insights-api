"""Document use cases, with HTTP errors kept close to the request decisions."""

from uuid import uuid4

from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from backend.scripts.core.models.documents import DocumentCreate, DocumentPatch
from backend.scripts.core.utils.versioning import (
    ACTIVE_STATUSES,
    PIPELINE_VERSION,
    apply_cached_results,
    content_hash,
    new_stages,
    public_document,
    utc_now,
)
from backend.scripts.db.mongodb import DocumentRepository
from backend.scripts.db.redis_store import MAX_ACTIVE_DOCUMENTS, RedisStore


class DocumentService:
    def __init__(self, repository: DocumentRepository, redis_store: RedisStore):
        self.repository = repository
        self.redis = redis_store

    async def get_owned(self, document_id: str, user_id: str) -> dict:
        document = await self.repository.get_owned(document_id, user_id)
        if document is None:
            raise HTTPException(404, "Document not found")
        return document

    def check_repeat(self, existing: dict, request: DocumentCreate, user_id: str):
        if (
            existing["user_id"] == user_id
            and existing["title"] == request.title
            and existing["content"] == request.content
        ):
            return existing
        raise HTTPException(409, "External reference is already in use")

    async def reserve_slot(self, user_id: str, token: str):
        tracked = await self.redis.tokens(user_id)
        for finished in await self.repository.terminal_tokens(user_id, tracked):
            await self.redis.release(user_id, finished)
        active = await self.repository.active_tokens(user_id)
        if not await self.redis.reserve(user_id, token, active):
            raise HTTPException(
                429,
                f"At most {MAX_ACTIVE_DOCUMENTS} documents may be active per user",
                headers={"Retry-After": "2"},
            )

    async def cached_result(self, user_id: str, digest: str):
        cached = await self.redis.get_cached(user_id, digest)
        if cached is not None:
            return cached
        previous = await self.repository.find_completed(user_id, digest)
        if previous is not None:
            visible = public_document(previous)
            if visible["results_current"]:
                await self.redis.cache_result(user_id, digest, visible["summary"], visible["tags"])
                return {"summary": visible["summary"], "tags": visible["tags"]}
        return None

    async def submit(self, request: DocumentCreate, user_id: str):
        if request.user_id != user_id:
            raise HTTPException(403, "user_id must match the authenticated caller")
        if request.client_doc_ref is not None:
            existing = await self.repository.get_by_ref(request.client_doc_ref)
            if existing is not None:
                return self.check_repeat(existing, request, user_id), False

        now = utc_now()
        document = {
            "_id": ObjectId(),
            "user_id": user_id,
            "title": request.title,
            "content": request.content,
            "content_hash": content_hash(request.content),
            "content_version": 1,
            "pipeline_version": PIPELINE_VERSION,
            "status": "queued",
            "stages": new_stages(),
            "created_at": now,
            "updated_at": now,
            "next_attempt_at": now,
            "claim_token": None,
            "lease_until": None,
            "reservation_token": None,
        }
        if request.client_doc_ref is not None:
            document["client_doc_ref"] = request.client_doc_ref
        cached = await self.cached_result(user_id, document["content_hash"])
        if cached is not None:
            apply_cached_results(document, cached)
        else:
            document["reservation_token"] = uuid4().hex
            await self.reserve_slot(user_id, document["reservation_token"])

        try:
            await self.repository.insert(document)
        except DuplicateKeyError:
            if document["reservation_token"]:
                await self.redis.release_safely(user_id, document["reservation_token"])
            existing = await self.repository.get_by_ref(request.client_doc_ref)
            if existing is not None:
                return self.check_repeat(existing, request, user_id), False
            raise
        return document, True

    async def patch(self, document_id: str, request: DocumentPatch, user_id: str):
        previous = await self.get_owned(document_id, user_id)
        if (
            request.expected_version is not None
            and request.expected_version != previous["content_version"]
        ):
            raise HTTPException(409, "Content version changed; read the document and retry")

        already_active = previous["status"] in ACTIVE_STATUSES
        if already_active:
            await self.redis.client.ping()
            token = previous["reservation_token"]
        else:
            if previous["reservation_token"]:
                await self.redis.release(user_id, previous["reservation_token"])
            token = uuid4().hex
            await self.reserve_slot(user_id, token)

        changes = {
            "content": request.content,
            "content_hash": content_hash(request.content),
            "content_version": previous["content_version"] + 1,
            "pipeline_version": PIPELINE_VERSION,
            "status": "queued",
            "stages": new_stages(),
            "updated_at": utc_now(),
            "next_attempt_at": utc_now(),
            "claim_token": None,
            "lease_until": None,
            "reservation_token": token,
        }
        updated = await self.repository.replace_content(previous, changes)
        if updated is None:
            if not already_active:
                await self.redis.release_safely(user_id, token)
            raise HTTPException(409, "Document changed during update; read it and retry")
        return updated
