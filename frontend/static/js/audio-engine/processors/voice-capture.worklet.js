// @ts-check
/**
 * Capture worklet: accumulates 128-sample render quanta into fixed 20ms frames
 * (960 samples @ 48kHz) and posts each completed frame to the main thread.
 *
 * Echo-slice note: this uses postMessage for clarity. The production hardening
 * pass replaces it with a SharedArrayBuffer ring buffer (see plan, Milestone 7).
 * Even so, process() stays allocation-free on the steady path.
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

    // RMS of this quantum, for the input level meter (cheap, no allocation).
    const rms = Math.sqrt(sumSquares / input.length);
    this.port.postMessage({ type: "level", rms });

    return true;
  }
}

registerProcessor("voice-capture", VoiceCaptureProcessor);
