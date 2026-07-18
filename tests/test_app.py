import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import registered_voice
from studio_core import save_temporary_wav


class AppTests(unittest.TestCase):
    def test_registered_voice_accepts_only_registered_wav(self):
        root = Path(tempfile.mkdtemp())
        folder = root / "data" / "voice_refs"
        folder.mkdir(parents=True)
        wav = folder / "sample.wav"
        wav.write_bytes(b"RIFF")
        outside = root / "outside.wav"
        outside.write_bytes(b"RIFF")
        with patch("app.ROOT", root):
            self.assertEqual(str(wav.resolve()), registered_voice("data/voice_refs/sample.wav"))
            with self.assertRaises(ValueError):
                registered_voice(str(outside))

    def test_temporary_voice_is_not_saved_in_project(self):
        root = Path(tempfile.mkdtemp())
        raw = b"RIFF" + (b"\x00" * 4) + b"WAVE" + (b"\x00" * 32)
        import base64

        path = save_temporary_wav("test.wav", base64.b64encode(raw).decode("ascii"))
        try:
            self.assertTrue(path.is_file())
            self.assertFalse(str(path).startswith(str(root)))
        finally:
            path.unlink(missing_ok=True)

    def test_non_wave_riff_is_rejected(self):
        import base64

        raw = b"RIFF" + (b"\x00" * 40)
        with self.assertRaises(ValueError):
            save_temporary_wav("not-wave.wav", base64.b64encode(raw).decode("ascii"))


if __name__ == "__main__":
    unittest.main()
