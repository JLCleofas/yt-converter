"""
config.py

Application configuration via pydantic-settings.
All values can be overridden with environment variables or a .env file.
No secrets are stored here — only defaults and validation rules.
"""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Conversion settings
    # ------------------------------------------------------------------

    max_concurrent_jobs: int = 3
    file_ttl_seconds: int = 600

    # ------------------------------------------------------------------
    # File system
    # ------------------------------------------------------------------

    tmp_dir: Path = Path("/tmp/yt-mp3")

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------

    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("max_concurrent_jobs")
    @classmethod
    def validate_max_concurrent_jobs(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_concurrent_jobs must be at least 1.")
        if v > 10:
            raise ValueError("max_concurrent_jobs cannot exceed 10.")
        return v

    @field_validator("file_ttl_seconds")
    @classmethod
    def validate_file_ttl(cls, v: int) -> int:
        if v < 60:
            raise ValueError("file_ttl_seconds must be at least 60.")
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        normalised = v.upper()
        if normalised not in allowed:
            raise ValueError(f"log_level must be one of: {', '.join(sorted(allowed))}.")
        return normalised

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("port must be between 1 and 65535.")
        return v