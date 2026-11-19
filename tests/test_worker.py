import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from redis.exceptions import ConnectionError as RedisConnectionError

from backend.scripts.core.services.pipeline import StageFailure
from backend.scripts.core.utils.versioning import utc_now


async def create_document(environment, payload):
    response = await environment.client.post("/documents", json=payload)
    assert response.status_code == 201
    return response.json()["document_id"]


@pytest.mark.parametrize("stage", ["processing", "enriching"])
async def test_patch_during_stage_discards_old_worker_result(
    environment, payload, monkeypatch, stage
):
    document_id = await create_document(environment, payload)
    if stage == "enriching":
        await environment.worker.run_once()
    entered = asyncio.Event()
    proceed = asyncio.Event()

    async def delayed_stage(document, stage_name):
        entered.set()
        await proceed.wait()
        return "Old summary" if stage_name == "processing" else ["old"]

    monkeypatch.setattr(environment.worker, "simulate", delayed_stage)
    running = asyncio.create_task(environment.worker.run_once())
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        patched = await environment.client.patch(
            f"/documents/{document_id}", json={"content": "Replacement", "expected_version": 1}
        )
        assert patched.status_code == 200
    finally:
        proceed.set()
        await running
    current = (await environment.client.get(f"/documents/{document_id}")).json()
    assert current["content_version"] == 2
    assert current["status"] == "queued"
    assert current["summary"] is None
    assert current["tags"] is None
    assert current["stages"]["processing"]["result_version"] is None
    assert len(await environment.store.tokens("alice")) == 1


async def test_only_one_worker_claims_a_document(environment, payload):
    await create_document(environment, payload)
    claims = await asyncio.gather(*(environment.repository.claim(60) for attempt in range(8)))
    assert sum(claim is not None for claim in claims) == 1


async def test_expired_claim_is_recovered_and_old_token_is_rejected(environment, payload):
    document_id = await create_document(environment, payload)
    old_claim = await environment.repository.claim(60)
    old_stage = await environment.repository.start_stage(old_claim, "processing")
    await environment.repository.documents.update_one(
        {"_id": ObjectId(document_id)}, {"$set": {"lease_until": utc_now() - timedelta(seconds=1)}}
    )
    new_claim = await environment.repository.claim(60)
    assert new_claim["claim_token"] != old_claim["claim_token"]
    assert await environment.repository.complete_stage(old_stage, "processing", "Old") is None
    new_stage = await environment.repository.start_stage(new_claim, "processing")
    assert (
        await environment.repository.complete_stage(new_stage, "processing", "Current") is not None
    )


async def test_enrichment_retry_preserves_summary(environment, payload, monkeypatch):
    document_id = await create_document(environment, payload)
    await environment.worker.run_once()
    original = await environment.repository.get_owned(document_id, "alice")
    summary = original["stages"]["processing"]["result"]
    monkeypatch.setattr(
        environment.worker,
        "simulate",
        AsyncMock(side_effect=[StageFailure("Tagging unavailable"), ["python"]]),
    )
    await environment.worker.run_once()
    retrying = await environment.repository.get_owned(document_id, "alice")
    assert retrying["stages"]["enriching"]["status"] == "retry_wait"
    assert retrying["stages"]["processing"]["result"] == summary
    assert len(await environment.store.tokens("alice")) == 1
    await environment.worker.run_once()
    completed = await environment.repository.get_owned(document_id, "alice")
    assert completed["status"] == "completed"
    assert completed["stages"]["processing"]["attempts"] == 1
    assert completed["stages"]["enriching"]["attempts"] == 2


@pytest.mark.parametrize("stage", ["processing", "enriching"])
async def test_terminal_failure_identifies_stage_and_releases_slot(
    environment, payload, monkeypatch, stage
):
    document_id = await create_document(environment, payload)
    if stage == "enriching":
        await environment.worker.run_once()
    environment.settings.max_stage_attempts = 1
    monkeypatch.setattr(
        environment.worker, "simulate", AsyncMock(side_effect=StageFailure("Expected failure"))
    )
    await environment.worker.run_once()
    failed = (await environment.client.get(f"/documents/{document_id}")).json()
    assert failed["status"] == "failed"
    assert failed["stages"][stage]["status"] == "failed"
    assert failed["stages"][stage]["error"] == "Expected failure"
    assert failed["summary"] is None
    assert failed["tags"] is None
    assert await environment.store.tokens("alice") == []


async def test_backoff_delays_retry(environment, payload, monkeypatch):
    document_id = await create_document(environment, payload)
    environment.settings.retry_base_seconds = 30
    monkeypatch.setattr(
        environment.worker, "simulate", AsyncMock(side_effect=StageFailure("Retry later"))
    )
    await environment.worker.run_once()
    document = await environment.repository.get_owned(document_id, "alice")
    assert document["next_attempt_at"] > utc_now() + timedelta(seconds=25)
    assert await environment.worker.run_once() is False


async def test_missed_terminal_release_is_reconciled_on_submission(
    environment, payload, monkeypatch
):
    await create_document(environment, payload)
    await environment.worker.run_once()
    with monkeypatch.context() as temporary:
        temporary.setattr(environment.redis, "srem", AsyncMock(side_effect=RedisConnectionError()))
        await environment.worker.run_once()
    assert len(await environment.store.tokens("alice")) == 1
    response = await environment.client.post("/documents", json={**payload, "content": "Next"})
    assert response.status_code == 201
    assert len(await environment.store.tokens("alice")) == 1


async def test_crosswalk_has_unique_partial_index(environment):
    indexes = await environment.repository.documents.index_information()
    reference_index = next(
        index for index in indexes.values() if index["key"] == [("client_doc_ref", 1)]
    )
    assert reference_index["unique"]
    assert reference_index["partialFilterExpression"] == {"client_doc_ref": {"$type": "string"}}
