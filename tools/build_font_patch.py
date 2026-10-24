#!/usr/bin/env python3
"""Build the conservative, font-only Traditional Chinese prototype."""

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA256 = "b3ed0f1c5c8d53a9537bee32c993b986f38ae57453c882499191f1dbafb31fb3"
ROM_SIZE = 0x2000000
FIRST_GLYPH = 0x300
GLYPH_COUNT = 0x1000


@dataclass(frozen=True)
class Bank:
    offset: int
    width: int
    rows: int
    font_size: int

    @property
    def stride(self) -> int:
        return self.rows * 2

    @property
    def end(self) -> int:
        return self.offset + GLYPH_COUNT * self.stride


BANKS = (Bank(0xCE39F8, 12, 11, 12), Bank(0xCF99F8, 10, 10, 10))
FONT_FILES = {
    12: "fusion-pixel-12px-monospaced-zh_hant.bdf",
    10: "mother3-traditional-full-cell-10px.bdf",
}


@dataclass(frozen=True)
class Character:
    index: int
    source: str
    target: str
    evidence: str


@dataclass(frozen=True)
class Glyph:
    width: int
    height: int
    x: int
    y: int
    rows: tuple[int, ...]


@dataclass(frozen=True)
class Font:
    size: int
    ascent: int
    glyphs: dict[int, Glyph]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_source(rom: bytes) -> None:
    if len(rom) != ROM_SIZE or sha256(rom) != SOURCE_SHA256:
        raise ValueError(
            "Wrong source ROM. Expected the unmodified Chinese 1.2 completion ROM "
            f"({ROM_SIZE} bytes, SHA-256 {SOURCE_SHA256}); "
            f"received {len(rom)} bytes, SHA-256 {sha256(rom)}."
        )


def read_charset(path: Path) -> list[Character]:
    result = []
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 4:
            raise ValueError(f"{path}:{line_number}: expected four tab-separated fields")
        index_text, source, target, evidence = fields
        index = int(index_text, 16)
        if index in seen or not FIRST_GLYPH <= index < GLYPH_COUNT:
            raise ValueError(f"{path}:{line_number}: duplicate or out-of-range glyph ID")
        if len(source) != 1 or len(target) != 1 or not evidence:
            raise ValueError(f"{path}:{line_number}: invalid character mapping")
        if not all("\u3400" <= char <= "\u9fff" for char in (source, target)):
            raise ValueError(f"{path}:{line_number}: expected a CJK character")
        seen.add(index)
        result.append(Character(index, source, target, evidence))
    if seen != set(range(FIRST_GLYPH, GLYPH_COUNT)):
        raise ValueError(f"{path}: incomplete character map")
    return sorted(result, key=lambda entry: entry.index)


