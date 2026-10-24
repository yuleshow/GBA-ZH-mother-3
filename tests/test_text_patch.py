import struct
import unittest

from tools.build_font_patch import Character
from tools.build_text_patch import character_runs, phrase_locations, phrase_matches


class PhraseTests(unittest.TestCase):
    def setUp(self):
        self.charset = [
            Character(0x300, "面", "面", "test"),
            Character(0x400, "面", "面", "test"),
            Character(0x301, "包", "包", "test"),
        ]

    def test_all_duplicate_ids_match(self):
        data = struct.pack("<4H", 0x300, 0x301, 0x400, 0x301)
        self.assertEqual(phrase_matches(data, "面包", self.charset), [0, 4])

    def test_control_code_interrupts_phrase(self):
        data = struct.pack("<3H", 0x300, 0xFFFF, 0x301)
        self.assertEqual(phrase_matches(data, "面包", self.charset), [])

    def test_unaligned_bytes_do_not_match(self):
        data = b"\x00" + struct.pack("<2H", 0x300, 0x301)
        self.assertEqual(phrase_matches(data, "面包", self.charset), [])

    def test_missing_character_rejected(self):
        with self.assertRaisesRegex(ValueError, "No source glyph"):
            phrase_matches(b"", "missing", self.charset)

    def test_control_arguments_are_not_text(self):
        data = struct.pack("<6H", 0x300, 0xFF26, 0x300, 0x301, 0x301, 0xFFFF)
        self.assertEqual(list(character_runs(data, 0, len(data))), [(0, 2), (8, 10)])

    def test_unknown_and_truncated_controls_rejected(self):
        for values in ((0xFFFE,), (0xFF26, 0x300)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                list(character_runs(struct.pack(f"<{len(values)}H", *values), 0, len(values) * 2))

    def test_unreferenced_phrase_is_excluded(self):
        data = struct.pack("<4H", 0x300, 0x301, 0x400, 0x301)
        self.assertEqual(phrase_locations(data, "面包", self.charset, [(4, 8)]), [4])