"""Build context-specific corrections on the verified Traditional font build."""

from bisect import bisect_right
import re
import struct

from tools.build_font_patch import BANKS, Character, ROOT, encode_glyph, read_bdf, read_charset, sha256
from tools.build_screen_patch import padding_record, table_entries
from tools.expand_small_font import expand_grid


SCRIPT_TABLE = 0x136A6F4
TEXT_TABLE = 0xD1EE78
ENDING_BANK = 462
ENDING_START = 0x13D4DF4
ENDING_DATA = 0x13D4E4C
ENDING_END = 0x13D716C
ENDING_RELOCATED = 0x1FB0000
ENDING_INSERTIONS = ((0x13D5316, 0x35), (0x13D53BA, 0xFF01))
SPECIAL_FONT = 0xD0D9F8
SPECIAL_COUNT = 0x182
SPECIAL_SHA256 = "2311b6f42b0370822876a0135e6b6ccdd0e27f6e69f9d7d6bfe77d91ea297acc"
SPECIAL_TARGETS = {
    0x4B: "嗎", 0x51: "這", 0x52: "邊", 0x5B: "謝", 0x5D: "顏",
    0x5F: "過", 0x60: "來", 0x66: "麥", 0x6C: "鳥", 0x6D: "兒",
    0x77: "裡", 0x8B: "後", 0x8C: "會", 0x93: "鹹", 0x94: "鮭",
    0x95: "魚", 0x98: "臍", 0x9D: "釣", 0x9F: "靜", 0xA2: "隻",
    0xA4: "飛", 0xA6: "愛", 0xAA: "麼", 0xAF: "個", 0xB2: "覺", 0xB5: "沒",
    0xBC: "溫", 0xCE: "輕", 0xCF: "鬆", 0xD0: "頭", 0xD1: "頂", 0xD3: "帶",
    0xDC: "們", 0xE4: "漸", 0xE5: "變", 0xEB: "發", 0xED: "東",
    0xEF: "閃", 0xF3: "還", 0xF4: "給", 0xF5: "見", 0xF8: "蘆",
    0xFA: "揮", 0xFB: "潛", 0x102: "層", 0x103: "禮", 0x107: "雞",
    0x10D: "樣", 0x110: "幫", 0x113: "鬧", 0x118: "諾", 0x11B: "當",
    0x11E: "簡", 0x11F: "單", 0x120: "圖", 0x121: "搖", 0x122: "馬",
    0x126: "時", 0x128: "現", 0x12A: "著", 0x12E: "乾", 0x130: "豬",
    0x131: "飯", 0x132: "壞", 0x133: "髒", 0x136: "點", 0x13E: "買",
    0x13F: "賣", 0x140: "開", 0x141: "難", 0x143: "對", 0x144: "剛",
    0x14C: "減", 0x14D: "窮", 0x155: "噴", 0x157: "臉", 0x15B: "進",
    0x15C: "滿", 0x164: "皺", 0x166: "讓", 0x16A: "種", 0x16B: "聲", 0x16D: "說",
    0x171: "間", 0x179: "隨", 0x181: "歡",
}
ARGUMENTS = {
    0xFFFF: 0, 0xFF01: 0, 0xFF02: 0, 0xFF03: 1, 0xFF04: 1,
    0xFF05: 1, 0xFF06: 1, 0xFF07: 0, 0xFF08: 1, 0xFF09: 0,
    0xFF0A: 0, 0xFF0B: 0, 0xFF0C: 1, 0xFF10: 0, 0xFF11: 0,
    0xFF12: 0, 0xFF13: 0, 0xFF14: 0, 0xFF15: 0, 0xFF16: 0,
    0xFF19: 0, 0xFF20: 0, 0xFF21: 1, 0xFF22: 1, 0xFF23: 1,
    0xFF24: 1, 0xFF25: 1, 0xFF26: 2, 0xFF30: 0, 0xFF31: 0,
    0xFF32: 0, 0xFF33: 0, 0xFF34: 0, 0xFF35: 0, 0xFF36: 0,
    0xFF37: 0, 0xFF42: 1, 0xFF44: 0, 0xFF45: 0, 0xFF46: 0,
    0xFF47: 0, 0xFF48: 0, 0xFF80: 1, 0xFF81: 0, 0xFF82: 0,
    0xFF83: 0, 0xFF84: 0, 0xFFA0: 0, 0xFFA1: 0, 0xFFA2: 0,
    0xFFA3: 0, 0xFFE0: 0, 0xFFE1: 1,
}
PHRASES = (
    ("\u5173\u7cfb", "\u95dc\u4fc2"), ("\u8054\u7cfb", "\u806f\u7e6b"),
    ("\u5bb6\u4f19", "\u50a2\u4f19"), ("\u8089\u5e72", "\u8089\u4e7e"),
    ("\u5e72\u65f1", "\u4e7e\u65f1"), ("\u5934\u53d1", "\u982d\u9aea"),
    ("\u5047\u53d1", "\u5047\u9aee"), ("\u63a7\u5236\u6746", "\u63a7\u5236\u687f"),
    ("\u8f7b\u677e", "\u8f15\u9b06"), ("\u9762\u5305", "\u9eb5\u5305"),
    ("\u82cf\u9192", "\u7526\u9192"),
    ("\u5e72\u71e5", "\u4e7e\u71e5"),
    ("烘干", "烘乾"), ("干净", "乾淨"), ("饼干", "餅乾"),
    ("干杯", "乾杯"), ("干脆", "乾脆"), ("干涸", "乾涸"),
    ("干冰", "乾冰"), ("鱼干", "魚乾"), ("衣服干", "衣服乾"),
    ("长发", "長髪"), ("发型", "髪型"), ("卷发", "捲髪"),
    ("洗发", "洗髪"), ("理发", "理髪"),
    ("粘土", "黏土"), ("面粉", "麵粉"),
    ("放松", "放鬆"),
    ("潜", "潛"), ("大伙", "大夥"),
    ("鸟", "鳥"), ("着", "著"),
)
CLASSIFIER_OFFSETS = (
    0xD24BA8, 0x13709D0, 0x13709FC, 0x138EEA0, 0x1392356,
    0x139C1DE, 0x139DA96, 0x13AE288, 0x13AE29A, 0x13AE2AC,
    0x13B1DD0, 0x13C23A4, 0x13C23DA, 0x13C82F4, 0x13C86E2,
    0x13C86EE, 0x13C8702, 0x13C873C, 0x13C87C0, 0x13C8826,
    0x13CDC24, 0x13CDC8E, 0x13CF2FE, 0x13D0C04, 0x13DE610,
    0x13EBFFA, 0x13EC03C, 0x13EC1E8, 0x13EEA9E,
)


