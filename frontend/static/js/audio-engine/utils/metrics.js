// @ts-check
/**
 * Roundtrip latency tracking for the echo loop.
 *
 * The echo is strict FIFO and 1:1 (one frame sent -> the same frame echoed back
 * in order), so we don't need to tag the PCM payload. Instead we keep a queue of
 * send timestamps: each outbound frame pushes `performance.now()`, and each
 * inbound echoed frame pops the oldest and computes the roundtrip. This measures
 * the transport loop (capture-exit -> network -> server -> network -> receive),
 * excluding the playback jitter buffer.
 */

export class LatencyTracker {
  /** @param {number} windowSize number of recent samples kept for percentiles */
  constructor(windowSize = 100) {
    this.windowSize = windowSize;
    /** @type {number[]} */
    this.sendTimes = [];
    /** @type {number[]} */
    this.samples = [];
    // Guard against FIFO desync (e.g. a dropped frame around reconnect).
    this.maxPending = 50;
  }

  markSent() {
    this.sendTimes.push(performance.now());
    // A growing backlog means echoes aren't coming back (e.g. a stall around
    // reconnect). Dropping only the oldest would permanently mispair every
    // later echo with a too-recent timestamp, silently under-reporting RTT for
    // the rest of the session. Clear the whole pending queue instead and let it
    // resync once the backlog drains.
    if (this.sendTimes.length > this.maxPending) this.sendTimes.length = 0;
  }

  /** @returns {number | null} roundtrip ms for this frame, or null if unmatched */
  markReceived() {
    const sent = this.sendTimes.shift();
    if (sent === undefined) return null;
    const rtt = performance.now() - sent;
    this.samples.push(rtt);
    if (this.samples.length > this.windowSize) this.samples.shift();
    return rtt;
  }

  reset() {
    this.sendTimes.length = 0;
    this.samples.length = 0;
  }

  /** @returns {{ p50: number, p95: number }} */
  stats() {
    if (this.samples.length === 0) return { p50: 0, p95: 0 };
    const sorted = [...this.samples].sort((a, b) => a - b);
    return {
      p50: percentile(sorted, 0.5),
      p95: percentile(sorted, 0.95),
    };
  }
}

/**
 * @param {number[]} sorted ascending
 * @param {number} q in [0, 1]
 * @returns {number}
 */
function percentile(sorted, q) {
  const idx = Math.min(sorted.length - 1, Math.floor(q * sorted.length));
  return sorted[idx];
}

/**
 * Exponential smoother for level meters.
 */
export class LevelSmoother {
  /** @param {number} attack smoothing factor in [0, 1] */
  constructor(attack = 0.3) {
    this.attack = attack;
    this.value = 0;
  }

  /** @param {number} rms @returns {number} */
  push(rms) {
    this.value = this.value + this.attack * (rms - this.value);
    return this.value;
  }
}
