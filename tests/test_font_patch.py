from pathlib import Path
import tempfile
import unittest

from tools.build_font_patch import (
    BANKS, FIRST_GLYPH, FONT_FILES, GLYPH_COUNT, ROOT, Bank, Font, Glyph,
    apply_ips, build, create_ips, encode_glyph, read_bdf, read_charset, validate_source,
)
from tools.expand_small_font import expand_grid, topology


def ink_bounds(data):
    points = [
        (x, y) for y in range(len(data) // 2) for x in range(16)
        if data[y * 2 + x // 8] & (1 << (7 - x % 8))
    ]
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


class ExpansionTests(unittest.TestCase):
    def test_box_keeps_single_pixel_strokes(self):
        grid = [[x in (0, 8) or y in (0, 8) for x in range(9)] for y in range(9)]
        result = expand_grid(grid)
        self.assertEqual(
            result.grid, [[x in (0, 9) or y in (0, 9) for x in range(10)] for y in range(10)]
        )

    def test_diagonal_keeps_connectivity_and_original_pixels(self):
        grid = [[x == y for x in range(9)] for y in range(9)]
        result = expand_grid(grid)
        self.assertEqual(topology(grid), topology(result.grid))
        recovered = [
            row[:result.x] + row[result.x + 1:]
            for y, row in enumerate(result.grid) if y != result.y
        ]
        self.assertEqual(grid, recovered)

    def test_narrow_glyph_expands_instead_of_shifting_right(self):
        grid = [
            [x >= 1 and (x in (1, 8) or y in (0, 8)) for x in range(9)]
            for y in range(9)
        ]
        result = expand_grid(grid)
        self.assertGreater(result.x, 1)
        self.assertEqual(
            result.grid,
            [[x >= 1 and (x in (1, 9) or y in (0, 9)) for x in range(10)] for y in range(10)],
        )

    def test_invalid_or_blank_source_rejected(self):
        for grid in ([], [[False] * 9 for _ in range(9)], [[True] * 10 for _ in range(9)]):
            with self.subTest(grid=grid), self.assertRaisesRegex(ValueError, "9x9"):
                expand_grid(grid)

    def test_all_961_glyphs_have_full_height_and_preserve_stroke_topology(self):
        base = read_bdf(ROOT / "data/fonts/fusion-pixel-10px-monospaced-zh_hant.bdf")
        base.glyphs.update(
            read_bdf(ROOT / "data/fonts/mother3-traditional-supplement-10px.bdf").glyphs
        )
        expanded = read_bdf(ROOT / "data/fonts" / FONT_FILES[10])
        targets = {
            ord(entry.target) for entry in read_charset(ROOT / "data/charset.tsv")
            if entry.source != entry.target
        }
        self.assertEqual(set(expanded.glyphs), targets)
        self.assertEqual(len(targets), 961)
        for cp in targets:
            before = encode_glyph(base, chr(cp), BANKS[1])
            after = encode_glyph(expanded, chr(cp), BANKS[1])
            old_grid = [
                [bool(before[y * 2 + x // 8] & (1 << (7 - x % 8))) for x in range(9)]
                for y in range(1, 10)
            ]
            new_grid = [
                [bool(after[y * 2 + x // 8] & (1 << (7 - x % 8))) for x in range(10)]
                for y in range(10)
            ]
            with self.subTest(character=chr(cp)):
                self.assertEqual(topology(old_grid), topology(new_grid))
                x, y, width, height = ink_bounds(after)
                old_x, _, old_width, _ = ink_bounds(before)
                self.assertEqual((x, y, width, height), (old_x, 0, old_width + 1, 10))


class SourceTests(unittest.TestCase):
    def test_wrong_source_rejected(self):
        with self.assertRaisesRegex(ValueError, "Wrong source ROM"):
            validate_source(b"not a ROM")

    def test_complete_charset_and_known_corrections(self):
        mapping = {entry.index: entry for entry in read_charset(ROOT / "data/charset.tsv")}
        self.assertEqual(set(mapping), set(range(FIRST_GLYPH, GLYPH_COUNT)))
        for index, source, target in (
            (0x300, "\u6b22", "\u6b61"),
            (0x309, "\u65e0", "\u7121"),
            (0x412, "\u5077", "\u5077"),
            (0x6D6, "\u8a89", "\u8b7d"),
            (0x7D2, "\u9cde", "\u9c57"),
            (0xB88, "\u75e3", "\u75e3"),
        ):
            self.assertEqual((mapping[index].source, mapping[index].target), (source, target))

    def test_duplicate_mapping_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "charset.tsv"
            row = "0300\t\u6b22\t\u6b61\ttest\n"
            path.write_text(row * 2)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                read_charset(path)

    def test_incomplete_mapping_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "charset.tsv"
            path.write_text("0300\t\u6b22\t\u6b61\ttest\n")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                read_charset(path)


class GlyphTests(unittest.TestCase):
    def test_msb_first_two_byte_rows(self):
        glyph = Glyph(10, 1, 0, 0, (0x8040,))
        font = Font(10, 1, {ord("A"): glyph})
        encoded = encode_glyph(font, "A", Bank(0, 10, 10, 10))
        self.assertEqual(encoded, b"\x80\x40" + bytes(18))

    def test_main_font_removes_only_unused_top_row(self):
        glyph = Glyph(1, 11, 0, -2, (0x80,) * 11)
        font = Font(12, 10, {ord("A"): glyph})
        self.assertEqual(encode_glyph(font, "A", BANKS[0]), b"\x80\x00" * 11)

    def test_missing_glyph_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Missing"):
            encode_glyph(Font(10, 9, {}), "A", BANKS[1])

    def test_clipping_is_an_error(self):
        glyph = Glyph(1, 1, 10, 0, (0x80,))
        with self.assertRaisesRegex(ValueError, "does not fit"):
            encode_glyph(Font(10, 9, {ord("A"): glyph}), "A", BANKS[1])

    def test_blank_glyph_is_an_error(self):
        glyph = Glyph(1, 1, 0, 0, (0,))
        with self.assertRaisesRegex(ValueError, "Blank"):
            encode_glyph(Font(10, 9, {ord("A"): glyph}), "A", BANKS[1])

    def test_supplement_is_complete_and_fits(self):
        font = read_bdf(ROOT / "data/fonts/mother3-traditional-supplement-10px.bdf")
        self.assertEqual(set(font.glyphs), {0x53C4, 0x78E3})
        for cp in font.glyphs:
            self.assertEqual(len(encode_glyph(font, chr(cp), BANKS[1])), 20)


class IpsTests(unittest.TestCase):
    def test_round_trip(self):
        source = bytes(32)
        patch = create_ips([(3, b"abc"), (20, b"xyz")])
        expected = bytearray(source)
        expected[3:6] = b"abc"
        expected[20:23] = b"xyz"
        self.assertEqual(apply_ips(source, patch), expected)

    def test_overlapping_records_rejected(self):
        with self.assertRaisesRegex(ValueError, "overlapping"):
            create_ips([(0, b"abc"), (1, b"xyz")])

    def test_reserved_offset_rejected(self):
        with self.assertRaisesRegex(ValueError, "represented"):
            create_ips([(0x454F46, b"a")])

    def test_truncation_rejected(self):
        patch = create_ips([(2, b"abc")])
        for broken in (patch[:-1], patch[:-5], b"PATCH"):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                apply_ips(bytes(32), broken)

    def test_out_of_bounds_and_extra_trailer_rejected(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            apply_ips(bytes(8), create_ips([(7, b"abc")]))
        with self.assertRaisesRegex(ValueError, "trailer"):
            apply_ips(bytes(8), b"PATCHEOFextra")


SOURCE_ROM = ROOT / "assets/Mother 3 (J)[T+Chn1.2]\u6c49\u5316\u8865\u5b8c.gba"


@unittest.skipUnless(SOURCE_ROM.exists(), "Local source ROM is not supplied")
class RealRomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE_ROM.read_bytes()
        cls.output, cls.patch, cls.report = build(cls.source)

    def test_patch_reproduces_rom(self):
        self.assertEqual(apply_ips(self.source, self.patch), self.output)
        self.assertEqual(len(self.output), 32 * 1024 * 1024)
        self.assertNotEqual(self.source, self.output)

    def test_every_non_font_byte_unchanged(self):
        self.assertEqual(self.source[:BANKS[0].offset], self.output[:BANKS[0].offset])
        self.assertEqual(self.source[BANKS[-1].end:], self.output[BANKS[-1].end:])

    def test_only_mapped_glyphs_changed(self):
        changed = {
            entry.index for entry in read_charset(ROOT / "data/charset.tsv")
            if entry.source != entry.target
        }
        self.assertEqual(len(changed), 1155)
        for bank in BANKS:
            for index in range(GLYPH_COUNT):
                offset = bank.offset + index * bank.stride
                before = self.source[offset:offset + bank.stride]
                after = self.output[offset:offset + bank.stride]
                if index not in changed:
                    self.assertEqual(before, after, f"Unexpected edit at glyph {index:04X}")
                else:
                    self.assertTrue(any(after), f"Blank glyph {index:04X}")

    def test_small_replacements_match_original_size_and_top_alignment(self):
        mapping = {entry.source: entry for entry in read_charset(ROOT / "data/charset.tsv")}
        bank = BANKS[1]
        for source in "\u51fb\u70b9\u7ecf\u9a8c\u56fe":
            entry = mapping[source]
            offset = bank.offset + entry.index * bank.stride
            with self.subTest(target=entry.target):
                self.assertEqual(
                    ink_bounds(self.output[offset:offset + bank.stride]), (0, 0, 10, 10)
                )

    def test_known_traditional_glyph_matches_font(self):
        for bank in BANKS:
            font = read_bdf(
                ROOT / "data/fonts" / FONT_FILES[bank.font_size]
            )
            offset = bank.offset + 0x300 * bank.stride
            self.assertEqual(
                self.output[offset:offset + bank.stride],
                encode_glyph(font, "\u6b61", bank),
            )


if __name__ == "__main__":
    unittest.main()
