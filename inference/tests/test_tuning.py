"""``POST /voices/{model_id}/tune`` route tests (M10).

Drives the route with ``httpx.ASGITransport`` against a bare FastAPI app that
mounts only ``app.tuning``'s router — the same minimal-app pattern
``test_voices.py``/``test_hd_train.py`` use for their route sections. Each
test builds its own app instance (rather than sharing one module-level app)
because these tests need to set ``app.state.backend`` directly, and a shared
mutable ``app.state`` across tests would leak between them.
"""

import httpx
import pytest
from fastapi import FastAPI

from app import tuning as tuning_mod
from app.backends.self_hosted import SelfHostedBackend
from app.tuning import TuneParams


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(tuning_mod.router)
    return app


def _self_hosted_backend(tmp_path) -> SelfHostedBackend:
    return SelfHostedBackend(model_dir=str(tmp_path), default_model="", device="cpu")


async def _post_tune(app: FastAPI, model_id: str, body: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/voices/{model_id}/tune", json=body)


async def test_tune_route_rejects_unsupported_backend(monkeypatch):
    monkeypatch.setattr(tuning_mod.settings, "inference_backend", "cartesia")
    resp = await _post_tune(_make_app(), "some-model", {})
    assert resp.status_code == 400
    assert "self_hosted|cloud_gpu" in resp.json()["detail"]


async def test_tune_route_503_when_backend_never_initialized(monkeypatch):
    monkeypatch.setattr(tuning_mod.settings, "inference_backend", "self_hosted")
    app = _make_app()  # app.state.backend was never set (no lifespan ran)
    resp = await _post_tune(app, "some-model", {})
    assert resp.status_code == 503


async def test_tune_route_503_when_backend_lacks_tune_support(monkeypatch):
    """E.g. a passthrough/Cartesia backend object, which has no set_tune_params."""
    monkeypatch.setattr(tuning_mod.settings, "inference_backend", "self_hosted")
    app = _make_app()
    app.state.backend = object()
    resp = await _post_tune(app, "some-model", {})
    assert resp.status_code == 503


async def test_tune_route_stores_valid_params(monkeypatch, tmp_path):
    monkeypatch.setattr(tuning_mod.settings, "inference_backend", "self_hosted")
    app = _make_app()
    backend = _self_hosted_backend(tmp_path)
    app.state.backend = backend

    resp = await _post_tune(
        app, "my-model", {"pitch_offset": 5.0, "speed_factor": 1.2, "breathiness": 0.3}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "model_id": "my-model",
        "pitch_offset": 5.0,
        "speed_factor": 1.2,
        "breathiness": 0.3,
    }
    stored = backend.get_tune_params("my-model")
    assert stored == TuneParams(pitch_offset=5.0, speed_factor=1.2, breathiness=0.3)


async def test_tune_route_cloud_gpu_backend_also_allowed(monkeypatch, tmp_path):
    monkeypatch.setattr(tuning_mod.settings, "inference_backend", "cloud_gpu")
    app = _make_app()
    backend = _self_hosted_backend(tmp_path)
    app.state.backend = backend

    resp = await _post_tune(app, "my-model", {"pitch_offset": -3.0})

    assert resp.status_code == 200
    assert backend.get_tune_params("my-model").pitch_offset == -3.0


async def test_tune_route_defaults_when_fields_omitted(monkeypatch, tmp_path):
    monkeypatch.setattr(tuning_mod.settings, "inference_backend", "self_hosted")
    app = _make_app()
    backend = _self_hosted_backend(tmp_path)
    app.state.backend = backend

    resp = await _post_tune(app, "my-model", {})

    assert resp.status_code == 200
    assert resp.json() == {
        "model_id": "my-model",
        "pitch_offset": 0.0,
        "speed_factor": 1.0,
        "breathiness": 0.0,
    }
    assert backend.get_tune_params("my-model") == TuneParams()


async def test_tune_route_overwrites_previous_params_for_the_same_model(monkeypatch, tmp_path):
    monkeypatch.setattr(tuning_mod.settings, "inference_backend", "self_hosted")
    app = _make_app()
    backend = _self_hosted_backend(tmp_path)
    app.state.backend = backend

    await _post_tune(app, "my-model", {"pitch_offset": 5.0})
    resp = await _post_tune(app, "my-model", {"speed_factor": 1.5})

    assert resp.status_code == 200
    # The second call's own omitted fields reset to the request's defaults
    # (each POST fully replaces the stored TuneParams — this is not a merge).
    assert backend.get_tune_params("my-model") == TuneParams(speed_factor=1.5)


@pytest.mark.parametrize(
    "body",
    [
        {"pitch_offset": 12.1},
        {"pitch_offset": -12.1},
        {"speed_factor": 2.1},
        {"speed_factor": 0.49},
        {"breathiness": -0.1},
        {"breathiness": 1.1},
    ],
)
async def test_tune_route_rejects_out_of_range_params(monkeypatch, tmp_path, body):
    monkeypatch.setattr(tuning_mod.settings, "inference_backend", "self_hosted")
    app = _make_app()
    app.state.backend = _self_hosted_backend(tmp_path)

    resp = await _post_tune(app, "my-model", body)

    assert resp.status_code == 422
