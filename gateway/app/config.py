"""Gateway settings, loaded from the environment / .env.

Slim M2 adds Postgres + Redis connection settings. Auth, rate limiting, and the
gRPC link to inference still arrive in later milestones (see the project plan).
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

    # Async Postgres DSN (asyncpg driver) matching infrastructure/docker-compose.yml.
    database_url: str = "postgresql+asyncpg://mockingbird:dev_password@localhost:5432/mockingbird"

    # Redis is stood up and pinged for health now; rate limiting/sessions land in M4.
    redis_url: str = "redis://localhost:6379"

    # Inference gRPC target (matches INFERENCE_GRPC_URL in .env.example). The gateway
    # opens one Convert stream per WS session; if it's unreachable the session
    # degrades to passthrough instead of dropping.
    inference_grpc_url: str = "localhost:50051"


settings = Settings()
