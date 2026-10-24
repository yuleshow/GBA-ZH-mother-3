import re
import struct
import unittest

from tools.validate_enemy_one_hit import ORIGINAL, PATCH_OFFSET, cheat_code, cheat_file


class EnemyCheatEncodingTests(unittest.TestCase):
    def test_halfword_addresses_round_trip(self):
        patch = bytearray(ORIGINAL)
        struct.pack_into("<H", patch, 6, 0x6AE8)
        struct.pack_into("<H", patch, 14, 0x2400)
        words = [int(word, 16) for word in cheat_code(patch).split("+")]
        restored = bytearray(ORIGINAL)
        for index in range(0, len(words), 4):
            command, address, value, unused = words[index:index + 4]
            self.assertEqual(command, 0)
            self.assertEqual(unused, 0)
            self.assertEqual(address & 0xFF000000, 0x18000000)
            offset = ((address & 0xFFFFFF) << 1) - PATCH_OFFSET
            self.assertGreaterEqual(offset, 0)
            self.assertLess(offset, len(ORIGINAL))
            struct.pack_into("<H", restored, offset, value)
        self.assertEqual(restored, patch)
        self.assertEqual(len(words), 8)

    def test_empty_and_wrong_size_patches_are_rejected(self):
        for patch in (ORIGINAL, b"", ORIGINAL[:-1], ORIGINAL + b"\0\0"):
            with self.subTest(length=len(patch)), self.assertRaises(ValueError):
                cheat_code(patch)

    def test_file_is_disabled_and_uses_emulator_handler(self):
        patch = bytearray(ORIGINAL)
        patch[6:8] = bytes.fromhex("e86a")
        content = cheat_file(patch)
        self.assertIn("cheats = 1\n", content)
        self.assertIn("cheat0_enable = false\n", content)
        self.assertIn("cheat0_handler = 0\n", content)
        self.assertIn("EXPERIMENTAL", content)
        code = re.search(r'^cheat0_code = "([^"]+)"$', content, re.MULTILINE)
        self.assertIsNotNone(code)
        self.assertEqual(code.group(1), cheat_code(patch))


if __name__ == "__main__":
    unittest.main()
