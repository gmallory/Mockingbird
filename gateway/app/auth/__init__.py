"""Auth (M6a): Supabase-hosted identity.

Supabase (GoTrue) owns credentials and mints access tokens; the gateway proxies
signup/login to it (``supabase``) and *verifies* the returned token offline
(``jwt``). ``dependencies`` exposes the FastAPI ``get_current_user`` used to gate
and scope routes; ``routes`` is the ``/auth`` router.
"""

from app.auth.routes import router

__all__ = ["router"]
