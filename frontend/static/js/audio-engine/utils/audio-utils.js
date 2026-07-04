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

/**
 * Encode mono Int16 PCM as a 16-bit WAV (44-byte header + data). Used by the
 * Voice Studio to package a recorded clone sample for upload.
 * @param {Int16Array} int16
 * @param {number} sampleRate
 * @returns {ArrayBuffer}
 */
export function encodeWav(int16, sampleRate) {
  const dataBytes = int16.length * 2;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);
  const writeStr = (offset, s) => {
    for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true); // PCM header size
  view.setUint16(20, 1, true); // format = PCM
  view.setUint16(22, 1, true); // channels = mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate = sampleRate * blockAlign
  view.setUint16(32, 2, true); // block align = channels * 2 bytes
  view.setUint16(34, 16, true); // bits per sample
  writeStr(36, "data");
  view.setUint32(40, dataBytes, true);
  new Int16Array(buffer, 44).set(int16);
  return buffer;
}
