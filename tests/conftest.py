import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient
from pymongo import AsyncMongoClient
from redis.asyncio import Redis

from backend.app import create_app
from backend.config.settings import Settings
from backend.scripts.core.services.documents import DocumentService
from backend.scripts.core.services.pipeline import PipelineWorker
from backend.scripts.db.mongodb import DocumentRepository
from backend.scripts.db.redis_store import RedisStore


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="Use real local MongoDB and Redis")


@pytest_asyncio.fixture
async def environment(request):
    name = f"cohort_test_{uuid4().hex}"
    live = request.config.getoption("--live")
    if live:
        mongo = AsyncMongoClient(
            os.getenv("TEST_MONGO_URL", "mongodb://localhost:27017"),
            tz_aware=True,
            serverSelectionTimeoutMS=3000,
        )
        redis = Redis.from_url(
            os.getenv("TEST_REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
        await mongo.admin.command("ping")
        await redis.ping()
    else:
        mongo = AsyncMongoMockClient(tz_aware=True)
        redis = FakeRedis(decode_responses=True)

    settings = Settings(
        _env_file=None,
        mongo_url="mongodb://localhost:27017",
        mongo_database=name,
        redis_url="redis://localhost:6379/0",
        api_keys={"demo-alice-key": "alice", "demo-bob-key": "bob"},
        processing_min_seconds=0,
        processing_max_seconds=0,
        enriching_min_seconds=0,
        enriching_max_seconds=0,
        failure_probability=0,
        retry_base_seconds=0,
    )
    repository = DocumentRepository(mongo[name])
    if not live:
        repository.documents.database.command = AsyncMock(return_value={"ok": 1})
    await repository.create_indexes()
    store = RedisStore(redis, cache_ttl_seconds=60, namespace=name)
    service = DocumentService(repository, store)
    application = create_app(settings, service)
    try:
        async with (
            application.router.lifespan_context(application),
            AsyncClient(
                transport=ASGITransport(app=application),
                base_url="http://test",
                headers={"Authorization": "Bearer demo-alice-key"},
            ) as client,
        ):
            yield SimpleNamespace(
                client=client,
                repository=repository,
                redis=redis,
                store=store,
                service=service,
                settings=settings,
                worker=PipelineWorker(service, settings),
            )
    finally:
        keys = [key async for key in redis.scan_iter(match=f"{name}:*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
        await mongo.drop_database(name)
        if live:
            await mongo.close()
        else:
            mongo.close()


@pytest.fixture
def payload():
    return {
        "user_id": "alice",
        "title": "Learning Python",
        "content": "Python pipelines generate useful insights.",
    }