def corrected_charset():
    return [Character(entry.index, "\u574f", "\u58de", "bitmap-and-dialogue-reviewed")
            if entry.index == 0x3B8 else entry
            for entry in read_charset(ROOT / "data/charset.tsv")]


def character_runs(rom, start, end):
    position = start
    run = start
    while position + 2 <= end:
        value = struct.unpack_from("<H", rom, position)[0]
        if value >= 0xFF00:
            if run < position:
                yield run, position
            if value == 0xFFFF:
                return
            if value not in ARGUMENTS:
                raise ValueError(f"Unknown text control {value:#x} at {position:#x}")
            position += 2 * (ARGUMENTS[value] + 1)
            if position > end:
                raise ValueError("Text control exceeds string boundary")
            run = position
        else:
            if value >= 0x1000:
                raise ValueError(f"Invalid glyph {value:#x} at {position:#x}")
            position += 2
    if run < position:
        yield run, position


def text_runs(rom):
    runs = set()
    count = struct.unpack_from("<I", rom, SCRIPT_TABLE)[0]
    if count != 2002:
        raise ValueError("Unexpected script bank count")
    offsets = struct.unpack_from(f"<{count + 1}I", rom, SCRIPT_TABLE + 4)
    script_end = SCRIPT_TABLE + offsets[-1]
    for index in range(0, count, 2):
        if not offsets[index] or not offsets[index + 1]:
            continue
        pointers = SCRIPT_TABLE + offsets[index]
        data = SCRIPT_TABLE + offsets[index + 1]
        if pointers == data:
            continue
        bank_end = 0x1FE0000 if pointers >= 0x1F86008 else script_end
        if not pointers < data <= bank_end:
            raise ValueError("Invalid script pointer-table extent")
        for position in range(pointers, data, 2):
            relative = struct.unpack_from("<H", rom, position)[0]
            start = data + relative
            if not data <= start < bank_end:
                raise ValueError(f"Script string pointer exceeds bank at {position:#x}")
            runs.update(character_runs(rom, start, min(data + 0x10000, bank_end)))
    entries = table_entries(rom, TEXT_TABLE)
    for pointer_index, data_index in ((0, 1), (3, 4), (9, 10), (14, 15)):
        data, end = entries[data_index]
        for position in range(*entries[pointer_index], 2):
            start = data + struct.unpack_from("<H", rom, position)[0]
            if not data <= start < end:
                raise ValueError("Menu string pointer exceeds data table")
            runs.update(character_runs(rom, start, end))
    for index in (2, 5, 6, 7, 8, 11, 12, 13):
        start, end = entries[index]
        width, count = struct.unpack_from("<HH", rom, start)
        if start + 4 + width * count * 2 > end:
            raise ValueError("Fixed string table exceeds resource")
        for string_index in range(count):
            position = start + 4 + string_index * width * 2
            runs.update(character_runs(rom, position, position + width * 2))
    return sorted(runs)


