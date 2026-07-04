"""Spec #1 spike: measure the Cartesia clip voice-changer as a walkie-talkie loop.

Not part of the service. A standalone harness that reuses M4a's
``CartesiaBackend.convert_utterance`` (the real ``/voice-changer/sse`` call) to
answer three questions before M4b/M4c UI gets built on top of it:

  1. Latency — how long from end-of-speech to first converted audio? The felt
     walkie-talkie lag is ``vad_silence_ms`` (the trailing silence we wait out to
     detect the pause) + the network+model conversion time this measures.
  2. Identity — does the output sound like the cloned target voice?
  3. Prosody — does the donor's delivery / timing / filler survive? It's an
     audio->audio transform, so it should; this spike is where we confirm by ear.

Run from ``inference/`` so the ``app`` package imports resolve::

    # clone a target voice from a ~15s sample, print its voice_id
    uv run python scripts/voice_changer_spike.py clone --sample me.wav --name Me

    # convert donor clips into that voice; writes *_converted.wav + a latency table
    uv run python scripts/voice_changer_spike.py convert --voice-id <id> d1.wav d2.wav

    # offline: prove the harness plumbing against a mocked Cartesia (no key/network)
    uv run python scripts/voice_changer_spike.py selftest

``clone`` and ``convert`` need ``CARTESIA_API_KEY`` (env or inference/.env);
``selftest`` needs neither.
"""

import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import time
import wave
from pathlib import Path

import httpx
import numpy as np

from app.backends.cartesia import CartesiaBackend, cartesia_client
from app.config import settings

# Keep converted audio out of the repo tree — this is throwaway spike output.
SCRATCH = Path(
    "/private/tmp/claude-501/-Users-garrettmallory-Code-Mockingbird"
    "/50b29677-7615-4e59-8d22-b6489759c1fe/scratchpad/spike_out"
)


def _require_key() -> None:
    if not settings.cartesia_api_key:
        raise SystemExit("CARTESIA_API_KEY not set (export it or add it to inference/.env)")


def read_wav_mono_int16(path: Path) -> tuple[bytes, int]:
    """Load a 16-bit WAV as mono Int16 PCM, returning (pcm, sample_rate).

    Cartesia's voice changer takes the clip at its own rate, so we pass the file's
    native sample rate straight through — no resampling, which would only muddy
    the by-ear quality judgment this spike exists to make.
    """
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path}: need 16-bit PCM WAV (got {w.getsampwidth() * 8}-bit)")
        n_ch = w.getnchannels()
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    if n_ch > 1:
        samples = np.frombuffer(raw, dtype=np.int16).reshape(-1, n_ch)
        raw = samples.mean(axis=1).astype(np.int16).tobytes()
    return raw, sr


