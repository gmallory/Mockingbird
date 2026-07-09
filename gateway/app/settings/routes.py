"""User settings API (M10): read/merge/write the ``User.settings`` JSON MutableDict.

``GET /api/settings`` returns the caller's settings merged over documented
defaults (so the frontend never has to guess about missing keys on a
brand-new account); ``PATCH /api/settings`` merge-patches only the fields the
caller actually sent (``exclude_unset`` — a field omitted from the request
body leaves the stored value untouched; sending it explicitly, including
``null``, overwrites it). Both are scoped to the caller via
``get_current_user`` — there is no path parameter here, so there is no way to
address another user's row.

Backs the Settings page (PRODUCT_SPEC §4.5): audio input/output device
preference, a latency-vs-quality preset, and the three ``getUserMedia``
constraint toggles the audio engine already exposes as config
(``enableNoiseSuppression``/``enableEchoCancellation``/``enableAutoGainControl``
in ``frontend/static/js/audio-engine/audio-engine.js``). Account fields
(email/plan) are deliberately NOT stored here — they come from Supabase via
the existing ``GET /auth/me``, so this module only ever owns the audio/quality
blob, never identity.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models import User
from app.db.session import get_session

router = APIRouter()

QualityPreset = Literal["latency", "balanced", "quality"]

# The only keys a GET/PATCH ever sees, in or out. Anything else already in
# ``user.settings`` (e.g. a future key, or a stray one from a bad client) is
# left on the row untouched but never surfaced or overwritable here — keeps
# this API's contract narrow even though the underlying column is a bare JSON
# blob that could hold anything.
_DEFAULTS: dict[str, object] = {
    "audio_input_device_id": None,
    "audio_output_device_id": None,
    "quality_preset": "balanced",
    "noise_suppression": True,
    "echo_cancellation": True,
    "auto_gain_control": True,
}


class SettingsPatch(BaseModel):
    audio_input_device_id: str | None = None
    audio_output_device_id: str | None = None
    quality_preset: QualityPreset | None = None
    noise_suppression: bool | None = None
    echo_cancellation: bool | None = None
    auto_gain_control: bool | None = None


def _merged(user_settings: dict) -> dict:
    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in user_settings.items() if k in _DEFAULTS})
    return merged


@router.get("/api/settings")
async def get_settings(user: User = Depends(get_current_user)) -> dict:
    """The caller's settings, merged over defaults."""
    return _merged(user.settings)


@router.patch("/api/settings")
async def update_settings(
    payload: SettingsPatch,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Merge-patch only the fields present in the request body."""
    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=422, detail="request body must set at least one field")
    for key, value in patch.items():
        # In-place edits on a MutableDict-backed column are tracked and
        # flushed (see app/db/models.py::User.settings); a wholesale
        # reassignment of user.settings would also work but would clobber any
        # unrelated keys the merge above is careful to leave alone.
        user.settings[key] = value
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return _merged(user.settings)
