"""Gateway settings, loaded from the environment / .env.

Kept intentionally small for the echo slice: no DB, Redis, or auth yet. Those
arrive in later milestones (see the project plan).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 3001

    # HTTP CORS origins (feeds CORSMiddleware only). "*" is fine for local dev.
    # Note: this does not enforce WebSocket Origin — that check arrives with
    # gateway auth in a later milestone.
    allowed_origins: list[str] = ["*"]


settings = Settings()