def phrase_matches(data, phrase, charset):
    choices = []
    for character in phrase:
        indices = sorted({entry.index for entry in charset if entry.source == character})
        if not indices:
            raise ValueError(f"No source glyph for {character!r}")
        choices.append(b"(?:" + b"|".join(
            re.escape(struct.pack("<H", index)) for index in indices
        ) + b")")
    pattern = re.compile(b"(?=(" + b"".join(choices) + b"))")
    return [match.start() for match in pattern.finditer(data) if match.start() % 2 == 0]


def phrase_locations(rom, phrase, charset, runs):
    starts = [start for start, _ in runs]
    locations = []
    for offset in phrase_matches(rom, phrase, charset):
        index = bisect_right(starts, offset) - 1
        if index >= 0 and offset + len(phrase) * 2 <= runs[index][1]:
            locations.append(offset)
    return locations


def target_glyphs(character, fonts):
    main = encode_glyph(fonts[12], character, BANKS[0])
    if ord(character) in fonts["full"].glyphs:
        small = encode_glyph(fonts["full"], character, BANKS[1])
    else:
        raw = encode_glyph(fonts[10], character, BANKS[1])
        if any(raw[:2]):
            raise ValueError("Unexpected small-font baseline")
        grid = [[bool(raw[row * 2 + column // 8] & (128 >> (column % 8)))
                 for column in range(9)] for row in range(1, 10)]
        expanded = expand_grid(grid).grid
        small = b"".join(sum(int(ink) << (15 - column) for column, ink in enumerate(row))
                         .to_bytes(2, "big") for row in expanded)
    return main, small


def text_records(source, font_rom):
    charset = corrected_charset()
    mapping = {entry.index: entry for entry in charset}
    runs = text_runs(source)
    used = {value for start, end in runs
            for (value,) in struct.iter_unpack("<H", source[start:end])}
    free = [entry.index for entry in charset if entry.index not in used]
    directory = ROOT / "data/fonts"
    fonts = {
        12: read_bdf(directory / "fusion-pixel-12px-monospaced-zh_hant.bdf"),
        10: read_bdf(directory / "fusion-pixel-10px-monospaced-zh_hant.bdf"),
        "full": read_bdf(directory / "mother3-traditional-full-cell-10px.bdf"),
    }
    allocated = {}
    records = []
    output = bytearray(font_rom)

    def write(offset, data):
        output[offset:offset + len(data)] = data
        records.append((offset, data))

    if sha256(source[SPECIAL_FONT:SPECIAL_FONT + SPECIAL_COUNT * 32]) != SPECIAL_SHA256:
        raise ValueError("Special dialogue font differs from the inspected bank")
    for slot, character in SPECIAL_TARGETS.items():
        bitmap = encode_glyph(fonts[12], character, BANKS[0])
        tiled = bitmap[::2] + bytes(5) + bitmap[1::2] + bytes(5)
        write(SPECIAL_FONT + slot * 32, tiled)

    def glyph_slot(character):
        if character in allocated:
            return allocated[character]
        candidates = [entry.index for entry in charset if entry.target == character]
        if candidates:
            return min(candidates)
        if not free:
            raise ValueError("No unused glyph slots remain")
        slot = free.pop()
        allocated[character] = slot
        for bank, bitmap in zip(BANKS, target_glyphs(character, fonts)):
            write(bank.offset + bank.stride * slot, bitmap)
        return slot

    for bank, bitmap in zip(BANKS, target_glyphs("\u58de", fonts)):
        write(bank.offset + bank.stride * 0x3B8, bitmap)
    report = []
    for before, after in PHRASES:
        locations = phrase_locations(source, before, charset, runs)
        if not locations:
            raise ValueError(f"Required phrase not found: {before}")
        for offset in locations:
            original = struct.unpack_from(f"<{len(before)}H", source, offset)
            for column, (slot, character) in enumerate(zip(original, after)):
                if mapping[slot].target != character:
                    write(offset + column * 2, struct.pack("<H", glyph_slot(character)))
        report.append({"source": before, "target": after, "count": len(locations),
                       "offsets": [hex(offset) for offset in locations]})

    elevator_locations = phrase_locations(source, "不能乘", charset, runs)
    if len(elevator_locations) != 12:
        raise ValueError("Unexpected number of reviewed elevator options")
    for offset in elevator_locations:
        if source[offset + 6:offset + 12] != struct.pack("<3H", 0xFF01, 0xFF02, 0xFFFF):
            raise ValueError("Elevator option is not the final menu choice")
        write(offset, struct.pack("<3H", glyph_slot("不"), glyph_slot("乘"), 0xAC))

    only_locations = set(phrase_locations(source, "\u53ea", charset, runs))
    if not set(CLASSIFIER_OFFSETS) <= only_locations:
        raise ValueError("Reviewed classifier locations no longer match the source text")
    classifier_slot = glyph_slot("\u96bb")
    for offset in CLASSIFIER_OFFSETS:
        write(offset, struct.pack("<H", classifier_slot))
    for offset in only_locations - set(CLASSIFIER_OFFSETS):
        if output[offset:offset + 2] != source[offset:offset + 2]:
            raise AssertionError("Non-classifier use of zhi changed")

    start, end = 0x137032A, 0x137033A
    expected = [0x5E] + [min(entry.index for entry in charset if entry.source == character)
                         for character in "\u8bf4\u4e86\u8c01\u8df3\u821e"] + [9, 0xFFFF]
    if list(struct.unpack_from("<8H", source, start)) != expected:
        raise ValueError("Desert dialogue does not match the reviewed line")
    replacement = struct.pack("<10H", 0x5E, *(glyph_slot(character) for character in
                              "\u8ab0\u53eb\u4f60\u8df3\u821e\u4e86"), 8, 9, 0xFFFF)
    pointer_start, data_start, bank_end = 0x136C644, 0x136C870, 0x13718A0
    bank = bytearray(output[pointer_start:bank_end])
    bank[start - pointer_start:end - pointer_start] = replacement
    delta = len(replacement) - (end - start)
    for position in range(0, data_start - pointer_start, 2):
        relative = struct.unpack_from("<H", bank, position)[0]
        if relative >= end - data_start:
            struct.pack_into("<H", bank, position, relative + delta)
        elif start - data_start < relative < end - data_start:
            raise ValueError("Another string points inside the replaced dialogue")
    relocation = 0x1F88000
    records.append(padding_record(source, relocation, bytes(bank)))
    records.append((SCRIPT_TABLE + 4, struct.pack("<II", relocation - SCRIPT_TABLE,
                    relocation + data_start - pointer_start - SCRIPT_TABLE)))
    ending_table = SCRIPT_TABLE + 4 + ENDING_BANK * 8
    if struct.unpack_from("<II", source, ending_table) != (
            ENDING_START - SCRIPT_TABLE, ENDING_DATA - SCRIPT_TABLE):
        raise ValueError("Ending dialogue bank differs from the verified ROM")
    if (struct.unpack_from("<5H", source, 0x13D5314) != (0x5E, 0x306, 0x307, 0x36, 0x322)
            or struct.unpack_from("<4H", source, 0x13D53B8) != (2, 0x5E, 0x304, 0x3C9)):
        raise ValueError("Ending dialogue differs from the reviewed lines")
    ending = bytearray(output[ENDING_START:ENDING_END])
    for offset, value in reversed(ENDING_INSERTIONS):
        relative = offset - ENDING_START
        ending[relative:relative] = struct.pack("<H", value)
    for position in range(0, ENDING_DATA - ENDING_START, 2):
        relative = struct.unpack_from("<H", ending, position)[0]
        delta = sum(2 for offset, _ in ENDING_INSERTIONS if ENDING_DATA + relative >= offset)
        struct.pack_into("<H", ending, position, relative + delta)
    records.append(padding_record(source, ENDING_RELOCATED, bytes(ending)))
    records.append((ending_table, struct.pack("<II", ENDING_RELOCATED - SCRIPT_TABLE,
                    ENDING_RELOCATED + ENDING_DATA - ENDING_START - SCRIPT_TABLE)))
    for offset, data in records:
        output[offset:offset + len(data)] = data
    text_runs(output)
    egg_start, egg_end = 0x136D7BA, 0x136D7F2
    relocated_egg = relocation + egg_start - pointer_start
    if output[relocated_egg:relocated_egg + egg_end - egg_start] != source[egg_start:egg_end]:
        raise AssertionError("Preserved Easter egg changed")
    return records, {
        "phrases": report,
        "glyph_slots": {character: hex(slot) for character, slot in allocated.items()},
        "bad_character": {"slot": "0x3b8", "source": "\u574f", "target": "\u58de",
                          "count": len(phrase_locations(source, "\u574f", charset, runs))},
        "cover_character": {"source": "\u76d6", "target": "\u84cb", "already_converted": True},
        "dialogue": {"entry": "0-116", "target": "\u8ab0\u53eb\u4f60\u8df3\u821e\u4e86\uff1f\uff01"},
        "elevator_option": {"source": "不能乘", "target": "不乘",
                    "count": len(elevator_locations), "trailing_space": True,
                    "offsets": [hex(offset) for offset in elevator_locations]},
        "easter_egg_preserved": True,
        "ending_dialogue": {"bank": ENDING_BANK, "relocated": hex(ENDING_RELOCATED),
                    "opening_quote_added": True, "newline_before_next_bullet": True},
        "special_font": {"offset": hex(SPECIAL_FONT), "inspected_slots": SPECIAL_COUNT,
                 "source_sha256": SPECIAL_SHA256, "stride": 32,
                 "targets": {hex(slot): character for slot, character in SPECIAL_TARGETS.items()},
                 "unchanged_slots_preserved": True},
        "zhi_classifier": {"target": "\u96bb", "count": len(CLASSIFIER_OFFSETS),
                   "offsets": [hex(offset) for offset in CLASSIFIER_OFFSETS],
                   "other_uses_preserved": len(only_locations) - len(CLASSIFIER_OFFSETS)},
        "validated_text_runs": len(runs),
    }