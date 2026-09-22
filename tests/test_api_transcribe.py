"""Local-only HTTP and audio checks. No real provider or API key is used."""

from contextlib import contextmanager
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
import wave

import numpy as np

import api_transcribe as api


def write_audio(path, samples):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(samples.astype("<i2").tobytes())


@contextmanager
def mock_provider(statuses=None, content_type="application/json", body=None, charset="utf-8"):
    records = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            message = BytesParser(policy=default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + data
            )
            fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                      for part in message.iter_parts()}
            records.append({"path": self.path, "fields": fields, "authorization": self.headers.get("Authorization")})
            status = statuses[min(len(records) - 1, len(statuses) - 1)] if statuses else 200
            payload = body if body is not None else json.dumps({"text": f"片段 {len(records)}"}, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + (f"; charset={charset}" if charset else ""))
            self.send_header("Content-Length", str(len(payload)))
            if 300 <= status < 400:
                self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class ApiTranscribeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="v2t-api-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.audio = self.directory / "input.wav"
        self.samples = np.full(16000, 10000, dtype=np.int16)
        write_audio(self.audio, self.samples)

    def test_preflight_is_local_and_does_not_echo_secret(self):
        with patch.object(api.requests.Session, "request", side_effect=AssertionError("No network allowed")):
            valid = api.api_preflight("https://example.com/v1", "fake-key", "custom-model")
            self.assertTrue(valid["ok"])
            self.assertFalse(valid["verified"])
            for url in ("http://remote.example/v1", "https://user:secret@example.com/v1",
                        "https://example.com/v1?key=secret", "https://example.com/#secret",
                        "ftp://localhost/v1", "https://example.com:bad/v1", "https://example.com/\nsecret"):
                result = api.api_preflight(url, "fake-key", "custom-model")
                self.assertFalse(result["ok"], url)
                self.assertNotIn("secret", json.dumps(result))

    def test_local_requests_preserve_audio_and_order(self):
        samples = np.arange(16000 * 3 + 4000, dtype=np.int16)
        samples[:] = 10000  # No silence: deterministic one-second boundaries.
        write_audio(self.audio, samples)
        with mock_provider() as (url, records):
            text = api.transcribe_api(self.audio, "zh", url, "fake-key", "custom-model", chunk_seconds=1)
        self.assertEqual(text, "片段 1\n片段 2\n片段 3\n片段 4")
        restored = bytearray()
        for record in records:
            self.assertEqual(record["path"], "/v1/audio/transcriptions")
            self.assertEqual(record["authorization"], "Bearer fake-key")
            self.assertEqual(record["fields"]["model"], b"custom-model")
            self.assertEqual(record["fields"]["language"], b"zh")
            self.assertEqual(record["fields"]["response_format"], b"json")
            with wave.open(io.BytesIO(record["fields"]["file"])) as chunk:
                self.assertEqual((chunk.getnchannels(), chunk.getsampwidth(), chunk.getframerate()), (1, 2, 16000))
                self.assertLessEqual(chunk.getnframes(), 16000)
                restored.extend(chunk.readframes(chunk.getnframes()))
        self.assertEqual(restored, samples.astype("<i2").tobytes())

    def test_quiet_boundary_keeps_every_sample(self):
        samples = np.full(16000 * 4, 10000, dtype=np.int16)
        samples[25600:28800] = 0  # Silence from 1.6 to 1.8 seconds.
        write_audio(self.audio, samples)
        chunks = list(api._wav_chunks(self.audio, self.directory, 2))
        lengths, restored = [], bytearray()
        for path in chunks:
            with wave.open(str(path)) as chunk:
                lengths.append(chunk.getnframes())
                restored.extend(chunk.readframes(chunk.getnframes()))
        self.assertGreater(lengths[0], 25600)
        self.assertLess(lengths[0], 28800)
        self.assertTrue(all(length <= 32000 for length in lengths))
        self.assertEqual(restored, samples.astype("<i2").tobytes())

    def test_stereo_audio_is_resampled_to_mono(self):
        # Includes resampler flush samples, which must survive chunking.
        with wave.open(str(self.audio), "wb") as audio:
            audio.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
            audio.writeframes(np.full((44100, 2), 10000, dtype="<i2").tobytes())
        chunks = list(api._wav_chunks(self.audio, self.directory, 1))
        count = 0
        for path in chunks:
            with wave.open(str(path)) as chunk:
                self.assertEqual((chunk.getnchannels(), chunk.getsampwidth(), chunk.getframerate()), (1, 2, 16000))
                count += chunk.getnframes()
        self.assertEqual(count, 16000)

    def test_transient_error_has_three_attempt_limit(self):
        with mock_provider([429]) as (url, records), patch.object(api, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "已尝试 3 次"):
                api.transcribe_api(self.audio, "auto", url, "fake-key", "custom-model")
        self.assertEqual(len(records), 3)
        self.assertNotIn("language", records[0]["fields"])

    def test_transient_errors_can_recover(self):
        with mock_provider([500, 429, 200]) as (url, records), patch.object(api, "sleep"):
            text = api.transcribe_api(self.audio, "en", url, "fake-key", "custom-model")
        self.assertEqual(len(records), 3)
        self.assertEqual(text, "片段 3")

    def test_network_failure_is_sanitized_and_bounded(self):
        with patch.object(api.requests.Session, "post", side_effect=api.requests.Timeout("fake-key")) as post, patch.object(api, "sleep"):
            with self.assertRaises(RuntimeError) as error:
                api.transcribe_api(self.audio, "en", "https://example.com/v1", "fake-key", "model")
            self.assertEqual(post.call_count, 3)
            self.assertNotIn("fake-key", str(error.exception))

    def test_auth_and_redirect_errors_are_not_retried(self):
        for status in (401, 403, 302):
            with self.subTest(status=status), mock_provider([status], body=b"fake-key") as (url, records):
                with self.assertRaises(RuntimeError) as error:
                    api.transcribe_api(self.audio, "auto", url, "fake-key", "custom-model")
                self.assertNotIn("fake-key", str(error.exception))
                self.assertEqual(len(records), 1)

    def test_plain_text_allowed_but_html_rejected(self):
        with mock_provider(content_type="text/plain", body="你好".encode()) as (url, _):
            self.assertEqual(api.transcribe_api(self.audio, "zh", url, "fake-key", "custom-model"), "你好")
        with mock_provider(content_type="text/html", body=b"<html>login</html>") as (url, records):
            with self.assertRaisesRegex(RuntimeError, "内容类型"):
                api.transcribe_api(self.audio, "auto", url, "fake-key", "custom-model")
            self.assertEqual(len(records), 1)

    def test_plain_text_without_charset_defaults_to_utf8(self):
        with mock_provider(content_type="text/plain", body="你好，世界。".encode("utf-8"), charset=None) as (url, records):
            text = api.transcribe_api(self.audio, "zh", url, "fake-key", "custom-model")
        self.assertEqual(text, "你好，世界。")
        self.assertEqual(len(records), 1)

    def test_failure_cleans_temporary_chunks(self):
        scratch_paths = []
        real_temp = api.TemporaryDirectory

        def tracked_temp(*args, **kwargs):
            result = real_temp(*args, **kwargs)
            scratch_paths.append(Path(result.name))
            return result

        with mock_provider([401]) as (url, _), patch.object(api, "TemporaryDirectory", side_effect=tracked_temp):
            with self.assertRaises(RuntimeError):
                api.transcribe_api(self.audio, "auto", url, "fake-key", "custom-model")
        self.assertEqual(len(scratch_paths), 1)
        self.assertFalse(scratch_paths[0].exists())

    def test_invalid_chunk_and_language_fail_before_network(self):
        with patch.object(api.requests.Session, "request", side_effect=AssertionError("No network allowed")):
            for seconds in (0, -1, 601, True, 1.5):
                with self.assertRaises(ValueError):
                    api.transcribe_api(self.audio, "auto", "https://example.com/v1", "fake-key", "model", seconds)
            with self.assertRaises(ValueError):
                api.transcribe_api(self.audio, "fr", "https://example.com/v1", "fake-key", "model")


if __name__ == "__main__":
    unittest.main()
