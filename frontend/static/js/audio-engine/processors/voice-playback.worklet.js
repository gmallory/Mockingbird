// @ts-check
/**
 * Playback worklet: a single-producer/single-consumer ring buffer fed by audio
 * frames posted from the main thread (transformed/echoed audio off the socket).
 * Outputs silence on underrun. Pre-allocated; process() never allocates.
 */

class VoicePlaybackProcessor extends AudioWorkletProcessor {
  /** @param {AudioWorkletNodeOptions} options */
  constructor(options) {
    super();
    const opts = options.processorOptions || {};
    // ~200ms jitter buffer capacity at 48kHz; absorbs network burstiness.
    this.capacity = opts.capacity || 9600;
    this.ringBuffer = new Float32Array(this.capacity);
    this.readIndex = 0;
    this.writeIndex = 0;
    this.isActive = true;

    this.port.onmessage = (e) => {
      if (e.data.type === "audio") this.writeToBuffer(e.data.data);
      if (e.data.type === "stop") this.isActive = false;
      if (e.data.type === "start") this.isActive = true;
    };
  }

  /** @param {Float32Array} data */
  writeToBuffer(data) {
    for (let i = 0; i < data.length; i++) {
      this.ringBuffer[this.writeIndex] = data[i];
      this.writeIndex = (this.writeIndex + 1) % this.capacity;
      // On overrun, advance read pointer (drop oldest) to stay live.
      if (this.writeIndex === this.readIndex) {
        this.readIndex = (this.readIndex + 1) % this.capacity;
      }
    }
  }

  /**
   * @param {Float32Array[][]} inputs
   * @param {Float32Array[][]} outputs
   * @returns {boolean}
   */
  process(inputs, outputs) {
    const output = outputs[0] && outputs[0][0];
    if (!output) return true;

    let sumSquares = 0;
    for (let i = 0; i < output.length; i++) {
      if (this.isActive && this.readIndex !== this.writeIndex) {
        const sample = this.ringBuffer[this.readIndex];
        output[i] = sample;
        sumSquares += sample * sample;
        this.readIndex = (this.readIndex + 1) % this.capacity;
      } else {
        output[i] = 0; // underrun -> silence
      }
    }

    const rms = Math.sqrt(sumSquares / output.length);
    this.port.postMessage({ type: "level", rms });

    return true;
  }
}

registerProcessor("voice-playback", VoicePlaybackProcessor);
