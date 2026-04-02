import os

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    IN_PRODUCTION: bool = False

    # PostgreSQL connection
    METADATA_DB_NAME: str
    METADATA_DB_USER: str
    METADATA_DB_PASSWORD: str
    METADATA_DB_HOST: str
    METADATA_DB_PORT: str = "5432"

    @computed_field  # type: ignore[misc]
    @property
    def DATABASE_URL(self) -> str:
        """Async SQLAlchemy URL used by db.py."""
        return (
            f"postgresql+asyncpg://{self.METADATA_DB_USER}:"
            f"{self.METADATA_DB_PASSWORD}@"
            f"{self.METADATA_DB_HOST}:{self.METADATA_DB_PORT}/"
            f"{self.METADATA_DB_NAME}"
        )

    # Read from .env file; ENV_FILE env var lets Docker override the path
    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", ".env"),
        extra="ignore",
    )


settings_obj = Settings()
