// @ts-check
/**
 * Float32 <-> Int16 PCM conversion helpers, shared by the WebSocket worker.
 * The wire format is Int16 (halves bandwidth vs Float32); the Web Audio graph
 * works in Float32 [-1, 1].
 */

/**
 * @param {Float32Array} float32
 * @returns {Int16Array}
 */
export function floatTo16(float32) {
  const out = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

/**
 * @param {Int16Array} int16
 * @returns {Float32Array}
 */
export function int16ToFloat(int16) {
  const out = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    out[i] = int16[i] / 0x8000;
  }
  return out;
}
