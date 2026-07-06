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

    # Inference HTTP base URL (matches INFERENCE_SERVICE_URL in .env.example). Used
    # off the hot path for voice cloning: POST /voices proxies the clip here.
    inference_service_url: str = "http://localhost:8001"

    # Per-frame deadline for the inference round-trip. A stalled or wedged stream
    # would otherwise hang the WS loop forever; on timeout the frame is treated as
    # InferenceUnavailable so the session degrades to passthrough promptly.
    inference_timeout_ms: int = 2000

    # Cap on an uploaded clone clip so POST /voices can't be used to exhaust
    # memory with an oversized body.
    max_clip_bytes: int = 10 * 1024 * 1024

    # === Auth (M6a) — Supabase-hosted ===
    # The gateway never mints tokens; Supabase (GoTrue) does. These drive two
    # things: proxying signup/login to GoTrue (url + anon key, one-shot at login)
    # and *verifying* the access token it returns (jwt secret) on every
    # authenticated request. Verification is the only piece on the request path.
    supabase_url: str = ""  # e.g. https://<ref>.supabase.co (env SUPABASE_URL)
    supabase_anon_key: str = ""  # public anon key, sent as the GoTrue apikey header
    # Legacy/symmetric project JWT secret (Supabase dashboard → Settings → API →
    # JWT Secret). Access tokens are HS256-signed with it, so we can verify them
    # offline with no JWKS round-trip. Moving to Supabase's asymmetric (ES256 +
    # JWKS) keys later is a swap behind verify_token — callers only see TokenClaims.
    supabase_jwt_secret: str = ""  # env SUPABASE_JWT_SECRET
    # Supabase stamps aud="authenticated" on logged-in user tokens; verification
    # requires it so a token minted for a different audience is rejected.
    supabase_jwt_audience: str = "authenticated"


settings = Settings()
