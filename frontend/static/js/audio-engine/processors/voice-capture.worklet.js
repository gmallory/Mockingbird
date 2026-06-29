// @ts-check
/**
 * Capture worklet: accumulates 128-sample render quanta into fixed 20ms frames
 * (960 samples @ 48kHz) and posts each completed frame to the main thread.
 *
 * Echo-slice note: this uses postMessage for clarity. Each completed frame is
 * transferred via `buffer.slice()`, so it allocates one Float32Array per 20ms.
 * The accumulation path itself reuses a preallocated buffer. The production
 * hardening pass replaces this with a SharedArrayBuffer ring buffer to remove
 * the per-frame allocation entirely (see plan, Milestone 7).
 *
 * The input level meter is accumulated across render quanta and posted only
 * ~every 50ms, so process() does not allocate a message object on every quantum
 * (~2.7ms) — the realtime thread stays effectively allocation-free.
 */

class VoiceCaptureProcessor extends AudioWorkletProcessor {
  /** @param {AudioWorkletNodeOptions} options */
  constructor(options) {
    super();
    const opts = options.processorOptions || {};
    this.chunkSize = opts.chunkSize || 960;
    this.buffer = new Float32Array(this.chunkSize);
    this.bufferIndex = 0;
    this.isActive = true;

    // Level meter is emitted at ~50ms cadence rather than every render quantum,
    // so the hot path doesn't allocate a message per quantum. `sampleRate` is a
    // global in the AudioWorklet scope.
    this.levelSumSquares = 0;
    this.levelCount = 0;
    this.levelInterval = Math.max(1, Math.round(sampleRate * 0.05));

    this.port.onmessage = (e) => {
      if (e.data.type === "stop") this.isActive = false;
      if (e.data.type === "start") this.isActive = true;
    };
  }

  /**
   * @param {Float32Array[][]} inputs
   * @returns {boolean}
   */
  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input || !this.isActive) return true;

    let sumSquares = 0;
    for (let i = 0; i < input.length; i++) {
      const sample = input[i];
      sumSquares += sample * sample;
      this.buffer[this.bufferIndex++] = sample;
      if (this.bufferIndex >= this.chunkSize) {
        // Transfer a copy so the worklet keeps its own scratch buffer.
        const frame = this.buffer.slice();
        this.port.postMessage({ type: "audio", data: frame }, [frame.buffer]);
        this.bufferIndex = 0;
      }
    }

    // Accumulate energy for the input level meter and flush at ~50ms cadence,
    // so the per-quantum path stays allocation-free.
    this.levelSumSquares += sumSquares;
    this.levelCount += input.length;
    if (this.levelCount >= this.levelInterval) {
      const rms = Math.sqrt(this.levelSumSquares / this.levelCount);
      this.port.postMessage({ type: "level", rms });
      this.levelSumSquares = 0;
      this.levelCount = 0;
    }

    return true;
  }
}

registerProcessor("voice-capture", VoiceCaptureProcessor);
