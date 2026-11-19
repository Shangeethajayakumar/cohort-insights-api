"""Two-stage pipeline with bounded retries and version-safe result writes."""

import asyncio
import logging
import random
import re
from collections import Counter

from pymongo.errors import PyMongoError
from redis.exceptions import RedisError

from backend.config.settings import Settings
from backend.scripts.core.services.documents import DocumentService
from backend.scripts.core.utils.versioning import result_is_current

logger = logging.getLogger(__name__)


class StageFailure(Exception):
    """An expected simulated stage failure, eligible for a bounded retry."""


class PipelineWorker:
    def __init__(self, service: DocumentService, settings: Settings):
        self.service = service
        self.repository = service.repository
        self.settings = settings

    async def simulate(self, document: dict, stage: str):
        if stage == "processing":
            minimum = self.settings.processing_min_seconds
            maximum = self.settings.processing_max_seconds
        else:
            minimum = self.settings.enriching_min_seconds
            maximum = self.settings.enriching_max_seconds
        await asyncio.sleep(random.uniform(minimum, maximum))
        if random.random() < self.settings.failure_probability:
            raise StageFailure(f"Simulated {stage} failure")
        if stage == "processing":
            return " ".join(document["content"].split())[:240]
        summary = document["stages"]["processing"]["result"]
        if not result_is_current(document, summary):
            raise StageFailure("Summary does not match the current content version")
        words = re.findall(r"\b[a-zA-Z]{3,}\b", summary["value"].lower())
        return [word for word, count in Counter(words).most_common(5)]

    async def run_once(self) -> bool:
        claimed = await self.repository.claim(self.settings.lease_seconds)
        if claimed is None:
            return False
        stage = (
            "enriching"
            if claimed["stages"]["processing"]["status"] == "completed"
            else "processing"
        )
        document = await self.repository.start_stage(claimed, stage)
        if document is None:
            return True
        logger.info(
            "stage_started",
            extra={
                "document_id": str(document["_id"]),
                "stage": stage,
                "content_version": document["content_version"],
            },
        )
        try:
            value = await self.simulate(document, stage)
        except StageFailure as error:
            attempts = document["stages"][stage]["attempts"]
            delay = None
            if attempts < self.settings.max_stage_attempts:
                delay = self.settings.retry_base_seconds * (2 ** (attempts - 1))
            saved = await self.repository.fail_stage(document, stage, str(error), delay)
        else:
            saved = await self.repository.complete_stage(document, stage, value)

        if saved is None:
            logger.info(
                "outdated_stage_discarded",
                extra={"document_id": str(document["_id"]), "stage": stage},
            )
            return True
        logger.info("stage_saved", extra={"document_id": str(saved["_id"]), "stage": stage})
        if saved["status"] == "completed":
            await self.service.redis.cache_result(
                saved["user_id"],
                saved["content_hash"],
                saved["stages"]["processing"]["result"]["value"],
                saved["stages"]["enriching"]["result"]["value"],
            )
        if saved["status"] in ("completed", "failed"):
            await self.service.redis.release_safely(saved["user_id"], saved["reservation_token"])
        return True

    async def run_forever(self):
        while True:
            try:
                worked = await self.run_once()
            except (PyMongoError, RedisError):
                logger.exception("worker_dependency_unavailable")
                worked = False
            if not worked:
                await asyncio.sleep(self.settings.worker_poll_seconds)
