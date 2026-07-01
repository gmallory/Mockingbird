# M4b code review — skipped fixes

From `/code-review max --fix` on `feat/m4b-voice-cloning-registry` (branched off m4a),
2026-06-30. These findings were verified but not auto-applied at the time. Fixed
findings landed in the working tree / a commit; see git log for that commit's message.

**Update 2026-06-30:** all five items below were revisited and landed on
`fix/m4b-review-followups`. Left the original writeups in place as the record of
why each was deferred; added a one-line status to each.

## 1. Duplicate Cartesia httpx-client construction — fixed

`inference/app/backends/cartesia.py` now exports a `cartesia_client()` factory;
both `CartesiaBackend.__init__` and `inference/app/voices.py::clone_voice` call it.

`inference/app/voices.py:57` builds the same Cartesia httpx client (Bearer auth,
`Cartesia-Version` header, `cartesia_base_url`, `httpx.Timeout(30.0, connect=10.0)`)
as `inference/app/backends/cartesia.py:156`. Genuinely dedupable into a shared
`_cartesia_client(settings)` factory — both call sites are the same service.

Skipped because `cartesia.py` is M4a, outside this diff's scope. Worth a follow-up
pass when next touching either file.

## 2. `_transport` module global vs gateway's `transport` param — fixed

Replaced the module global with a FastAPI dependency (`voices._get_transport`,
`Depends`-injected into the route), overridden in tests via
`app.dependency_overrides` — the same pattern `get_session` already uses in the
gateway tests, rather than gateway's plain-function `transport` param (the route
needs the value per-request, not just per-call).

`inference/app/voices.py:25` uses a mutable module-level global (`_transport`) as
its test seam. `gateway/app/inference/http.py` does the identical job (mockable
httpx transport for a clone-voice POST) via a `transport` function parameter —
cleaner DI. Same PR, two different altitudes for the same job.

No runtime bug: `_transport` is only ever mutated by tests via `monkeypatch`,
never written at request time, so there's no production race.

Skipped because aligning it is a design call (route handler vs. plain function,
how to inject the transport into a FastAPI route) that would churn the test file.
Left for deliberate follow-up rather than folded into a bug-fix pass.

## 3. `voice_id` has no unique constraint — fixed

`gateway/app/db/models.py:74` — `Voice.voice_id` (the Cartesia natural key) has no
`unique=True`, unlike `User.email`. The migration
(`gateway/alembic/versions/e9658781796e_voice_table.py`) only constrains the
surrogate `id` PK. A retried/double-submitted clone could in principle persist two
registry rows pointing at the same Cartesia voice.

Skipped: fixing this needs a migration edit and flips behavior to a 500/integrity
error on duplicate-insert, which is a behavior change outside a pure bug-fix pass.
Real-world risk is low — Cartesia mints a fresh id per clone call, so the normal
flow doesn't produce duplicate ids. Revisit if dedup becomes a real issue.

Added `Field(unique=True, index=True)` on `Voice.voice_id`, a new migration
(`b3f1a6c9d2e4_voice_voice_id_unique.py`) creating the unique index, and an
`IntegrityError` catch in `gateway/app/voices/routes.py::create_voice` that
rolls back and returns 409 instead of a raw 500 on a duplicate insert.

## 4. Unbounded `await clip.read()` — fixed

Both `gateway/app/voices/routes.py` and `inference/app/voices.py` read the entire
uploaded clip into memory with no size cap, on an unauthenticated `POST /voices`.

Skipped per `CLAUDE.md`: project goal is "clean architecture and forward momentum,
not production hardening." A size cap is reasonable to add when M5 auth lands and
the endpoint is no longer wide open.

Added anyway: a `max_clip_bytes` setting (10MB default) on both services, enforced
by reading the upload in 64KB chunks and aborting with 413 past the cap — bounds
memory regardless of what the client's Content-Length claims. Small enough to not
be "production hardening," and it closes an unbounded-memory-read footgun that
doesn't need auth to matter.

## 5. Cross-service multipart contract duplication — mitigated

`inference/app/voices.py` and `gateway/app/inference/http.py` build the identical
`files={"clip": (name, bytes, content_type)}` + `data={"name", "language"}`
multipart shape and the same try/`raise_for_status`/except pattern. Looks like #1
but **can't** be deduped the same way — they're two separate uv-managed services
(separate `pyproject.toml` + `uv.lock`), so no shared Python module is possible
across the gateway↔inference boundary.

Not really "skipped" so much as not fixable as code reuse. The right mitigation,
if this needs hardening later, is a contract test per `agents/AGENTS.md`, not a
shared helper.

Added `docs/contracts/voice_clone_multipart.json` as the field-name source of
truth, with `gateway/tests/test_contracts.py` and `inference/tests/test_contracts.py`
each asserting their request/response field names against it — so a one-sided
rename now fails a test instead of silently breaking the other service.
