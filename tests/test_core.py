import tempfile
import unittest
from pathlib import Path

from studio_core import build_plan


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.settings = {
            "target_chars": 120,
            "backtrack_chars": 50,
            "ignored_texts": ["【読まない】"],
            "speaker_switch_enabled": True,
            "speaker_tag_pattern": "【{name}】",
            "speaker_markers": {},
            "speaker_fuzzy_threshold": 0.62,
            "silence_sentence_ms": 150,
            "silence_dialogue_ms": 220,
            "silence_hard_ms": 100,
        }

    def test_long_text_never_exceeds_target(self):
        text = "これは自然に分割される文章です。" * 30
        plan = build_plan(text, self.settings, self.root)
        self.assertTrue(plan)
        self.assertLessEqual(max(item["length"] for item in plan), 120)

    def test_ignored_text_is_removed(self):
        plan = build_plan("【読まない】本文です。", self.settings, self.root)
        self.assertEqual("本文です。", "".join(item["text"] for item in plan))

    def test_reader_tag_applies_until_next_tag(self):
        self.settings["reader_extraction_enabled"] = True
        plan = build_plan("【佐藤】こんにちは。【花子】おはよう。", self.settings, self.root)
        self.assertEqual(["佐藤", "花子"], [item["speaker"] for item in plan])

    def test_reader_delimiters_are_customizable(self):
        self.settings.update(reader_extraction_enabled=True, speaker_tag_pattern="({name})")
        plan = build_plan("(佐藤)こんにちは。(花子)おはよう。", self.settings, self.root)
        self.assertEqual(["佐藤", "花子"], [item["speaker"] for item in plan])

    def test_prefix_only_pattern_is_supported(self):
        voice_dir = self.root / "data" / "voice_refs"
        voice_dir.mkdir(parents=True)
        sato, hanako = voice_dir / "佐藤.wav", voice_dir / "花子.wav"
        sato.write_bytes(b"RIFF")
        hanako.write_bytes(b"RIFF")
        self.settings.update(reader_extraction_enabled=True, speaker_markers={"@@佐藤": "data/voice_refs/佐藤.wav", "@@花子": "data/voice_refs/花子.wav"})
        plan = build_plan("@@佐藤 こんにちは。@@花子 おはよう。", self.settings, self.root)
        self.assertEqual(["佐藤", "花子"], [item["speaker"] for item in plan])

    def test_arbitrary_exact_markers_are_supported(self):
        voice_dir = self.root / "data" / "voice_refs"
        voice_dir.mkdir(parents=True)
        sato, hanako = voice_dir / "佐藤.wav", voice_dir / "花子.wav"
        sato.write_bytes(b"RIFF")
        hanako.write_bytes(b"RIFF")
        self.settings.update(reader_extraction_enabled=True, speaker_markers={"＜佐藤＞": "data/voice_refs/佐藤.wav", "【花子】": "data/voice_refs/花子.wav"})
        plan = build_plan("＜佐藤＞こんにちは。【花子】おはよう。", self.settings, self.root)
        self.assertEqual(["佐藤", "花子"], [item["speaker"] for item in plan])

    def test_name_only_mapping_consumes_the_whole_recommended_tag(self):
        voice_dir = self.root / "data" / "voice_refs"
        voice_dir.mkdir(parents=True)
        sato, hanako = voice_dir / "佐藤.wav", voice_dir / "花子.wav"
        sato.write_bytes(b"RIFF")
        hanako.write_bytes(b"RIFF")
        self.settings.update(reader_extraction_enabled=True, speaker_markers={"佐藤": "data/voice_refs/佐藤.wav", "花子": "data/voice_refs/花子.wav"})
        plan = build_plan("【佐藤】今日もいい天気だなあ♪【花子】いい天気ですね！", self.settings, self.root)
        self.assertEqual(["今日もいい天気だなあ♪", "いい天気ですね！"], [item["text"] for item in plan])
        self.assertEqual(["佐藤", "花子"], [item["speaker"] for item in plan])
        self.assertNotIn("【", "".join(item["text"] for item in plan))
        self.assertNotIn("】", "".join(item["text"] for item in plan))

    def test_unrelated_explicit_mapping_does_not_disable_auto_tag_detection(self):
        voice_dir = self.root / "data" / "voice_refs"
        voice_dir.mkdir(parents=True)
        sato = voice_dir / "佐藤.wav"
        sato.write_bytes(b"RIFF")
        self.settings.update(reader_extraction_enabled=True, speaker_markers={"@@花子": "data/voice_refs/佐藤.wav"})
        plan = build_plan("【佐藤】こんにちは。", self.settings, self.root)
        self.assertEqual("こんにちは。", plan[0]["text"])
        self.assertEqual("佐藤", plan[0]["speaker"])

    def test_explicit_mapping_has_priority(self):
        voice_dir = self.root / "data" / "voice_refs"
        voice_dir.mkdir(parents=True)
        wav = voice_dir / "別名.wav"
        wav.write_bytes(b"RIFF")
        self.settings.update(reader_extraction_enabled=True, speaker_aliases={"佐藤": "data/voice_refs/別名.wav"})
        plan = build_plan("【佐藤】こんにちは。", self.settings, self.root)
        self.assertEqual("explicit", plan[0]["speaker_match"])
        self.assertEqual(wav.resolve(), Path(plan[0]["voice_ref"]))

    def test_chat_style_fixed_voice_ignores_story_speaker_names(self):
        voice_dir = self.root / "data" / "voice_refs"
        voice_dir.mkdir(parents=True)
        selected = voice_dir / "選択.wav"
        other = voice_dir / "別人.wav"
        selected.write_bytes(b"RIFF")
        other.write_bytes(b"RIFF")
        self.settings.update(
            reader_extraction_enabled=False,
            speaker_switch_enabled=False,
            default_voice=str(selected),
            speaker_markers={"花子": str(other)},
        )
        plan = build_plan("花子の話を聞いたよ。", self.settings, self.root)
        self.assertEqual("花子の話を聞いたよ。", plan[0]["text"])
        self.assertEqual(selected.resolve(), Path(plan[0]["voice_ref"]))

    def test_quick_voice_is_fallback_but_explicit_speaker_wins(self):
        voice_dir = self.root / "data" / "voice_refs"
        voice_dir.mkdir(parents=True)
        quick = voice_dir / "今回.wav"
        sato = voice_dir / "佐藤.wav"
        quick.write_bytes(b"RIFF")
        sato.write_bytes(b"RIFF")
        self.settings.update(
            reader_extraction_enabled=True,
            default_voice=str(quick),
            speaker_markers={"【佐藤】": str(sato)},
        )
        plan = build_plan("最初の地の文。【佐藤】こんにちは。", self.settings, self.root)
        self.assertEqual(quick.resolve(), Path(plan[0]["voice_ref"]))
        self.assertEqual(sato.resolve(), Path(plan[1]["voice_ref"]))

    def test_quick_voice_replaces_missing_explicit_voice(self):
        voice_dir = self.root / "data" / "voice_refs"
        voice_dir.mkdir(parents=True)
        quick = voice_dir / "今回.wav"
        quick.write_bytes(b"RIFF")
        self.settings.update(
            default_voice=str(quick),
            reader_extraction_enabled=True,
            speaker_switch_enabled=True,
            speaker_markers={"【佐藤】": "data/voice_refs/deleted.wav"},
        )
        plan = build_plan("【佐藤】こんにちは。", self.settings, self.root)
        self.assertEqual(quick.resolve(), Path(plan[0]["voice_ref"]))
        self.assertEqual("default", plan[0]["speaker_match"])


if __name__ == "__main__":
    unittest.main()
