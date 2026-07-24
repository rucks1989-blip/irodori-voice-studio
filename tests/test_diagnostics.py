import io
import json
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from long_jobs import LongJobManager
from studio_core import build_plan, synthesize_chunk, validate_voice_reference


def wav_bytes(seconds=0.05, sample_rate=24000):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return output.getvalue()


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        investigation = Path(__file__).resolve().parents[1] / "investigation"
        investigation.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=investigation)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_invalid_clone_reference_stops_before_http(self):
        settings = {
            "model": "test",
            "audio_cpp_url": "http://127.0.0.1:1/v1/audio/speech",
        }
        with patch("studio_core.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "参照WAVが見つかりません"):
                synthesize_chunk("テスト", "missing-reference.wav", settings, {"request_mode": "clone"})
            post.assert_not_called()

    def test_missing_configured_reference_remains_required(self):
        root = self.root
        settings = {
            "default_voice": "data/voice_refs/存在しない参照.wav",
            "target_chars": 120,
            "backtrack_chars": 50,
        }
        plan = build_plan("参照音声なしで生成してはいけません。", settings, root)
        self.assertEqual(1, len(plan))
        self.assertEqual("", plan[0]["voice_ref"])
        self.assertTrue(plan[0]["voice_ref_required"])
        self.assertEqual("default_missing", plan[0]["speaker_match"])

    def test_failed_job_records_missing_required_reference(self):
        root = self.root
        config = {
            "model": "test-model",
            "target_chars": 120,
            "backtrack_chars": 50,
            "fade_ms": 0,
            "audio_cpp_health_url": "http://127.0.0.1:1/health",
        }
        plan = [{
            "text": "参照WAVがないため停止します。",
            "voice_ref": str(root / "存在しない参照.wav"),
            "voice_ref_required": True,
            "silence_after_ms": 0,
            "reason": "end",
            "speaker": "",
            "speaker_match": "default_missing",
        }]
        manager = LongJobManager(root, threading.Lock(), lambda item: None, lambda config: None)
        with patch.object(manager, "_backend_ready", return_value=True), patch("studio_core.requests.post") as post:
            public = manager.create("参照WAVがないため停止します。", config, plan)
            deadline = time.time() + 5
            while time.time() < deadline:
                state = manager.get(public["id"])
                if state["status"] == "failed":
                    break
                time.sleep(0.02)
        self.assertEqual("failed", state["status"])
        post.assert_not_called()
        chunk_path = root / "outputs" / "jobs" / public["id"] / "chunks" / "chunk_0001.json"
        chunk = json.loads(chunk_path.read_text(encoding="utf-8"))
        self.assertEqual("clone", chunk["request_mode"])
        self.assertTrue(chunk["voice_ref_requested"])
        self.assertEqual(1, chunk["attempt"])
        self.assertIn("参照WAVが見つかりません", chunk["error"])

    def test_job_keeps_chunks_text_metadata_reference_and_final(self):
        root = self.root
        reference = root / "参照音声.wav"
        reference.write_bytes(wav_bytes())
        config = {
            "model": "test-model",
            "target_chars": 120,
            "backtrack_chars": 50,
            "fade_ms": 0,
            "chunk_wav_retention": "keep",
            "audio_cpp_health_url": "http://127.0.0.1:1/health",
        }
        plan = [{
            "text": "日本語の診断テキストです。",
            "voice_ref": str(reference),
            "silence_after_ms": 0,
            "reason": "sentence",
            "speaker": "",
            "speaker_match": "default",
        }]
        manager = LongJobManager(root, threading.Lock(), lambda item: None, lambda config: None)
        diagnostics = {}

        def fake_synthesize(text, voice_ref, settings, diagnostic_context):
            info = validate_voice_reference(voice_ref, required=True)
            diagnostic_context.update({
                "request_id": "test-request",
                "request_mode": "clone",
                "request_started_at": "2026-07-24T00:00:00+09:00",
                "request_finished_at": "2026-07-24T00:00:01+09:00",
                "http_status": 200,
                "request": {
                    "voice_ref_path": voice_ref,
                    "voice_ref_sha256": info["voice_ref_sha256"],
                },
                "voice_reference": info,
            })
            diagnostics.update(diagnostic_context)
            return wav_bytes()

        with patch.object(manager, "_backend_ready", return_value=True), patch("long_jobs.synthesize_chunk", side_effect=fake_synthesize):
            public = manager.create("日本語の診断テキストです。", config, plan)
            deadline = time.time() + 5
            while time.time() < deadline:
                state = manager.get(public["id"])
                if state["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.02)

        self.assertEqual("completed", state["status"], state.get("error"))
        job_dir = root / "outputs" / "jobs" / public["id"]
        self.assertTrue((job_dir / "final.wav").is_file())
        self.assertTrue((job_dir / "chunks" / "chunk_0001.wav").is_file())
        self.assertEqual("日本語の診断テキストです。", (job_dir / "chunks" / "chunk_0001.txt").read_text(encoding="utf-8"))
        chunk = json.loads((job_dir / "chunks" / "chunk_0001.json").read_text(encoding="utf-8"))
        self.assertEqual(1, chunk["attempt"])
        self.assertEqual("clone", chunk["request_mode"])
        self.assertEqual(diagnostics["voice_reference"]["voice_ref_sha256"], chunk["voice_ref_sha256"])
        self.assertTrue((job_dir / "reference" / "voice_ref.json").is_file())


if __name__ == "__main__":
    unittest.main()
