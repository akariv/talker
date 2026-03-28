from http.server import HTTPServer, BaseHTTPRequestHandler
import math
import struct
import wave

audio_buffer = bytearray()


def save_as_wav(pcm_data, filename="debug_recording.wav", rate=16000):
    """Save raw 16-bit mono PCM as a WAV file for inspection."""
    with wave.open(filename, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(rate)
        wf.writeframes(pcm_data)
    print(f"  Saved {filename} ({len(pcm_data)} bytes, {len(pcm_data)/(rate*2):.1f}s)")

    # Quick stats
    num_samples = len(pcm_data) // 2
    if num_samples > 0:
        samples = struct.unpack(f"<{num_samples}h", pcm_data)
        peak = max(abs(s) for s in samples)
        rms = int((sum(s*s for s in samples) / num_samples) ** 0.5)
        print(f"  Stats: peak={peak}/32767, RMS={rms}, samples={num_samples}")

# Set to True to replace playback with a clean 440Hz sine (for testing DAC)
SINE_TEST = False


def generate_sine_16bit(duration_secs, freq=440.0, rate=16000):
    """Generate 16-bit signed 16kHz PCM sine wave."""
    num_samples = int(duration_secs * rate)
    amplitude = 10000  # well within 16-bit range
    buf = bytearray(num_samples * 2)
    for i in range(num_samples):
        sample = int(amplitude * math.sin(2 * math.pi * freq * i / rate))
        struct.pack_into("<h", buf, i * 2, sample)
    print(f"  Generated {freq}Hz sine: {num_samples} samples, {len(buf)} bytes")
    return buf


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global audio_buffer

        if self.path == "/upload":
            length = int(self.headers["Content-Length"])
            chunk = self.rfile.read(length)
            audio_buffer.extend(chunk)
            total_secs = len(audio_buffer) / (16000 * 2)
            print(f"  Chunk: {len(chunk)} bytes | Total: {len(audio_buffer)} bytes ({total_secs:.1f}s)")
            self.send_response(200)
            self.end_headers()

        elif self.path == "/done":
            total_secs = len(audio_buffer) / (16000 * 2)
            print(f"Recording complete: {len(audio_buffer)} bytes ({total_secs:.1f}s)")
            save_as_wav(audio_buffer)
            self.send_response(200)
            self.end_headers()

        elif self.path == "/reset":
            audio_buffer = bytearray()
            print("Buffer cleared")
            self.send_response(200)
            self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/playback":
            total_secs = len(audio_buffer) / (16000 * 2)
            print(f"Playback requested: {len(audio_buffer)} bytes ({total_secs:.1f}s)")

            if SINE_TEST:
                data = generate_sine_16bit(total_secs)
                print(f"  SINE_TEST: replacing audio with 440Hz sine ({len(data)} bytes)")
            else:
                data = audio_buffer

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()

            offset = 0
            chunk_size = 4096
            while offset < len(data):
                end = min(offset + chunk_size, len(data))
                self.wfile.write(data[offset:end])
                offset = end
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


server = HTTPServer(("0.0.0.0", 8080), Handler)
print("Echo server listening on port 8080...")
server.serve_forever()
