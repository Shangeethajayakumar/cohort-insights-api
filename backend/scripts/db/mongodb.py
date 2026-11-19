"""MongoDB queries; a document is also its durable pipeline job."""

from datetime import timedelta
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from backend.scripts.core.utils.versioning import ACTIVE_STATUSES, utc_now, versioned_result


class DocumentRepository:
    def __init__(self, database):
        self.documents = database.documents

    async def create_indexes(self):
        await self.documents.create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)]
        )
        await self.documents.create_index(
            [
                ("user_id", ASCENDING),
                ("status", ASCENDING),
                ("created_at", DESCENDING),
                ("_id", DESCENDING),
            ]
        )
        await self.documents.create_index(
            [("client_doc_ref", ASCENDING)],
            unique=True,
            partialFilterExpression={"client_doc_ref": {"$type": "string"}},
        )
        await self.documents.create_index(
            [("user_id", ASCENDING), ("content_hash", ASCENDING), ("status", ASCENDING)]
        )
        await self.documents.create_index(
            [("status", ASCENDING), ("next_attempt_at", ASCENDING), ("lease_until", ASCENDING)]
        )
        await self.documents.create_index("reservation_token", sparse=True)

    async def insert(self, document: dict):
        await self.documents.insert_one(document)

    async def get_owned(self, document_id: str, user_id: str):
        if not ObjectId.is_valid(document_id):
            return None
        return await self.documents.find_one({"_id": ObjectId(document_id), "user_id": user_id})

    async def get_by_ref(self, reference: str):
        return await self.documents.find_one({"client_doc_ref": reference})

    async def list_owned(self, user_id: str, page: int, page_size: int, status: str | None):
        query = {"user_id": user_id}
        if status:
            query["status"] = status
        cursor = (
            self.documents.find(query)
            .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        return await cursor.to_list(length=page_size)

    async def find_completed(self, user_id: str, digest: str):
        return await self.documents.find_one(
            {
                "user_id": user_id,
                "content_hash": digest,
                "status": "completed",
                "pipeline_version": "v1",
            }
        )

    async def active_tokens(self, user_id: str):
        cursor = self.documents.find(
            {"user_id": user_id, "status": {"$in": list(ACTIVE_STATUSES)}},
            {"reservation_token": 1},
        )
        return [document["reservation_token"] async for document in cursor]

    async def terminal_tokens(self, user_id: str, tokens: list[str]):
        cursor = self.documents.find(
            {
                "user_id": user_id,
                "status": {"$in": ["completed", "failed"]},
                "reservation_token": {"$in": tokens},
            },
            {"reservation_token": 1},
        )
        return [document["reservation_token"] async for document in cursor]

    async def replace_content(self, previous: dict, changes: dict):
        return await self.documents.find_one_and_update(
            {
                "_id": previous["_id"],
                "user_id": previous["user_id"],
                "content_version": previous["content_version"],
                "status": previous["status"],
            },
            {"$set": changes},
            return_document=ReturnDocument.AFTER,
        )

    async def claim(self, lease_seconds: int):
        now = utc_now()
        return await self.documents.find_one_and_update(
            {
                "status": {"$in": list(ACTIVE_STATUSES)},
                "next_attempt_at": {"$lte": now},
                "$or": [{"lease_until": None}, {"lease_until": {"$lte": now}}],
            },
            {
                "$set": {
                    "claim_token": uuid4().hex,
                    "lease_until": now + timedelta(seconds=lease_seconds),
                }
            },
            sort=[("next_attempt_at", ASCENDING), ("_id", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    def claim_filter(self, document: dict):
        return {
            "_id": document["_id"],
            "content_version": document["content_version"],
            "claim_token": document["claim_token"],
            "lease_until": {"$gt": utc_now()},
        }

    async def start_stage(self, document: dict, stage: str):
        return await self.documents.find_one_and_update(
            self.claim_filter(document),
            {
                "$set": {
                    "status": stage,
                    f"stages.{stage}.status": "running",
                    f"stages.{stage}.error": None,
                    "updated_at": utc_now(),
                },
                "$inc": {f"stages.{stage}.attempts": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def complete_stage(self, document: dict, stage: str, value):
        return await self.documents.find_one_and_update(
            self.claim_filter(document),
            {
                "$set": {
                    f"stages.{stage}.status": "completed",
                    f"stages.{stage}.error": None,
                    f"stages.{stage}.result": versioned_result(document, value),
                    "status": "enriching" if stage == "processing" else "completed",
                    "claim_token": None,
                    "lease_until": None,
                    "next_attempt_at": utc_now(),
                    "updated_at": utc_now(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def fail_stage(self, document: dict, stage: str, error: str, retry_delay: float | None):
        terminal = retry_delay is None
        return await self.documents.find_one_and_update(
            self.claim_filter(document),
            {
                "$set": {
                    "status": "failed" if terminal else stage,
                    f"stages.{stage}.status": "failed" if terminal else "retry_wait",
                    f"stages.{stage}.error": error,
                    "claim_token": None,
                    "lease_until": None,
                    "next_attempt_at": utc_now() + timedelta(seconds=retry_delay or 0),
                    "updated_at": utc_now(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
