from pathlib import Path
import ctypes
import struct
import unittest
import zlib

from PIL import Image, ImageDraw, ImageFont

from tools.build_font_patch import BANKS, ROOT, encode_glyph, read_bdf
from tools.build_rom import (
    C2_PRESERVED_CHARACTERS, apply_bps, build, c2_font_records, c2_spacing_records,
    create_bps, variable_integer,
)
from tools.build_screen_patch import (
    CHAPTER_RELOCATED, MISC_TABLE, TITLE_TABLE, WELCOME_RELOCATED, WELCOME_TABLE,
    RELEASE_VERSION,
    compress, decompress, encode_image, padding_record, render, table_entries,
)
from tools.build_text_patch import (
    CLASSIFIER_OFFSETS, ENDING_BANK, ENDING_DATA, ENDING_END, ENDING_INSERTIONS,
    ENDING_RELOCATED, ENDING_START, SCRIPT_TABLE, corrected_charset, phrase_locations, text_runs,
    SPECIAL_COUNT, SPECIAL_FONT, SPECIAL_TARGETS,
)


class BinaryTests(unittest.TestCase):
    def test_bps_known_source_read(self):
        source = b"abc"
        expected = b"BPS1\x83\x83\x80\x88" + struct.pack("<II", zlib.crc32(source), zlib.crc32(source))
        expected += struct.pack("<I", zlib.crc32(expected))
        self.assertEqual(create_bps(source, source), expected)

    def test_bps_round_trip_and_checksums(self):
        for source, target in ((b"", b""), (b"abc", b"abd"),
                               (b"a" * 18000, b"b" * 18000)):
            patch = create_bps(source, target)
            self.assertEqual(apply_bps(source, patch), target)
            with self.assertRaises(ValueError):
                apply_bps(source, patch[:-1] + bytes([patch[-1] ^ 1]))
        with self.assertRaises(ValueError):
            apply_bps(b"xyz", create_bps(b"abc", b"def"))

    def test_bps_integer_boundaries(self):
        self.assertEqual(variable_integer(0), b"\x80")
        self.assertEqual(variable_integer(127), b"\xff")
        self.assertEqual(variable_integer(128), b"\x00\x80")
        self.assertEqual(variable_integer(16511), b"\x7f\xff")
        self.assertEqual(variable_integer(16512), b"\x00\x00\x80")
        with self.assertRaises(ValueError):
            variable_integer(-1)

    def test_lz77_known_literals_and_overlapping_copy(self):
        self.assertEqual(decompress(b"\x10\x03\x00\x00\x00abc"), b"abc")
        self.assertEqual(decompress(b"\x10\x07\x00\x00\x40a\x30\x00"), b"a" * 7)

    def test_lz77_round_trip(self):
        for data in (b"x", bytes(0x6000), bytes(range(256)) * 30, b"abcabc" * 300):
            packed = compress(data)
            self.assertEqual(len(packed) % 4, 0)
            self.assertEqual(decompress(packed), data)

    def test_lz77_invalid_data_rejected(self):
        for data in (b"", b"\x11\x01\x00\x00", b"\x10\x03\x00\x00\x80\x00\x00",
                     b"\x10\x07\x00\x00\x40a\x30"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                decompress(data)

    def test_compressor_avoids_one_byte_vram_back_references(self):
        data = b"\x00\xff\xff\xff\xff" * 500
        packed = compress(data)
        position, decoded = 4, 0
        while decoded < len(data):
            flags = packed[position]
            position += 1
            for bit in range(7, -1, -1):
                if decoded == len(data):
                    break
                if flags & (1 << bit):
                    first, second = packed[position:position + 2]
                    distance = ((first & 15) << 8 | second) + 1
                    self.assertGreaterEqual(distance, 2)
                    decoded += (first >> 4) + 3
                    position += 2
                else:
                    decoded += 1
                    position += 1

    def test_indexed_tile_round_trip(self):
        image = Image.new("P", (256, 256))
        image.putdata([(column // 8 + row // 8) % 16 for row in range(256) for column in range(256)])
        palette = b"".join(struct.pack("<H", index | index << 5 | index << 10) for index in range(16))
        tiles, tilemap = encode_image(image, 16)
        decoded = render(tiles, palette, tilemap)
        self.assertEqual(decoded.tobytes(), b"".join(bytes([value * 255 // 31]) * 3
                              for value in image.tobytes()))
        with self.assertRaises(ValueError):
            encode_image(image, 15)

    def test_padding_guards(self):
        with self.assertRaises(ValueError):
            padding_record(b"", 0, b"x")
        with self.assertRaises(ValueError):
            padding_record(bytes(0x2000000), 0x1F90000, b"x")


class ReviewedRomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sources = list((ROOT / "assets").glob("*.gba"))
        font = Path("/System/Library/Fonts/STHeiti Medium.ttc")
        if not sources or not font.exists():
            raise unittest.SkipTest("Requires the local source ROM and chapter font")
        cls.source = sources[0].read_bytes()
        cls.output, cls.patch, cls.report, cls.welcome, cls.chapters = build(
            cls.source, ROOT / "assets/yuleshow-chop.png", font)
        cls.charset = corrected_charset()
        cls.original_runs = text_runs(cls.source)
        cls.targets = {entry.index: entry.target for entry in cls.charset}
        cls.targets.update({int(slot, 16): character for character, slot in cls.report["text"]["glyph_slots"].items()})

    def relocated(self, offset):
        if 0x136C644 <= offset < 0x13718A0:
            return 0x1F88000 + offset - 0x136C644 + (4 if offset >= 0x137033A else 0)
        if ENDING_START <= offset < ENDING_END:
            return ENDING_RELOCATED + offset - ENDING_START + sum(
                2 for position, _ in ENDING_INSERTIONS if offset >= position)
        return offset

    def displayed(self, offset, length):
        return "".join(self.targets[value] for (value,) in struct.iter_unpack(
            "<H", self.output[offset:offset + length * 2]))

    def test_all_phrase_occurrences_including_relocated_common_bank(self):
        expected = [19, 4, 261, 6, 1, 3, 10, 3, 13, 28, 8,
                    7, 3, 11, 18, 2, 1, 1, 1, 1, 5, 1, 3, 1, 1, 1, 55, 3, 9]
        self.assertEqual([item["count"] for item in self.report["text"]["phrases"][:29]], expected)
        for item in self.report["text"]["phrases"]:
            self.assertGreater(item["count"], 0)
            for offset in item["offsets"]:
                self.assertEqual(self.displayed(self.relocated(int(offset, 16)), len(item["target"])), item["target"])

    def test_final_batch_wording(self):
        for before, after in (("潜", "潛"), ("大伙", "大夥"),
                              ("鸟", "鳥"), ("着", "著")):
            locations = phrase_locations(self.source, before, self.charset, self.original_runs)
            self.assertGreater(len(locations), 0, before)
            for offset in locations:
                self.assertEqual(self.displayed(self.relocated(offset), len(after)), after)

    def test_dryness_uses_qian_not_gan(self):
        locations = phrase_locations(self.source, "干燥", self.charset, self.original_runs)
        self.assertEqual(len(locations), 7)
        for offset in locations:
            self.assertEqual(self.displayed(self.relocated(offset), 2), "乾燥")

    def test_reviewed_dry_and_hair_contexts(self):
        corrections = (
            ("干燥", "乾燥"), ("烘干", "烘乾"), ("干净", "乾淨"),
            ("饼干", "餅乾"), ("干杯", "乾杯"), ("干脆", "乾脆"),
            ("干涸", "乾涸"), ("干冰", "乾冰"), ("鱼干", "魚乾"),
            ("衣服干", "衣服乾"), ("肉干", "肉乾"), ("干旱", "乾旱"),
            ("长发", "長髪"), ("发型", "髪型"), ("卷发", "捲髪"),
            ("洗发", "洗髪"), ("理发", "理髪"), ("头发", "頭髪"),
            ("假发", "假髮"),
        )
        reviewed = {}
        for before, after in corrections:
            locations = phrase_locations(self.source, before, self.charset, self.original_runs)
            self.assertGreater(len(locations), 0, before)
            for offset in locations:
                self.assertEqual(self.displayed(self.relocated(offset), len(after)), after)
                for column, character in enumerate(before):
                    if character in "干发":
                        reviewed[offset + column * 2] = after[column]
        for before, default in (("干", "幹"), ("发", "發")):
            locations = phrase_locations(self.source, before, self.charset, self.original_runs)
            for offset in locations:
                self.assertEqual(self.displayed(self.relocated(offset), 1),
                                 reviewed.get(offset, default), hex(offset))

    def test_clay_and_flour_occurrences(self):
        for before, after, count in (("粘土", "黏土", 55), ("面粉", "麵粉", 3)):
            locations = phrase_locations(self.source, before, self.charset, self.original_runs)
            self.assertEqual(len(locations), count)
            for offset in locations:
                self.assertEqual(self.displayed(self.relocated(offset), len(after)), after)

    def test_relax_uses_loose_not_pine(self):
        locations = phrase_locations(self.source, "放松", self.charset, self.original_runs)
        self.assertEqual(len(locations), 9)
        for offset in locations:
            self.assertEqual(self.displayed(self.relocated(offset), 2), "放鬆",
                             f"{len(locations)} occurrences; offset {offset:#x}")
        corrected = {offset + 2 for phrase in ("轻松", "放松")
                     for offset in phrase_locations(self.source, phrase, self.charset, self.original_runs)}
        for offset in phrase_locations(self.source, "松", self.charset, self.original_runs):
            if offset not in corrected:
                self.assertEqual(self.displayed(self.relocated(offset), 1), "松", hex(offset))

    def test_nonphrase_contexts_are_preserved(self):
        for phrase in ("系统", "家人", "干活", "发生", "见面", "紫苏"):
            locations = phrase_locations(self.source, phrase, self.charset, self.original_runs)
            for offset in locations:
                new_offset = self.relocated(offset)
                self.assertEqual(self.output[new_offset:new_offset + len(phrase) * 2],
                                 self.source[offset:offset + len(phrase) * 2])

    def test_classifier_and_adverb_uses(self):
        locations = phrase_locations(self.source, "只", self.charset, self.original_runs)
        self.assertEqual(len(locations), 226)
        for offset in locations:
            self.assertEqual(self.displayed(self.relocated(offset), 1),
                             "隻" if offset in CLASSIFIER_OFFSETS else "只")

    def test_elevator_options_preserve_menu_controls(self):
        locations = phrase_locations(self.source, "不能乘", self.charset, self.original_runs)
        self.assertEqual(len(locations), 12)
        self.assertEqual(self.report["text"]["elevator_option"]["count"], 12)
        for offset in locations:
            self.assertEqual(self.displayed(offset, 2), "不乘")
            self.assertEqual(struct.unpack_from("<4H", self.output, offset + 4),
                             (0xAC, 0xFF01, 0xFF02, 0xFFFF))
            self.assertEqual(self.output[offset - 16:offset], self.source[offset - 16:offset])

    def test_dialogue_pointer_and_easter_egg(self):
        pointers, data = struct.unpack_from("<II", self.output, SCRIPT_TABLE + 4)
        pointers += SCRIPT_TABLE
        data += SCRIPT_TABLE
        relative = struct.unpack_from("<H", self.output, pointers + 116 * 2)[0]
        dialogue = struct.unpack_from("<10H", self.output, data + relative)
        self.assertEqual("".join(self.targets[value] for value in dialogue[1:7]), "誰叫你跳舞了")
        self.assertEqual(dialogue[7:], (8, 9, 0xFFFF))
        start, end = 0x136D7BA, 0x136D7F2
        moved = self.relocated(start)
        self.assertEqual(self.output[moved:moved + end - start], self.source[start:end])
        self.assertEqual(len(text_runs(self.output)), len(self.original_runs))

    def test_ending_quote_newline_and_all_string_pointers(self):
        self.assertEqual(struct.unpack_from("<5H", self.output, self.relocated(0x13D5314)),
                         (0x5E, 0x35, 0x306, 0x307, 0x36))
        self.assertEqual(struct.unpack_from("<5H", self.output, self.relocated(0x13D53B8)),
                         (2, 0xFF01, 0x5E, 0x304, 0x3C9))
        pointers, data = struct.unpack_from("<II", self.output, SCRIPT_TABLE + 4 + ENDING_BANK * 8)
        pointers += SCRIPT_TABLE
        data += SCRIPT_TABLE
        self.assertEqual(pointers, ENDING_RELOCATED)
        for position in range(ENDING_START, ENDING_DATA, 2):
            relative = struct.unpack_from("<H", self.source, position)[0]
            actual = struct.unpack_from("<H", self.output, pointers + position - ENDING_START)[0]
            self.assertEqual(data + actual, self.relocated(ENDING_DATA + relative))

    def test_new_glyphs_have_both_sizes(self):
        for slot in self.report["text"]["glyph_slots"].values():
            for bank in BANKS:
                start = bank.offset + int(slot, 16) * bank.stride
                bitmap = self.output[start:start + bank.stride]
                self.assertTrue(any(bitmap))
        self.assertNotEqual(self.report["text"]["glyph_slots"]["髪"], self.report["text"]["glyph_slots"]["髮"])

    def test_special_font_changes_only_reviewed_slots(self):
        self.assertEqual(SPECIAL_TARGETS[0x5B], "謝")
        self.assertEqual(SPECIAL_TARGETS[0x5D], "顏")
        self.assertNotIn(0x59, SPECIAL_TARGETS)
        self.assertNotIn(0x5C, SPECIAL_TARGETS)
        font = read_bdf(ROOT / "data/fonts/fusion-pixel-12px-monospaced-zh_hant.bdf")
        for slot in range(SPECIAL_COUNT):
            start = SPECIAL_FONT + slot * 32
            bitmap = self.output[start:start + 32]
            if slot not in SPECIAL_TARGETS:
                self.assertEqual(bitmap, self.source[start:start + 32])
                continue
            expected = encode_glyph(font, SPECIAL_TARGETS[slot], BANKS[0])
            self.assertEqual(bytes(value for row in range(11)
                                   for value in (bitmap[row], bitmap[row + 16])), expected)
            self.assertEqual(bitmap[11:16] + bitmap[27:32], bytes(10))

    def test_special_font_renderer_in_mgba(self):
        from tools.validate_enemy_one_hit import Core

        core_path = Path("/tmp/mother3-mgba-core/mgba_libretro.dylib")
        if not core_path.exists():
            self.skipTest("Requires the local mGBA core")
        core = Core(core_path)
        try:
            core.load(self.output)
            for _ in range(120):
                core.library.retro_run()
            baseline = core.save()
            for slot in SPECIAL_TARGETS:
                state = bytearray(baseline)
                state[0x22000:0x22088] = bytes(0x88)
                state[0x22085] = 0x10
                state[0x48CBA] = 4
                struct.pack_into("<I", state, 0x48CAC, 0x08000000 + SPECIAL_FONT)
                for index in range(16):
                    struct.pack_into("<I", state, 0x20 + index * 4, 0)
                for offset, value in ((0x20, 0x02001000), (0x24, slot),
                                      (0x24000, 0xE7FEE7FE), (0x54, 0x03007000),
                                      (0x58, 0x02003001), (0x5C, 0x08009396),
                                      (0x60, 0xFF), (0x2F8, 0), (0x2FC, 0), (0x31C, 2)):
                    struct.pack_into("<I", state, offset, value)
                struct.pack_into("<H", state, 0x608, 0)
                buffer = ctypes.create_string_buffer(bytes(state))
                self.assertTrue(core.library.retro_unserialize(buffer, len(state)))
                for _ in range(3):
                    core.library.retro_run()
                    result = core.save()
                    if struct.unpack_from("<I", result, 0x5C)[0] in (0x02003002, 0x02003004):
                        break
                self.assertIn(struct.unpack_from("<I", result, 0x5C)[0], (0x02003002, 0x02003004))
                bitmap = self.output[SPECIAL_FONT + slot * 32:SPECIAL_FONT + (slot + 1) * 32]
                for row in range(16):
                    for column in range(16):
                        tile = row // 8 * 2 + column // 8
                        offset = 0x22004 + tile * 32 + row % 8 * 4 + column % 8 // 2
                        actual = (result[offset] >> (column % 2 * 4)) & 15
                        expected = int(bool(bitmap[column // 8 * 16 + row] & (128 >> (column % 8))))
                        self.assertEqual(actual, expected, (hex(slot), row, column))
            core.unload()
        finally:
            core.close()

    def test_c2_small_font_covers_all_chinese_slots(self):
        records = c2_font_records(self.report["text"])
        bank = BANKS[1]
        self.assertEqual(len(records), 0x1000 - 0x300 - 1)
        self.assertEqual(len({offset for offset, _ in records}), len(records))
        replaced = {(offset - bank.offset) // bank.stride for offset, _ in records}
        self.assertEqual({self.targets[slot] for slot in self.targets.keys() - replaced},
                 C2_PRESERVED_CHARACTERS)
        for offset, bitmap in records:
            self.assertTrue(bank.offset + 0x300 * bank.stride <= offset < bank.end)
            self.assertEqual(len(bitmap), bank.stride)
            self.assertTrue(any(bitmap))
            self.assertEqual(bitmap[:2], bytes(2))
            self.assertTrue(all(not row & 0x7F for row in bitmap[1::2]))

    def test_c2_spacing_initializer_in_mgba(self):
        from tools.validate_enemy_one_hit import Core

        core_path = Path("/tmp/mother3-mgba-core/mgba_libretro.dylib")
        if not core_path.exists():
            self.skipTest("Requires the local mGBA core")
        rom = bytearray(self.output)
        for offset, data in c2_spacing_records(rom):
            rom[offset:offset + len(data)] = data
        core = Core(core_path)
        try:
            core.load(rom)
            for font_pointer, width, height, expected_width in (
                (0x08CF99F8, 10, 10, 11), (0x08CF99F8, 8, 8, 8),
                (0x08CE39F8, 12, 11, 12), (0x08D0D9F8, 10, 10, 10),
            ):
                state = bytearray(core.baseline)
                state[0x22000:0x22008] = bytes(8)
                struct.pack_into("<IHH", state, 0x23000, font_pointer, width, height)
                struct.pack_into("<I", state, 0x24000, 0xE7FEE7FE)
                for index in range(16):
                    struct.pack_into("<I", state, 0x20 + index * 4, 0)
                for offset, value in ((0x20, 0x02001000), (0x24, 0x02002000),
                                      (0x28, 0x02002004), (0x54, 0x03007000),
                                      (0x58, 0x02003001), (0x5C, 0x08088E4E),
                                      (0x60, 0xFF), (0x2F8, 0), (0x2FC, 0), (0x31C, 2)):
                    struct.pack_into("<I", state, offset, value)
                struct.pack_into("<H", state, 0x608, 0)
                buffer = ctypes.create_string_buffer(bytes(state))
                self.assertTrue(core.library.retro_unserialize(buffer, len(state)))
                for _ in range(3):
                    core.library.retro_run()
                    result = core.save()
                    if struct.unpack_from("<I", result, 0x5C)[0] in (0x02003002, 0x02003004):
                        break
                self.assertIn(struct.unpack_from("<I", result, 0x5C)[0], (0x02003002, 0x02003004))
                self.assertEqual(struct.unpack_from("<HHI", result, 0x22000),
                                 (expected_width, height, font_pointer))
                self.assertEqual(struct.unpack_from("<I", result, 0x20)[0], 1)
                self.assertEqual(struct.unpack_from("<8I", result, 0x30), (0,) * 8)
                self.assertEqual(struct.unpack_from("<I", result, 0x54)[0], 0x03007000)
            core.unload()
        finally:
            core.close()

    def test_c2_build_preserves_reviewed_content(self):
        output, patch, report, _, _ = build(
            self.source, ROOT / "assets/yuleshow-chop.png",
            Path("/System/Library/Fonts/STHeiti Medium.ttc"), c2=True)
        expected = bytearray(self.output)
        records = c2_font_records(self.report["text"]) + c2_spacing_records(self.output)
        for offset, data in records:
            expected[offset:offset + len(data)] = data
        self.assertEqual(output, expected)
        self.assertEqual(apply_bps(self.source, patch), output)
        self.assertEqual(report["text"], self.report["text"])
        self.assertEqual(report["typography"]["advance"], 11)
        self.assertEqual(report["typography"]["small_chinese_slots_replaced"], 3327)
        bank = BANKS[0]
        self.assertEqual(output[bank.offset:bank.end], self.output[bank.offset:bank.end])

    def test_chapter_cards_change_only_reviewed_cells(self):
        entries = table_entries(self.source, MISC_TABLE)
        palette = self.source[slice(*entries[45])]
        original_tiles = decompress(self.source[slice(*entries[44])])
        new_tiles = decompress(self.output[CHAPTER_RELOCATED:CHAPTER_RELOCATED + 0x8000])
        pointer = struct.unpack_from("<I", self.output, MISC_TABLE + 4 + 44 * 4)[0]
        self.assertEqual(pointer + MISC_TABLE, CHAPTER_RELOCATED)
        self.assertEqual(len(new_tiles), len(original_tiles))
        changed_cells = {0: [88], 1: [120, 136], 3: [136, 152], 6: [128]}
        for chapter in range(8):
            original = render(original_tiles, palette, self.source[slice(*entries[46 + chapter])])
            actual = render(new_tiles, palette, self.output[slice(*entries[46 + chapter])])
            self.assertEqual(actual.crop((0, 0, 240, 160)).tobytes(), self.chapters[chapter].tobytes())
            for left in changed_cells.get(chapter, []):
                original.paste(0, (left, 72, left + 16, 88))
                actual.paste(0, (left, 72, left + 16, 88))
            self.assertEqual(actual.tobytes(), original.tobytes())

    def test_welcome_allocation_prompt_and_main_title(self):
        original = table_entries(self.source, WELCOME_TABLE)
        replacement = table_entries(self.output, WELCOME_RELOCATED)
        self.assertEqual(len(decompress(self.source[slice(*original[0])])),
                         len(decompress(self.output[slice(*replacement[0])])))
        for index in (3, 4):
            self.assertEqual(self.source[slice(*original[index])], self.output[slice(*replacement[index])])
        title_entries = table_entries(self.source, TITLE_TABLE)
        end = title_entries[-1][1]
        self.assertEqual(self.source[TITLE_TABLE:end], self.output[TITLE_TABLE:end])
        self.assertEqual(apply_bps(self.source, self.patch), self.output)

    def test_welcome_version_label_matches_report_and_encoded_screen(self):
        self.assertEqual(self.report["version"], RELEASE_VERSION)
        entries = table_entries(self.output, WELCOME_RELOCATED)
        decoded = render(decompress(self.output[slice(*entries[0])]),
                         self.output[slice(*entries[1])],
                         decompress(self.output[slice(*entries[2])]))
        self.assertEqual(decoded.crop((0, 0, 240, 160)).tobytes(), self.welcome.tobytes())
        expected = Image.new("P", (240, 32), 0)
        expected.putpalette([255, 255, 255, 0, 0, 0] + [0] * 762)
        draw = ImageDraw.Draw(expected)
        draw.fontmode = "1"
        font = ImageFont.load_default()
        label = f"v{self.report['version']}"
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        self.assertLessEqual(right - left, 224)
        self.assertLessEqual(bottom - top, 8)
        draw.text(((240 - (right - left)) // 2 - left, 4 - top), label,
                  font=font, fill=1)
        expected = expected.convert("RGB")
        self.assertEqual(self.welcome.crop((0, 128, 240, 160)).tobytes(), expected.tobytes())
        self.assertEqual(expected.getextrema(), ((0, 255),) * 3)