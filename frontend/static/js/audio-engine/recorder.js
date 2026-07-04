// @ts-check
/**
 * VoiceRecorder — captures a short mono PCM sample for voice cloning, reusing the
 * SAME voice-capture worklet as the live engine (per the M4c decision to reuse the
 * capture path rather than MediaRecorder). It accumulates 20ms Float32 frames, then
 * encodes them as a 16-bit WAV blob for upload to the gateway /voices clone route.
 *
 * Emits 'level' (0..1 RMS) while recording so the page can show a meter, and tracks
 * the peak RMS so the caller can warn on a too-quiet take — the lesson from the
 * spike, where a near-silent donor produced silent output.
 */

import { encodeWav, floatTo16 } from "./utils/audio-utils.js";

const BASE = "/static/js/audio-engine";

export class VoiceRecorder {
  /** @param {{ sampleRate?: number }} [config] */
  constructor({ sampleRate = 48000 } = {}) {
    this.sampleRate = sampleRate;
    /** @type {AudioContext | null} */
    this.audioContext = null;
    /** @type {MediaStream | null} */
    this.mediaStream = null;
    this.sourceNode = null;
    this.captureNode = null;
    /** @type {Float32Array[]} */
    this._frames = [];
    this._peak = 0;
    /** @type {Map<string, Set<Function>>} */
    this._handlers = new Map();
  }

  /** @param {string} event @param {Function} handler */
  on(event, handler) {
    if (!this._handlers.has(event)) this._handlers.set(event, new Set());
    this._handlers.get(event).add(handler);
  }

  /** @param {string} event @param {*} payload */
  _emit(event, payload) {
    this._handlers.get(event)?.forEach((h) => h(payload));
  }

  async start() {
    this._frames = [];
    this._peak = 0;
    const chunkSize = Math.round((this.sampleRate * 20) / 1000);

    this.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        sampleRate: this.sampleRate,
        noiseSuppression: true,
        echoCancellation: true,
        autoGainControl: true,
      },
    });
    this.audioContext = new AudioContext({ sampleRate: this.sampleRate });
    if (this.audioContext.state === "suspended") await this.audioContext.resume();
    await this.audioContext.audioWorklet.addModule(`${BASE}/processors/voice-capture.worklet.js`);

    this.sourceNode = this.audioContext.createMediaStreamSource(this.mediaStream);
    this.captureNode = new AudioWorkletNode(this.audioContext, "voice-capture", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      channelCount: 1,
      processorOptions: { chunkSize },
    });
    this.captureNode.port.onmessage = (e) => this._onMessage(e.data);
    // The capture worklet emits no audio, so routing to destination just keeps it
    // pulled by the render graph (no feedback), same as the live engine does.
    this.sourceNode.connect(this.captureNode).connect(this.audioContext.destination);
  }

  _onMessage(data) {
    if (data.type === "audio") {
      this._frames.push(data.data);
    } else if (data.type === "level") {
      if (data.rms > this._peak) this._peak = data.rms;
      this._emit("level", data.rms);
    }
  }

  /**
   * Stop recording and package the take.
   * @returns {Promise<{ blob: Blob, peak: number, durationS: number }>}
   */
  async stop() {
    this.captureNode?.disconnect();
    this.sourceNode?.disconnect();
    this.mediaStream?.getTracks().forEach((t) => t.stop());
    if (this.audioContext && this.audioContext.state !== "closed") {
      await this.audioContext.close();
    }

    let total = 0;
    for (const f of this._frames) total += f.length;
    const pcm = new Float32Array(total);
    let offset = 0;
    for (const f of this._frames) {
      pcm.set(f, offset);
      offset += f.length;
    }
    const wav = encodeWav(floatTo16(pcm), this.sampleRate);
    return {
      blob: new Blob([wav], { type: "audio/wav" }),
      peak: this._peak,
      durationS: total / this.sampleRate,
    };
  }
}