def write_wav(path: Path, pcm: bytes, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


def _dur_s(pcm: bytes, sr: int) -> float:
    return len(pcm) / 2 / sr if sr else 0.0


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = min(len(s) - 1, round((q / 100) * (len(s) - 1)))
    return s[idx]


def build_backend() -> CartesiaBackend:
    """Construct the M4a Cartesia backend straight from settings (VAD params and all)."""
    return CartesiaBackend(
        api_key=settings.cartesia_api_key or "offline",
        base_url=settings.cartesia_base_url,
        version=settings.cartesia_version,
        default_voice_id=settings.cartesia_voice_id,
        frame_ms=settings.frame_ms,
        energy_threshold=settings.vad_energy_threshold,
        silence_ms=settings.vad_silence_ms,
        max_utterance_ms=settings.vad_max_utterance_ms,
        preroll_ms=settings.vad_preroll_ms,
    )


async def clone(clip_bytes: bytes, filename: str, name: str, language: str) -> str:
    """Clone a target voice from a sample clip; return the Cartesia voice_id.

    Mirrors ``app/voices.py`` so the spike mints an id the same way the service does.
    The clip bytes are read by the caller so file I/O stays off the async path.
    """
    _require_key()
    files = {"clip": (filename, clip_bytes, "audio/wav")}
    data = {"name": name, "language": language}
    async with cartesia_client(
        api_key=settings.cartesia_api_key,
        base_url=settings.cartesia_base_url,
        version=settings.cartesia_version,
    ) as client:
        resp = await client.post("/voices/clone", files=files, data=data)
        resp.raise_for_status()
        meta = resp.json()
    voice_id = meta.get("id")
    if not voice_id:
        raise SystemExit(f"clone returned no voice id: {meta!r}")
    return voice_id


async def convert_clips(voice_id: str, clips: list[Path], outdir: Path) -> None:
    """Convert each donor clip into ``voice_id`` and print the latency table.

    Each whole clip is treated as one utterance (the direct conversion cost). Note
    ``convert_utterance`` echoes the input back on an API error rather than raising —
    watch stderr for ``cartesia.convert_failed`` and treat an output that just
    sounds like the donor as a failed conversion, not a bad clone.
    """
    _require_key()
    backend = build_backend()
    rows: list[tuple[str, float, float, float, float, Path]] = []
    try:
        for clip in clips:
            pcm, sr = read_wav_mono_int16(clip)
            t0 = time.perf_counter()
            frames = await backend.convert_utterance(pcm, sr, voice_id)
            convert_ms = (time.perf_counter() - t0) * 1000
            out = b"".join(frames)
            out_path = outdir / f"{clip.stem}_converted.wav"
            write_wav(out_path, out, sr)
            felt_ms = settings.vad_silence_ms + convert_ms
            rows.append(
                (clip.name, _dur_s(pcm, sr), convert_ms, felt_ms, _dur_s(out, sr), out_path)
            )
    finally:
        await backend.aclose()
    _report(rows)


def _report(rows: list[tuple[str, float, float, float, float, Path]]) -> None:
    print()
    print(f"{'clip':<28}{'in(s)':>8}{'convert(ms)':>13}{'felt(ms)':>11}{'out(s)':>8}")
    print("-" * 68)
    for name, in_s, conv, felt, out_s, _ in rows:
        print(f"{name:<28}{in_s:>8.2f}{conv:>13.0f}{felt:>11.0f}{out_s:>8.2f}")
    if not rows:
        return
    convs = [r[2] for r in rows]
    felts = [r[3] for r in rows]
    print("-" * 68)
    print(f"convert ms   P50={_pct(convs, 50):.0f}   P95={_pct(convs, 95):.0f}   (n={len(convs)})")
    print(
        f"felt ms      P50={_pct(felts, 50):.0f}   P95={_pct(felts, 95):.0f}"
        f"   [= vad_silence_ms {settings.vad_silence_ms} + convert]"
    )
    print(f"\noutputs: {rows[0][5].parent}")
    print("LISTEN: sounds like the target voice? donor's delivery + filler preserved?")


async def selftest() -> None:
    """Prove the read->convert->write plumbing against a mocked Cartesia (no key/network)."""

    def handler(request: httpx.Request) -> httpx.Response:
        # One SSE 'data:' event carrying 0.1s of base64 PCM, terminated with done.
        chunk = base64.b64encode(np.zeros(4800, dtype=np.int16).tobytes()).decode()
        return httpx.Response(200, text=f"data: {json.dumps({'data': chunk, 'done': True})}\n\n")

    backend = build_backend()
    backend._client = cartesia_client(  # noqa: SLF001 - spike injects a mock transport
        api_key="test",
        base_url=settings.cartesia_base_url,
        version=settings.cartesia_version,
        transport=httpx.MockTransport(handler),
    )
    try:
        sr = 48000
        t = np.arange(int(0.5 * sr))
        sine = (0.2 * 32767 * np.sin(2 * np.pi * 220 * t / sr)).astype(np.int16).tobytes()
        frames = await backend.convert_utterance(sine, sr, "voice_test")
    finally:
        await backend.aclose()

    out = b"".join(frames)
    assert out, "no output frames from mocked convert"
    assert all(len(f) == 1920 for f in frames), "frames are not 20ms/1920B @48k"
    print(f"SELFTEST PASS — {len(frames)} frames, {len(out)} bytes out (mocked Cartesia)")


def _repo_root() -> Path:
    """Repo root = two levels up from inference/scripts/."""
    return Path(__file__).resolve().parents[2]


def _ensure_wav(src: Path, dst: Path) -> None:
    """Stage ``src`` as a 16-bit WAV at ``dst`` — copy if already WAV, else macOS afconvert."""
    if src.suffix.lower() == ".wav":
        shutil.copyfile(src, dst)
        return
    if not shutil.which("afconvert"):
        raise SystemExit("need macOS `afconvert` to transcode non-WAV input")
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16", str(src), str(dst)], check=True)


def _normalize_wav(path: Path, target_peak: float = 0.8) -> int:
    """Peak-normalize a WAV in place so quiet clips are audible and convert cleanly.

    Phone/voice-memo clips arrive at wildly different levels; a near-silent donor
    (a peak-405/32768 clip bit us here) reads as silence and gives Cartesia nothing
    to convert. Returns the ORIGINAL peak (0..32767) for logging.
    """
    with wave.open(str(path), "rb") as w:
        params = w.getparams()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    peak = float(np.abs(a).max()) if a.size else 0.0
    if peak > 0:
        a = np.clip(a * (target_peak * 32767.0 / peak), -32768.0, 32767.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(a.astype(np.int16).tobytes())
    return int(peak)


async def _pipeline_net(
    clip_bytes: bytes,
    target_name: str,
    donor_wav: Path,
    name: str,
    language: str,
    listen_dir: Path,
) -> None:
    voice_id = await clone(clip_bytes, target_name, name, language)
    print(f"voice_id={voice_id}")
    await convert_clips(voice_id, [donor_wav], listen_dir)


def run_pipeline(target: Path, donor: Path, name: str, language: str, listen_dir: Path) -> None:
    """One call: transcode both inputs, clone the target, convert the donor, stage for listening.

    Outputs land in ``listen_dir`` as 00_target.wav / 01_donor.wav / 01_donor_converted.wav.
    """
    _require_key()
    listen_dir.mkdir(parents=True, exist_ok=True)
    target_wav = listen_dir / "00_target.wav"
    donor_wav = listen_dir / "01_donor.wav"
    _ensure_wav(target, target_wav)
    _ensure_wav(donor, donor_wav)
    target_peak = _normalize_wav(target_wav)
    donor_peak = _normalize_wav(donor_wav)
    print(f"source peaks /32767: target={target_peak} donor={donor_peak} (normalized to 0.8)")
    if min(target_peak, donor_peak) < 200:
        print("WARNING: a source was near-silent before normalize — check that recording")
    clip_bytes = target_wav.read_bytes()
    asyncio.run(_pipeline_net(clip_bytes, target_wav.name, donor_wav, name, language, listen_dir))
    print(f"\nlisten dir: {listen_dir}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Cartesia voice-changer walkie-talkie spike")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("clone", help="clone a target voice from a sample clip")
    pc.add_argument("--sample", required=True, type=Path)
    pc.add_argument("--name", required=True)
    pc.add_argument("--language", default="en")

    pv = sub.add_parser("convert", help="convert donor clips into a target voice")
    pv.add_argument("--voice-id", required=True)
    pv.add_argument("clips", nargs="+", type=Path)
    pv.add_argument("--outdir", type=Path, default=SCRATCH)

    sub.add_parser("selftest", help="offline plumbing check (no key/network)")

    ppl = sub.add_parser("pipeline", help="transcode + clone + convert + stage, one call")
    ppl.add_argument("--target", required=True, type=Path)
    ppl.add_argument("--donor", required=True, type=Path)
    ppl.add_argument("--name", default="SpikeTarget")
    ppl.add_argument("--language", default="en")
    ppl.add_argument("--listen-dir", type=Path, default=_repo_root() / "spike_listen")

    args = p.parse_args(argv)
    if args.cmd == "clone":
        clip_bytes = args.sample.read_bytes()
        print(asyncio.run(clone(clip_bytes, args.sample.name, args.name, args.language)))
    elif args.cmd == "convert":
        args.outdir.mkdir(parents=True, exist_ok=True)
        asyncio.run(convert_clips(args.voice_id, args.clips, args.outdir))
    elif args.cmd == "selftest":
        asyncio.run(selftest())
    elif args.cmd == "pipeline":
        run_pipeline(args.target, args.donor, args.name, args.language, args.listen_dir)


if __name__ == "__main__":
    main()
