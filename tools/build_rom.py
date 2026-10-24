"""Build the reviewed Traditional Chinese text and screen update locally."""

import argparse
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

from PIL import Image

from tools.build_font_patch import (
    BANKS, ROOT, build as build_fonts, encode_glyph, read_bdf, sha256, validate_source,
)
from tools.build_screen_patch import (
    CHAPTER_TITLES, RELEASE_VERSION, chapter_records, padding_record, welcome_records,
)
from tools.build_text_patch import corrected_charset, text_records

C2_PRESERVED_CHARACTERS = frozenset("\u562c")
C2_SPACING_ADDRESS = 0x1FA0000


def c2_spacing_records(rom):
    expected = {
        0xA136: bytes.fromhex("0a20"),
        0x49CB4: bytes.fromhex("0a20"),
        0x88E4C: bytes.fromhex("126802600968416001207047"),
    }
    for offset, original in expected.items():
        if rom[offset:offset + len(original)] != original:
            raise ValueError(f"Unexpected font instructions at {offset:#x}")
    compiler = Path("/opt/devkitpro/devkitARM/bin")
    with tempfile.TemporaryDirectory(prefix="mother3-spacing-") as directory:
        object_path = Path(directory) / "spacing.o"
        binary_path = Path(directory) / "spacing.bin"
        subprocess.run([str(compiler / "arm-none-eabi-as"), "-mthumb",
                        "-o", str(object_path), str(ROOT / "tools/font_spacing.s")], check=True)
        subprocess.run([str(compiler / "arm-none-eabi-objcopy"), "-O", "binary",
                        str(object_path), str(binary_path)], check=True)
        routine = binary_path.read_bytes()
    hook = bytes.fromhex("004b1847") + struct.pack("<I", 0x08000001 + C2_SPACING_ADDRESS)
    hook += bytes.fromhex("c046c046")
    return [(0xA136, bytes.fromhex("0b20")), (0x49CB4, bytes.fromhex("0b20")),
            (0x88E4C, hook), padding_record(rom, C2_SPACING_ADDRESS, routine)]


def c2_font_records(text_report):
    font_path = ROOT / "data/fonts/fusion-pixel-10px-monospaced-zh_hant.bdf"
    checksums = json.loads((ROOT / "data/fonts/sha256.json").read_text())
    if sha256(font_path.read_bytes()) != checksums[font_path.name]:
        raise ValueError("C2 font checksum mismatch")
    font = read_bdf(font_path)
    supplement_path = ROOT / "data/fonts/mother3-traditional-supplement-10px.bdf"
    if sha256(supplement_path.read_bytes()) != checksums[supplement_path.name]:
        raise ValueError("C2 supplement checksum mismatch")
    supplement = read_bdf(supplement_path)
    if (supplement.size, supplement.ascent) != (font.size, font.ascent):
        raise ValueError("C2 supplement metrics differ")
    if font.glyphs.keys() & supplement.glyphs.keys():
        raise ValueError("C2 supplement replaces existing glyphs")
    font.glyphs.update(supplement.glyphs)
    targets = {entry.index: entry.target for entry in corrected_charset()}
    targets.update({int(slot, 16): character
                    for character, slot in text_report["glyph_slots"].items()})
    bank = BANKS[1]
    return [(bank.offset + slot * bank.stride, encode_glyph(font, character, bank))
            for slot, character in sorted(targets.items())
            if character not in C2_PRESERVED_CHARACTERS]


def variable_integer(value):
    if value < 0:
        raise ValueError("Negative BPS integer")
    output = bytearray()
    while True:
        part = value & 127
        value >>= 7
        if not value:
            output.append(part | 128)
            return bytes(output)
        output.append(part)
        value -= 1


def create_bps(source, target):
    if len(source) != len(target):
        raise ValueError("This builder preserves ROM size")
    patch = bytearray(b"BPS1" + variable_integer(len(source))
                      + variable_integer(len(target)) + variable_integer(0))
    start = 0
    while start < len(source):
        changed = source[start] != target[start]
        end = start + 1
        while end < len(source) and (source[end] != target[end]) == changed:
            end += 1
        patch.extend(variable_integer(((end - start - 1) << 2) | int(changed)))
        if changed:
            patch.extend(target[start:end])
        start = end
    patch.extend(struct.pack("<II", zlib.crc32(source), zlib.crc32(target)))
    patch.extend(struct.pack("<I", zlib.crc32(patch)))
    return bytes(patch)


