"""Application settings, read from environment variables or .env."""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mongo_url: str
    mongo_database: str
    redis_url: str
    api_keys: dict[str, str]
    cache_ttl_seconds: int = Field(default=3600, ge=1)
    worker_concurrency: int = Field(default=2, ge=1, le=32)
    processing_min_seconds: float = Field(default=10, ge=0)
    processing_max_seconds: float = Field(default=20, ge=0)
    enriching_min_seconds: float = Field(default=5, ge=0)
    enriching_max_seconds: float = Field(default=15, ge=0)
    failure_probability: float = Field(default=0.1, ge=0, le=1)
    max_stage_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_seconds: float = Field(default=2, ge=0)
    worker_poll_seconds: float = Field(default=0.5, gt=0)
    lease_seconds: int = Field(default=60, ge=1)

    @model_validator(mode="after")
    def validate_stage_timing(self):
        if self.processing_min_seconds > self.processing_max_seconds:
            raise ValueError("processing minimum must not exceed its maximum")
        if self.enriching_min_seconds > self.enriching_max_seconds:
            raise ValueError("enriching minimum must not exceed its maximum")
        if (
            self.processing_max_seconds >= self.lease_seconds
            or self.enriching_max_seconds >= self.lease_seconds
        ):
            raise ValueError("Worker lease must exceed both stage durations")
        if not self.api_keys or any(not key or not owner for key, owner in self.api_keys.items()):
            raise ValueError("API keys and their owners must not be empty")
        return self
