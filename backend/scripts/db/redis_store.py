"""Redis has two separate jobs: admission slots and completed-result caching."""

import logging
from hashlib import sha256

from pydantic import ValidationError
from redis.exceptions import RedisError

from backend.scripts.core.models.documents import CacheEntry
from backend.scripts.core.utils.versioning import PIPELINE_VERSION

logger = logging.getLogger(__name__)

MAX_ACTIVE_DOCUMENTS = 3

# Keep cleanup, counting, and adding a reservation atomic across concurrent requests.
RESERVE_SLOT = """
for index = 3, #ARGV do
    redis.call('SADD', KEYS[1], ARGV[index])
end
if redis.call('SCARD', KEYS[1]) >= tonumber(ARGV[2]) then
    return 0
end
redis.call('SADD', KEYS[1], ARGV[1])
return 1
"""


class RedisStore:
    def __init__(self, client, cache_ttl_seconds: int, namespace: str = "cohort"):
        self.client = client
        self.cache_ttl_seconds = cache_ttl_seconds
        self.namespace = namespace

    def owner_key(self, user_id: str) -> str:
        return sha256(user_id.encode("utf-8")).hexdigest()

    def active_key(self, user_id: str) -> str:
        return f"{self.namespace}:active:{{{self.owner_key(user_id)}}}"

    def cache_key(self, user_id: str, digest: str) -> str:
        return f"{self.namespace}:cache:{PIPELINE_VERSION}:{self.owner_key(user_id)}:{digest}"

    async def reserve(self, user_id: str, token: str, active_tokens: list[str]) -> bool:
        result = await self.client.eval(
            RESERVE_SLOT,
            1,
            self.active_key(user_id),
            token,
            MAX_ACTIVE_DOCUMENTS,
            *active_tokens,
        )
        return bool(result)

    async def release(self, user_id: str, token: str):
        await self.client.srem(self.active_key(user_id), token)

    async def release_safely(self, user_id: str, token: str):
        try:
            await self.release(user_id, token)
        except RedisError:
            logger.exception("slot_release_deferred", extra={"user_id": user_id})

    async def tokens(self, user_id: str) -> list[str]:
        return list(await self.client.smembers(self.active_key(user_id)))

    async def get_cached(self, user_id: str, digest: str) -> dict | None:
        try:
            value = await self.client.get(self.cache_key(user_id, digest))
            if not value:
                return None
            cached = CacheEntry.model_validate_json(value)
            if cached.content_hash != digest or cached.pipeline_version != PIPELINE_VERSION:
                return None
            return cached.model_dump()
        except (RedisError, ValidationError):
            logger.warning("cache_read_unavailable_or_invalid", extra={"user_id": user_id})
            return None

    async def cache_result(self, user_id: str, digest: str, summary: str, tags: list[str]):
        entry = CacheEntry(
            content_hash=digest, pipeline_version=PIPELINE_VERSION, summary=summary, tags=tags
        )
        try:
            await self.client.set(
                self.cache_key(user_id, digest),
                entry.model_dump_json(),
                ex=self.cache_ttl_seconds,
            )
        except RedisError:
            logger.warning("cache_write_unavailable", extra={"user_id": user_id})