def apply_bps(source, patch):
    if len(patch) < 16 or patch[:4] != b"BPS1":
        raise ValueError("Invalid BPS patch")
    source_crc, target_crc, patch_crc = struct.unpack_from("<III", patch, len(patch) - 12)
    if zlib.crc32(source) != source_crc or zlib.crc32(patch[:-4]) != patch_crc:
        raise ValueError("BPS source or patch checksum mismatch")
    position = 4

    def number():
        nonlocal position
        value = 0
        shift = 1
        while position < len(patch) - 12:
            part = patch[position]
            position += 1
            value += (part & 127) * shift
            if part & 128:
                return value
            shift <<= 7
            value += shift
        raise ValueError("Truncated BPS integer")

    if number() != len(source):
        raise ValueError("BPS source size mismatch")
    target_size = number()
    metadata_size = number()
    position += metadata_size
    output = bytearray()
    while len(output) < target_size:
        action = number()
        length = (action >> 2) + 1
        if len(output) + length > target_size:
            raise ValueError("BPS action exceeds target")
        if action & 3 == 0:
            chunk = source[len(output):len(output) + length]
        elif action & 3 == 1:
            if position + length > len(patch) - 12:
                raise ValueError("Truncated BPS target data")
            chunk = patch[position:position + length]
            position += length
        else:
            raise ValueError("Unsupported BPS copy action")
        if len(chunk) != length:
            raise ValueError("Truncated BPS source data")
        output.extend(chunk)
    if position != len(patch) - 12 or zlib.crc32(output) != target_crc:
        raise ValueError("BPS target checksum or trailer mismatch")
    return bytes(output)


def build(source, logo_path, chapter_font, *, c2=False):
    validate_source(source)
    font_rom, _, font_report = build_fonts(source)
    output = bytearray(font_rom)
    text_changes, text_report = text_records(source, font_rom)
    welcome_changes, welcome_preview = welcome_records(source, logo_path)
    chapter_changes, chapter_previews = chapter_records(source, chapter_font)
    changes = text_changes + welcome_changes + chapter_changes
    for offset, data in changes:
        if not 0 <= offset < offset + len(data) <= len(source):
            raise ValueError("Patch exceeds ROM bounds")
        output[offset:offset + len(data)] = data
    if (output[:0x5A61C] != source[:0x5A61C]
            or output[0x5A620:BANKS[0].offset] != source[0x5A620:BANKS[0].offset]):
        raise AssertionError("Game code changed beyond the welcome-resource pointer")
    typography_report = None
    if c2:
        glyph_changes = c2_font_records(text_report)
        spacing_changes = c2_spacing_records(output)
        for offset, data in glyph_changes + spacing_changes:
            output[offset:offset + len(data)] = data
        typography_report = {
            "selection": "C2", "font": "Fusion Pixel 10px native zh-Hant",
            "ink_size": [9, 9], "advance": 11,
            "small_chinese_slots_replaced": len(glyph_changes),
            "supplemental_characters": ["\u53c4", "\u78e3"],
            "preserved_original_characters": sorted(C2_PRESERVED_CHARACTERS),
            "large_font_unchanged": True,
            "spacing_records": [{"offset": hex(offset), "bytes": len(data)}
                                for offset, data in spacing_changes],
            "spacing_assembly_sha256": sha256((ROOT / "tools/font_spacing.s").read_bytes()),
            "full_game_layout_verified": False,
        }
    patch = create_bps(source, output)
    if apply_bps(source, patch) != output:
        raise AssertionError("BPS round-trip mismatch")
    report = {
        "version": RELEASE_VERSION,
        "source_sha256": sha256(source), "output_sha256": sha256(output),
        "bps_sha256": sha256(patch), "rom_size": len(output),
        "logo_sha256": sha256(logo_path.read_bytes()),
        "chapter_font_sha256": sha256(chapter_font.read_bytes()),
        "font_base_sha256": font_report["output_sha256"],
        "text": text_report,
        "chapter_titles": list(CHAPTER_TITLES),
        "chapter_number_lines_preserved": True,
        "main_title_and_credits_preserved": True,
        "welcome_controls_and_timing_preserved": True,
        "bps_round_trip": True,
        "device_deployed": False,
        "user_saves_modified": False,
        "time_skip_card": "pending",
    }
    if typography_report is not None:
        report["typography"] = typography_report
    return bytes(output), patch, report, welcome_preview, chapter_previews


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rom", type=Path)
    parser.add_argument("--logo", type=Path, default=ROOT / "assets/yuleshow-chop.png")
    parser.add_argument("--chapter-font", type=Path,
                        default=Path("/System/Library/Fonts/STHeiti Medium.ttc"))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build/reviewed")
    parser.add_argument("--c2", action="store_true",
                        help="Use native small Chinese glyphs and 11px small-font advance")
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("Refusing to overwrite an existing output directory")
    rom, patch, report, welcome, chapters = build(
        args.rom.read_bytes(), args.logo, args.chapter_font, c2=args.c2)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "mother3-zh-Hant-reviewed.gba").write_bytes(rom)
    (args.output_dir / "mother3-zh-Hant-reviewed.bps").write_bytes(patch)
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    welcome.save(args.output_dir / "welcome.png")
    sheet = Image.new("RGB", (960, 320))
    for index, image in enumerate(chapters):
        image.save(args.output_dir / f"chapter-{index + 1}.png")
        sheet.paste(image, (index % 4 * 240, index // 4 * 160))
    sheet.save(args.output_dir / "chapters.png")
    print(json.dumps({key: report[key] for key in ("source_sha256", "output_sha256", "bps_sha256")}, indent=2))


if __name__ == "__main__":
    main()