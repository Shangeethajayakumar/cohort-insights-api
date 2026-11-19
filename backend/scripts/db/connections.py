"""Open and close the shared MongoDB and Redis connections."""

from contextlib import asynccontextmanager

from pymongo import AsyncMongoClient
from redis.asyncio import Redis

from backend.config.settings import Settings
from backend.scripts.core.services.documents import DocumentService
from backend.scripts.db.mongodb import DocumentRepository
from backend.scripts.db.redis_store import RedisStore


@asynccontextmanager
async def connect_service(settings: Settings):
    mongo = AsyncMongoClient(
        settings.mongo_url,
        tz_aware=True,
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
        timeoutMS=5000,
    )
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        repository = DocumentRepository(mongo[settings.mongo_database])
        await repository.create_indexes()
        yield DocumentService(repository, RedisStore(redis, settings.cache_ttl_seconds))
    finally:
        await redis.aclose()
        await mongo.close()