def read_bdf(path: Path) -> Font:
    text = path.read_text(encoding="utf-8")
    header = text.split("STARTCHAR ", 1)[0].splitlines()
    properties = {line.split()[0]: line.split()[1:] for line in header if line.strip()}
    size = int(properties["PIXEL_SIZE"][0])
    ascent = int(properties["FONT_ASCENT"][0])
    glyphs = {}
    for entry in text.split("STARTCHAR ")[1:]:
        lines = entry.splitlines()
        encoding = int(next(line.split()[1] for line in lines if line.startswith("ENCODING ")))
        width, height, x, y = map(
            int, next(line.split()[1:] for line in lines if line.startswith("BBX "))
        )
        start = next(i for i, line in enumerate(lines) if line.strip() == "BITMAP") + 1
        end = lines.index("ENDCHAR")
        if width < 0 or height < 0 or end - start != height:
            raise ValueError(f"{path}: invalid bitmap dimensions for U+{encoding:04X}")
        row_bits = ((width + 7) // 8) * 8
        rows = tuple(int(line, 16) if line else 0 for line in lines[start:end])
        if any(row >= 1 << row_bits for row in rows):
            raise ValueError(f"{path}: bitmap row overflow for U+{encoding:04X}")
        if encoding < 0:
            continue
        if encoding in glyphs:
            raise ValueError(f"{path}: duplicate U+{encoding:04X}")
        glyphs[encoding] = Glyph(width, height, x, y, rows)
    if len(glyphs) != int(properties["CHARS"][0]):
        raise ValueError(f"{path}: inconsistent BDF character count")
    return Font(size, ascent, glyphs)


def encode_glyph(font: Font, char: str, bank: Bank) -> bytes:
    if font.size != bank.font_size:
        raise ValueError("Font pixel size does not match its ROM bank")
    if ord(char) not in font.glyphs:
        raise ValueError(f"Missing {font.size}px glyph: {char} (U+{ord(char):04X})")
    glyph = font.glyphs[ord(char)]
    row_bits = ((glyph.width + 7) // 8) * 8
    top = font.ascent - (font.size - bank.rows)
    y_offset = top - glyph.y - glyph.height
    output = bytearray(bank.stride)
    for y, row in enumerate(glyph.rows):
        for x in range(glyph.width):
            if not row & (1 << (row_bits - 1 - x)):
                continue
            target_x, target_y = glyph.x + x, y_offset + y
            if not (0 <= target_x < bank.width and 0 <= target_y < bank.rows):
                raise ValueError(f"Glyph {char} does not fit the {bank.width}x{bank.rows} bank")
            output[target_y * 2 + target_x // 8] |= 1 << (7 - target_x % 8)
    if not any(output):
        raise ValueError(f"Blank target glyph: {char}")
    return bytes(output)


def create_ips(records: list[tuple[int, bytes]]) -> bytes:
    patch = bytearray(b"PATCH")
    previous_end = 0
    for offset, data in sorted(records):
        if not 0 <= offset < 0x1000000 or offset == 0x454F46:
            raise ValueError("Offset cannot be represented by IPS")
        if not 0 < len(data) <= 0xFFFF or offset < previous_end:
            raise ValueError("Invalid or overlapping IPS record")
        patch.extend(offset.to_bytes(3, "big"))
        patch.extend(struct.pack(">H", len(data)))
        patch.extend(data)
        previous_end = offset + len(data)
    patch.extend(b"EOF")
    return bytes(patch)


def apply_ips(source: bytes, patch: bytes) -> bytes:
    """Strict reader for the non-RLE IPS records emitted by this builder."""
    if not patch.startswith(b"PATCH"):
        raise ValueError("Invalid IPS header")
    result = bytearray(source)
    position = 5
    previous_end = 0
    while patch[position:position + 3] != b"EOF":
        if position + 5 > len(patch):
            raise ValueError("Truncated IPS record")
        offset = int.from_bytes(patch[position:position + 3], "big")
        size = int.from_bytes(patch[position + 3:position + 5], "big")
        position += 5
        if not size or position + size > len(patch):
            raise ValueError("Truncated or unsupported RLE record")
        if offset < previous_end or offset + size > len(result):
            raise ValueError("IPS record overlaps or exceeds the ROM")
        result[offset:offset + size] = patch[position:position + size]
        position += size
        previous_end = offset + size
    if position + 3 != len(patch):
        raise ValueError("Unexpected IPS trailer")
    return bytes(result)


def build(rom: bytes) -> tuple[bytes, bytes, dict]:
    validate_source(rom)
    charset = read_charset(ROOT / "data/charset.tsv")
    font_dir = ROOT / "data/fonts"
    checksums = json.loads((font_dir / "sha256.json").read_text())
    fonts = {}
    for name, expected_hash in checksums.items():
        if sha256((font_dir / name).read_bytes()) != expected_hash:
            raise ValueError(f"Font checksum mismatch: {name}")
    for bank in BANKS:
        name = FONT_FILES[bank.font_size]
        path = font_dir / name
        if name not in checksums:
            raise ValueError(f"Missing font checksum: {name}")
        fonts[bank.font_size] = read_bdf(path)
    supplement_name = "mother3-traditional-supplement-10px.bdf"
    if supplement_name not in checksums:
        raise ValueError("Missing supplemental font checksum")
    supplement = read_bdf(font_dir / supplement_name)
    if (supplement.size, supplement.ascent) != (fonts[10].size, fonts[10].ascent):
        raise ValueError("Supplemental font metrics do not match")
    if not supplement.glyphs.keys() <= fonts[10].glyphs.keys():
        raise ValueError("Full-cell font is missing supplemental characters")
    output = bytearray(rom)
    records = []
    converted = [entry for entry in charset if entry.source != entry.target]
    for bank in BANKS:
        for entry in converted:
            glyph = encode_glyph(fonts[bank.font_size], entry.target, bank)
            offset = bank.offset + entry.index * bank.stride
            output[offset:offset + bank.stride] = glyph
            records.append((offset, glyph))
    result = bytes(output)
    patch = create_ips(records)
    if apply_ips(rom, patch) != result:
        raise ValueError("IPS round-trip verification failed")
    cursor = 0
    for bank in BANKS:
        if rom[cursor:bank.offset] != result[cursor:bank.offset]:
            raise ValueError("Bytes outside the permitted font banks changed")
        cursor = bank.end
    if rom[cursor:] != result[cursor:] or len(result) != ROM_SIZE:
        raise ValueError("ROM length or non-font data changed")
    changed_bytes = sum(a != b for bank in BANKS for a, b in zip(
        rom[bank.offset:bank.end], result[bank.offset:bank.end]
    ))
    report = {
        "status": "font-only prototype; not a complete contextual translation",
        "source_sha256": sha256(rom),
        "output_sha256": sha256(result),
        "ips_sha256": sha256(patch),
        "charset_sha256": sha256((ROOT / "data/charset.tsv").read_bytes()),
        "rom_bytes": len(result),
        "converted_character_slots": len(converted),
        "font_records_replaced": len(records),
        "changed_bytes": changed_bytes,
        "conversion": "OpenCC 1.4.0, per-character s2t defaults, no regional rewording",
        "font": "Fusion Pixel Font 2026.10.16, monospaced zh-Hant, SIL OFL 1.1",
        "small_font": (
            "M3 Traditional Full Cell: stroke-preserving 9x9 to 10x10 adaptation, "
            "top-aligned with the original small font"
        ),
        "supplemental_10px_glyphs": [chr(cp) for cp in sorted(supplement.glyphs)],
        "font_checksums": checksums,
        "mapping_evidence": dict(Counter(entry.evidence for entry in charset)),
        "font_banks": [
            {"start": hex(bank.offset), "end_exclusive": hex(bank.end),
             "glyph_bytes": bank.stride, "width": bank.width, "height": bank.rows}
            for bank in BANKS
        ],
        "verified": {
            "source_hash": True,
            "ips_round_trip": True,
            "all_non_font_bytes_unchanged": True,
            "all_target_glyphs_fit_without_clipping": True,
            "full_game_playthrough": False,
            "save_file_compatibility_playtested": False,
        },
        "limitations": [
            "One-to-many variants use a single default glyph, not phrase context.",
            "Text baked into graphics and other fonts is unchanged.",
            "OCR-derived character mapping may still contain identification errors.",
            "Save format and code are unchanged, but existing saves were not playtested.",
        ],
    }
    return result, patch, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rom", type=Path, help="Exact Chinese 1.2 completion ROM")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build")
    args = parser.parse_args()
    names = ("mother3-zh-Hant-font-only.gba", "mother3-zh-Hant-font-only.ips", "report.json")
    destinations = [args.output_dir / name for name in names]
    try:
        for path in destinations:
            if path.resolve() == args.rom.resolve() or path.exists():
                raise ValueError(f"Refusing to overwrite {path}; choose a fresh output directory")
        output, patch, report = build(args.rom.read_bytes())
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for path, contents in zip(destinations, (
            output, patch, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode()
        )):
            with path.open("xb") as handle:
                handle.write(contents)
        print(f"Converted {report['converted_character_slots']} slots in each of two fonts.")
        print(f"Changed {report['changed_bytes']} bytes; all non-font bytes are unchanged.")
        for path in destinations:
            print(path)
    except (OSError, ValueError, KeyError, StopIteration) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
