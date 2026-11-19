"""Plain document rules, shared by the API and worker."""

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

ACTIVE_STATUSES = ("queued", "processing", "enriching")
PIPELINE_VERSION = "v1"


def utc_now() -> datetime:
    return datetime.now(UTC)


def content_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def new_stages() -> dict[str, dict[str, Any]]:
    return {
        name: {"status": "pending", "attempts": 0, "error": None, "result": None}
        for name in ("processing", "enriching")
    }


def versioned_result(document: dict, value: Any) -> dict:
    return {
        "content_version": document["content_version"],
        "content_hash": document["content_hash"],
        "value": value,
    }


def result_is_current(document: dict, result: dict | None) -> bool:
    return bool(
        result
        and result["content_version"] == document["content_version"]
        and result["content_hash"] == document["content_hash"]
    )


def public_document(document: dict) -> dict:
    summary = document["stages"]["processing"]["result"]
    tags = document["stages"]["enriching"]["result"]
    current = (
        document["status"] == "completed"
        and all(stage["status"] == "completed" for stage in document["stages"].values())
        and result_is_current(document, summary)
        and result_is_current(document, tags)
    )
    return {
        "document_id": str(document["_id"]),
        "user_id": document["user_id"],
        "title": document["title"],
        "content": document["content"],
        "client_doc_ref": document.get("client_doc_ref"),
        "content_version": document["content_version"],
        "content_hash": document["content_hash"],
        "status": document["status"],
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
        "stages": {
            name: {
                "status": stage["status"],
                "attempts": stage["attempts"],
                "error": stage["error"],
                "result_version": (stage["result"]["content_version"] if stage["result"] else None),
            }
            for name, stage in document["stages"].items()
        },
        "results_current": current,
        "is_stale": any(
            result is not None and not result_is_current(document, result)
            for result in (summary, tags)
        ),
        "result_version": document["content_version"] if current else None,
        "summary": summary["value"] if current else None,
        "tags": tags["value"] if current else None,
    }


def apply_cached_results(document: dict, cached: dict) -> None:
    document["status"] = "completed"
    for stage_name, field in (("processing", "summary"), ("enriching", "tags")):
        document["stages"][stage_name].update(
            status="completed", result=versioned_result(document, cached[field])
        )
