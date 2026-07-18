import unittest
from pathlib import Path

from chat_service import character_prompt, load_character


ROOT = Path(__file__).resolve().parents[1]


class ChatServiceTests(unittest.TestCase):
    def test_lily_sample_is_loadable(self):
        character = load_character(ROOT / "characters" / "lily.sample.json")
        self.assertEqual("リリー", character["name"])
        self.assertIn("サンプル", character["description"])

    def test_character_prompt_uses_system_prompt(self):
        character = load_character(ROOT / "characters" / "lily.sample.json")
        prompt = character_prompt(character)
        self.assertIn("リリー", prompt)
        self.assertIn("ツンデレ", prompt)


if __name__ == "__main__":
    unittest.main()
