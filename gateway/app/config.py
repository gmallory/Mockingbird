"""Gateway settings, loaded from the environment / .env.

Kept intentionally small for the echo slice: no DB, Redis, or auth yet. Those
arrive in later milestones (see the project plan).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 3001

    # CORS / WS origins allowed to connect. "*" is fine for local dev.
    allowed_origins: list[str] = ["*"]


settings = Settings()
