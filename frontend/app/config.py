"""Frontend settings.

``public_ws_url`` is rendered into the page so the browser knows where to open
its audio WebSocket. The ``PUBLIC_`` prefix marks values safe to expose to the
browser (see the root .env.example).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 3000

    # Where the browser audio engine connects for the echo loop.
    public_ws_url: str = "ws://localhost:3001/ws/voice"

    # Gateway HTTP base the browser calls for the voice registry (GET/POST /voices).
    # PUBLIC_ prefix = safe to expose to the browser (gateway CORS allows it).
    public_gateway_url: str = "http://localhost:3001"


settings = Settings()
