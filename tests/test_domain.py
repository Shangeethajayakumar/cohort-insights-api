import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from backend.config.settings import Settings
from backend.scripts.core.utils.versioning import (
    apply_cached_results,
    content_hash,
    new_stages,
    public_document,
    utc_now,
)


def example_document():
    return {
        "_id": "example",
        "user_id": "alice",
        "title": "Example",
        "content": "Original content",
        "content_hash": content_hash("Original content"),
        "content_version": 1,
        "status": "queued",
        "stages": new_stages(),
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


class SettingsValidationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.configuration = {
            "mongo_url": "mongodb://localhost:27017",
            "mongo_database": "test_settings",
            "redis_url": "redis://localhost:6379/0",
            "api_keys": {"test-key": "test-user"},
        }

    def test_database_settings_and_api_keys_are_required(self):
        with self.assertRaises(ValidationError) as raised:
            Settings(_env_file=None)
        missing = {error["loc"][0] for error in raised.exception.errors()}
        self.assertEqual(missing, set(self.configuration))

    def test_dotenv_loads_settings_and_environment_can_override(self):
        with TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                'MONGO_URL=mongodb://localhost:27017\n'
                'MONGO_DATABASE=test_settings\n'
                'REDIS_URL=redis://localhost:6379/0\n'
                'API_KEYS={"test-key":"test-user"}\n',
                encoding="utf-8",
            )
            settings = Settings(_env_file=env_file)
            for name, value in self.configuration.items():
                self.assertEqual(getattr(settings, name), value)
            with patch.dict(os.environ, {"MONGO_URL": "mongodb://mongodb:27017"}):
                settings = Settings(_env_file=env_file)
                self.assertEqual(settings.mongo_url, "mongodb://mongodb:27017")

    def test_stage_minimum_cannot_exceed_maximum(self):
        with self.assertRaisesRegex(ValidationError, "processing minimum"):
            Settings(
                _env_file=None,
                **self.configuration,
                processing_min_seconds=2,
                processing_max_seconds=1,
            )

    def test_lease_must_exceed_stage_durations(self):
        with self.assertRaisesRegex(ValidationError, "Worker lease"):
            Settings(
                _env_file=None,
                **self.configuration,
                processing_max_seconds=60,
                lease_seconds=60,
            )


class VersionSafetyTests(unittest.TestCase):
    def test_completed_results_are_visible_together(self):
        document = example_document()
        apply_cached_results(document, {"summary": "Summary", "tags": ["tag"]})
        result = public_document(document)
        self.assertTrue(result["results_current"])
        self.assertEqual(result["summary"], "Summary")
        self.assertEqual(result["tags"], ["tag"])

    def test_mixed_versions_hide_both_outputs(self):
        document = example_document()
        apply_cached_results(document, {"summary": "Summary", "tags": ["tag"]})
        document["stages"]["enriching"]["result"]["content_version"] = 2
        result = public_document(document)
        self.assertTrue(result["is_stale"])
        self.assertFalse(result["results_current"])
        self.assertIsNone(result["summary"])
        self.assertIsNone(result["tags"])

    def test_old_content_results_are_not_current(self):
        document = example_document()
        apply_cached_results(document, {"summary": "Old", "tags": ["old"]})
        document.update(content="New content", content_version=2)
        document["content_hash"] = content_hash(document["content"])
        self.assertFalse(public_document(document)["results_current"])

    def test_hash_mismatch_also_hides_results(self):
        document = example_document()
        apply_cached_results(document, {"summary": "Summary", "tags": ["tag"]})
        document["stages"]["processing"]["result"]["content_hash"] = "wrong"
        self.assertIsNone(public_document(document)["summary"])

    def test_cached_results_are_bound_to_destination_version(self):
        document = example_document()
        document["content_version"] = 9
        apply_cached_results(document, {"summary": "Summary", "tags": ["tag"]})
        self.assertEqual(public_document(document)["result_version"], 9)


if __name__ == "__main__":
    unittest.main()
