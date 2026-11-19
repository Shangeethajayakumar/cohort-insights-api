import asyncio
from unittest.mock import AsyncMock

import pytest
from pymongo.errors import NetworkTimeout
from redis.exceptions import ConnectionError as RedisConnectionError


async def submit(environment, payload):
    response = await environment.client.post("/documents", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def finish(environment):
    assert await environment.worker.run_once()
    assert await environment.worker.run_once()


async def test_submit_pipeline_and_status(environment, payload):
    document = await submit(environment, payload)
    assert document["status"] == "queued"
    assert document["summary"] is None
    assert not document["results_current"]
    await environment.worker.run_once()
    midway = (await environment.client.get(f"/documents/{document['document_id']}")).json()
    assert midway["status"] == "enriching"
    assert midway["stages"]["processing"]["status"] == "completed"
    assert midway["summary"] is None
    assert midway["tags"] is None
    await environment.worker.run_once()
    completed = (await environment.client.get(f"/documents/{document['document_id']}")).json()
    assert completed["status"] == "completed"
    assert completed["results_current"]
    assert completed["result_version"] == 1
    assert completed["tags"]
    assert await environment.store.tokens("alice") == []


async def test_ownership_for_get_patch_list_and_reference(environment, payload):
    document = await submit(environment, {**payload, "client_doc_ref": "partner-1"})
    other_owner = {"Authorization": "Bearer demo-bob-key"}
    paths = [f"/documents/{document['document_id']}", "/documents/by-ref/partner-1"]
    for path in paths:
        response = await environment.client.get(path, headers=other_owner)
        assert response.status_code == 404
        assert response.json() == {"detail": "Document not found"}
    response = await environment.client.patch(
        paths[0], headers=other_owner, json={"content": "Changed"}
    )
    assert response.status_code == 404
    assert (
        await environment.client.get("/users/alice/documents", headers=other_owner)
    ).status_code == 404
    assert (await environment.client.get("/documents/not-an-id")).status_code == 404


async def test_authentication_and_owner_validation(environment, payload):
    response = await environment.client.post(
        "/documents", json=payload, headers={"Authorization": "Bearer invalid"}
    )
    assert response.status_code == 401
    response = await environment.client.post("/documents", json={**payload, "user_id": "bob"})
    assert response.status_code == 403


@pytest.mark.parametrize(
    "changes",
    [
        {"content": "   "},
        {"content": ""},
        {"title": " "},
        {"content": "x" * 100_001},
        {"title": "x" * 201},
        {"user_id": "bad user"},
        {"client_doc_ref": "bad/ref"},
        {"extra": True},
    ],
)
async def test_invalid_submissions(environment, payload, changes):
    response = await environment.client.post("/documents", json={**payload, **changes})
    assert response.status_code == 422


async def test_crosswalk_repeat_conflict_and_lookup(environment, payload):
    body = {**payload, "client_doc_ref": "cms:123"}
    first = await submit(environment, body)
    repeat = await environment.client.post("/documents", json=body)
    assert repeat.status_code == 200
    assert repeat.json()["document_id"] == first["document_id"]
    conflict = await environment.client.post("/documents", json={**body, "content": "Different"})
    assert conflict.status_code == 409
    found = await environment.client.get("/documents/by-ref/cms:123")
    assert found.json()["document_id"] == first["document_id"]
    assert (await environment.client.get("/documents/by-ref/missing")).status_code == 404
    assert len(await environment.store.tokens("alice")) == 1


async def test_concurrent_duplicate_reference(environment, payload):
    body = {**payload, "client_doc_ref": "same-ref"}
    responses = await asyncio.gather(
        *(environment.client.post("/documents", json=body) for attempt in range(2))
    )
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert len({response.json()["document_id"] for response in responses}) == 1
    assert len(await environment.store.tokens("alice")) == 1


async def test_conflicting_reference_from_another_owner(environment, payload):
    await submit(environment, {**payload, "client_doc_ref": "shared-ref"})
    response = await environment.client.post(
        "/documents",
        json={**payload, "user_id": "bob", "client_doc_ref": "shared-ref"},
        headers={"Authorization": "Bearer demo-bob-key"},
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "External reference is already in use"}


async def test_rate_limit_counts_both_stages_and_queued_documents(environment, payload):
    await submit(environment, payload)
    await environment.worker.run_once()
    await submit(environment, {**payload, "content": "Second"})
    await submit(environment, {**payload, "content": "Third"})
    blocked = await environment.client.post("/documents", json={**payload, "content": "Fourth"})
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "2"
    bob = await environment.client.post(
        "/documents",
        json={**payload, "user_id": "bob"},
        headers={"Authorization": "Bearer demo-bob-key"},
    )
    assert bob.status_code == 201


async def test_atomic_concurrent_limit_and_idempotent_release(environment):
    allowed = await asyncio.gather(
        *(environment.store.reserve("alice", f"token-{number}", []) for number in range(20))
    )
    assert sum(allowed) == 3
    winner = (await environment.store.tokens("alice"))[0]
    await environment.store.release("alice", winner)
    await environment.store.release("alice", winner)
    assert len(await environment.store.tokens("alice")) == 2
    assert await environment.redis.ttl(environment.store.active_key("alice")) == -1


async def test_cache_hit_needs_no_active_slot(environment, payload):
    original = await submit(environment, payload)
    await finish(environment)
    for number in range(3):
        await submit(environment, {**payload, "content": f"Other content {number}"})
    hit = await submit(environment, payload)
    assert hit["status"] == "completed"
    assert hit["document_id"] != original["document_id"]
    assert len(await environment.store.tokens("alice")) == 3
    key = environment.store.cache_key("alice", original["content_hash"])
    assert 0 < await environment.redis.ttl(key) <= 60


async def test_cache_and_patch_are_isolated_by_content(environment, payload):
    original = await submit(environment, payload)
    await finish(environment)
    patched = await environment.client.patch(
        f"/documents/{original['document_id']}",
        json={"content": "New subject", "expected_version": 1},
    )
    updated = patched.json()
    assert patched.status_code == 200
    assert updated["content_version"] == 2
    assert updated["summary"] is None
    assert updated["tags"] is None
    assert updated["status"] == "queued"
    old_content = await submit(environment, payload)
    assert old_content["status"] == "completed"
    new_content = await submit(environment, {**payload, "content": "New subject"})
    assert new_content["status"] == "queued"
    assert new_content["summary"] is None


async def test_cache_is_owner_scoped(environment, payload):
    await submit(environment, payload)
    await finish(environment)
    response = await environment.client.post(
        "/documents",
        json={**payload, "user_id": "bob"},
        headers={"Authorization": "Bearer demo-bob-key"},
    )
    assert response.json()["status"] == "queued"


async def test_expired_or_invalid_cache_falls_back_to_mongodb(environment, payload):
    original = await submit(environment, payload)
    await finish(environment)
    key = environment.store.cache_key("alice", original["content_hash"])
    await environment.redis.set(key, "not valid JSON")
    restored = await submit(environment, payload)
    assert restored["results_current"]
    await environment.redis.delete(key)
    assert (await submit(environment, payload))["results_current"]


async def test_two_patches_with_same_expected_version(environment, payload):
    document = await submit(environment, payload)
    responses = await asyncio.gather(
        *(
            environment.client.patch(
                f"/documents/{document['document_id']}",
                json={"content": f"Revision {number}", "expected_version": 1},
            )
            for number in range(2)
        )
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(await environment.store.tokens("alice")) == 1


async def test_patch_same_content_still_restarts_both_stages(environment, payload):
    document = await submit(environment, payload)
    await finish(environment)
    response = await environment.client.patch(
        f"/documents/{document['document_id']}", json={"content": payload["content"]}
    )
    assert response.json()["status"] == "queued"
    assert response.json()["content_version"] == 2
    assert response.json()["stages"]["processing"]["attempts"] == 0


async def test_reference_retry_after_patch_cannot_overwrite_new_content(environment, payload):
    body = {**payload, "client_doc_ref": "cms-1"}
    document = await submit(environment, body)
    await environment.client.patch(
        f"/documents/{document['document_id']}", json={"content": "Newest"}
    )
    delayed = await environment.client.post("/documents", json=body)
    assert delayed.status_code == 409
    current = (await environment.client.get("/documents/by-ref/cms-1")).json()
    assert current["content"] == "Newest"


async def test_paginated_newest_first_listing_and_filters(environment, payload):
    first = await submit(environment, payload)
    await finish(environment)
    second = await submit(environment, {**payload, "content": "Second"})
    page_one = await environment.client.get("/users/alice/documents?page=1&page_size=1")
    page_two = await environment.client.get("/users/alice/documents?page=2&page_size=1")
    assert page_one.json()["items"][0]["document_id"] == second["document_id"]
    assert page_two.json()["items"][0]["document_id"] == first["document_id"]
    filtered = await environment.client.get("/users/alice/documents?status=completed")
    assert len(filtered.json()["items"]) == 1
    assert (await environment.client.get("/users/alice/documents?page_size=101")).status_code == 422
    assert (await environment.client.get("/users/alice/documents?page=0")).status_code == 422


async def test_redis_outage_preserves_reads_and_rejects_new_work(environment, payload, monkeypatch):
    document = await submit(environment, payload)
    monkeypatch.setattr(environment.redis, "get", AsyncMock(side_effect=RedisConnectionError()))
    monkeypatch.setattr(
        environment.redis, "smembers", AsyncMock(side_effect=RedisConnectionError())
    )
    monkeypatch.setattr(environment.redis, "ping", AsyncMock(side_effect=RedisConnectionError()))
    assert (
        await environment.client.get(f"/documents/{document['document_id']}")
    ).status_code == 200
    assert (await environment.client.post("/documents", json=payload)).status_code == 503
    health = await environment.client.get("/health")
    assert health.status_code == 503
    assert health.json()["redis"] == "unavailable"


async def test_health_checks_both_dependencies(environment, monkeypatch):
    assert (await environment.client.get("/health")).json() == {
        "status": "ok",
        "mongodb": "ok",
        "redis": "ok",
    }
    monkeypatch.setattr(
        environment.repository.documents.database,
        "command",
        AsyncMock(side_effect=NetworkTimeout()),
    )
    response = await environment.client.get("/health")
    assert response.status_code == 503
    assert response.json()["mongodb"] == "unavailable"


async def test_ambiguous_mongo_write_keeps_reservation(environment, payload, monkeypatch):
    monkeypatch.setattr(environment.repository, "insert", AsyncMock(side_effect=NetworkTimeout()))
    response = await environment.client.post("/documents", json=payload)
    assert response.status_code == 503
    assert len(await environment.store.tokens("alice")) == 1


async def test_active_tokens_are_rebuilt_from_mongodb(environment, payload):
    for number in range(3):
        await submit(environment, {**payload, "content": f"Content {number}"})
    await environment.redis.delete(environment.store.active_key("alice"))
    response = await environment.client.post("/documents", json=payload)
    assert response.status_code == 429
    assert len(await environment.store.tokens("alice")) == 3
