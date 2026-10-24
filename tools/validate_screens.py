"""Capture isolated boot frames without loading or writing user saves."""

import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import struct

from PIL import Image

from tools.build_font_patch import BANKS
from tools.build_screen_patch import MISC_TABLE, decompress
from tools.build_text_patch import corrected_charset
from tools.validate_enemy_one_hit import Core, INPUT_STATE, VIDEO


class ScreenCore(Core):
    def __init__(self, path):
        self.pixel_format = 0
        self.image = None
        self.buttons = set()
        self.capture_video = True
        super().__init__(path)
        self.video_callback = VIDEO(self.video)
        self.library.retro_set_video_refresh(self.video_callback)
        self.input_callback = INPUT_STATE(
            lambda port, device, index, button: int(button in self.buttons)
        )
        self.library.retro_set_input_state(self.input_callback)

    def environment(self, command, data):
        if command == 10:
            self.pixel_format = ctypes.cast(data, ctypes.POINTER(ctypes.c_uint))[0]
            return self.pixel_format in (0, 1, 2)
        return super().environment(command, data)

    def video(self, data, width, height, pitch):
        if not data or not self.capture_video:
            return
        pixels = ctypes.string_at(data, pitch * height)
        output = bytearray()
        for row in range(height):
            for column in range(width):
                if self.pixel_format == 1:
                    value = struct.unpack_from("<I", pixels, row * pitch + column * 4)[0]
                    output.extend(((value >> 16) & 255, (value >> 8) & 255, value & 255))
                else:
                    value = struct.unpack_from("<H", pixels, row * pitch + column * 2)[0]
                    green_bits = 6 if self.pixel_format == 2 else 5
                    output.extend((
                        ((value >> (green_bits + 5)) & 31) * 255 // 31,
                        ((value >> 5) & ((1 << green_bits) - 1)) * 255 // ((1 << green_bits) - 1),
                        (value & 31) * 255 // 31,
                    ))
        self.image = Image.frombytes("RGB", (width, height), bytes(output))


def capture(rom_path, core_path, output_dir, frames, press_frames=()):
    output_dir.mkdir(parents=True, exist_ok=False)
    rom = rom_path.read_bytes()
    core = ScreenCore(core_path)
    records = []
    try:
        core.load(rom)
        for frame in range(1, max(frames) + 1):
            core.capture_video = frame in frames
            core.buttons = {8} if frame in press_frames else set()
            if frame > 1:
                core.library.retro_run()
            if frame not in frames:
                continue
            if core.image is None:
                raise RuntimeError("No video frame received")
            core.image.save(output_dir / f"frame-{frame:04d}.png")
            state = core.save()
            (output_dir / f"frame-{frame:04d}.state").write_bytes(state)
            records.append({
                "frame": frame,
                "video_sha256": hashlib.sha256(core.image.tobytes()).hexdigest(),
                "pc": hex(struct.unpack_from("<I", state, 0x5C)[0]),
                "display_control": hex(struct.unpack_from("<H", state, 0x400)[0]),
            })
        core.unload()
    finally:
        core.close()
    report = {
        "rom_sha256": hashlib.sha256(rom).hexdigest(),
        "core_sha256": hashlib.sha256(core_path.read_bytes()).hexdigest(),
        "pixel_format": core.pixel_format,
        "user_saves_accessed": False,
        "checkpoints": records,
    }
    (output_dir / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def validate_chapters(rom_path, core_path, output_dir):
    output_dir.mkdir(parents=True, exist_ok=False)
    rom = rom_path.read_bytes()

    def resource(index, length):
        start = MISC_TABLE + struct.unpack_from("<I", rom, MISC_TABLE + 4 + index * 4)[0]
        return rom[start:start + length]

    expected_tiles = decompress(resource(44, 0x10000))
    palette = resource(45, 64)
    core = ScreenCore(core_path)
    results = []
    try:
        core.load(rom)
        for chapter in range(8):
            state = bytearray(core.baseline)
            sentinel = 0x02003000
            struct.pack_into("<I", state, 0x24000, 0xE7FEE7FE)
            for index in range(16):
                struct.pack_into("<I", state, 0x20 + index * 4, 0)
            for offset, value in ((0x20, chapter), (0x54, 0x03007000),
                                  (0x58, sentinel | 1), (0x5C, 0x08019EEA),
                                  (0x60, 0xFF), (0x2F8, 0), (0x2FC, 0), (0x31C, 2)):
                struct.pack_into("<I", state, offset, value)
            struct.pack_into("<H", state, 0x608, 0)
            buffer = ctypes.create_string_buffer(bytes(state))
            if not core.library.retro_unserialize(buffer, len(state)):
                raise RuntimeError("Chapter fixture could not be loaded")
            core.capture_video = False
            for _ in range(60):
                core.library.retro_run()
                result = bytearray(core.save())
                program_counter = struct.unpack_from("<I", result, 0x5C)[0]
                if program_counter in (sentinel + 2, sentinel + 4):
                    break
            if program_counter not in (sentinel + 2, sentinel + 4):
                raise AssertionError(f"Chapter loader did not return: {program_counter:#x}")
            if result[0x1000:0x1000 + len(expected_tiles)] != expected_tiles:
                raise AssertionError("Emulator chapter VRAM differs from the built tiles")
            expected_map = resource(46 + chapter, 0x800)
            loaded_map = result[0x38878:0x39078]
            if loaded_map != expected_map:
                raise AssertionError("Chapter loader copied an incorrect tilemap")
            result[0xF000:0xF800] = loaded_map
            result[0x800:0x840] = palette
            for offset, value in ((0x400, 0x100), (0x408, 0x1C00),
                                  (0x410, 0), (0x412, 0), (0x450, 0)):
                struct.pack_into("<H", result, offset, value)
            buffer = ctypes.create_string_buffer(bytes(result))
            if not core.library.retro_unserialize(buffer, len(result)):
                raise RuntimeError("Chapter display fixture could not be loaded")
            core.capture_video = True
            for _ in range(2):
                core.library.retro_run()
            if core.image is None or not core.image.getbbox():
                raise AssertionError("Chapter fixture rendered blank")
            core.image.save(output_dir / f"chapter-{chapter + 1}.png")
            results.append({"chapter": chapter + 1, "loader_returned": True,
                            "tiles_match": True, "tilemap_matches": True,
                            "video_sha256": hashlib.sha256(core.image.tobytes()).hexdigest()})
        core.unload()
    finally:
        core.close()
    report = {
        "rom_sha256": hashlib.sha256(rom).hexdigest(),
        "core_sha256": hashlib.sha256(core_path.read_bytes()).hexdigest(),
        "scope": "Actual chapter loader in isolated states; display registers configured by fixture",
        "full_playthrough": False, "chapter_fades_verified": False,
        "user_saves_accessed": False, "chapters": results,
    }
    (output_dir / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def validate_typography(rom_path, core_path, output_dir):
    output_dir.mkdir(parents=True, exist_ok=False)
    rom = rom_path.read_bytes()
    report = json.loads((rom_path.parent / "report.json").read_text())
    if hashlib.sha256(rom).hexdigest() != report["output_sha256"]:
        raise ValueError("ROM does not match its build report")
    if report.get("typography", {}).get("selection") != "C2":
        raise ValueError("Requires the C2 build")
    targets = {entry.index: entry.target for entry in corrected_charset()}
    targets.update({int(slot, 16): character
                    for character, slot in report["text"]["glyph_slots"].items()})
    slots = {character: min(slot for slot, target in targets.items() if target == character)
             for character in set(targets.values())}
    fixture = bytearray(rom)
    stubs = {
        0x6B070: bytes.fromhex("00207047"),
        0x6BB34: bytes.fromhex("014901607047c046f000a000"),
        0x6B828: bytes.fromhex("520101488018704700800002"),
    }
    for offset, data in stubs.items():
        fixture[offset:offset + len(data)] = data
    core = Core(core_path)
    results = []
    samples = ("\u8aa0\u5be6\u4e38\u5b50", "\u6696\u548c\u7684\u6bdb\u8863",
               "\u5145\u6eff\u4e86\u87cb\u87c0\u7684\u8aa0\u610f\u7684\u4e38\u5b50",
               "\u64cb\u4f4f\u4e86\u53bb\u8def", "\u9aea\u9aee\u53c4\u78e3\u562c")
    sheet = Image.new("RGB", (240, 96), "#111820")

    def invoke(state, entry, arguments, stack_value=0):
        state = bytearray(state)
        for index in range(16):
            struct.pack_into("<I", state, 0x20 + index * 4, 0)
        for index, value in enumerate(arguments):
            struct.pack_into("<I", state, 0x20 + index * 4, value)
        for offset, value in ((0x24000, 0xE7FEE7FE), (0x54, 0x03007000),
                              (0x58, 0x02003001), (0x5C, entry + 2),
                              (0x60, 0xFF), (0x2F8, 0), (0x2FC, 0), (0x31C, 2),
                              (0x20000, stack_value)):
            struct.pack_into("<I", state, offset, value)
        struct.pack_into("<H", state, 0x608, 0)
        buffer = ctypes.create_string_buffer(bytes(state))
        if not core.library.retro_unserialize(buffer, len(state)):
            raise RuntimeError("Could not load the typography fixture")
        for _ in range(3):
            core.library.retro_run()
            result = core.save()
            if struct.unpack_from("<I", result, 0x5C)[0] in (0x02003002, 0x02003004):
                return bytearray(result)
        raise AssertionError("Typography function did not return")

    try:
        core.load(fixture)
        baseline = bytearray(core.baseline)
        struct.pack_into("<IHH", baseline, 0x22020, 0x08CF99F8, 10, 10)
        baseline = invoke(baseline, 0x08088E4C, (0x02001000, 0x02001020, 0x02001024))
        advance, height, pointer = struct.unpack_from("<HHI", baseline, 0x22000)
        if (advance, height, pointer) != (11, 10, 0x08CF99F8):
            raise AssertionError("Unexpected runtime font descriptor")
        bank = BANKS[1]
        for sample_index, text in enumerate(samples):
            for left in range(8):
                state = bytearray(baseline)
                state[0x29000:0x2DB00] = bytes(19200)
                expected = Image.new("L", (240, 160))
                top = 5 + sample_index % 8
                for index, character in enumerate(text):
                    position = left + index * advance
                    struct.pack_into("<HH", state, 0x22010, position, top)
                    state = invoke(state, 0x08089178,
                                   (0, 0x02001000, 0x02001010, 1), slots[character])
                    offset = bank.offset + slots[character] * bank.stride
                    bitmap = rom[offset:offset + bank.stride]
                    for row in range(10):
                        for column in range(10):
                            if bitmap[row * 2 + column // 8] & (128 >> (column % 8)):
                                expected.putpixel((position + column, top + row), 255)
                actual = Image.new("L", (240, 160))
                tiles = state[0x29000:0x2DB00]
                for row in range(160):
                    for column in range(240):
                        offset = ((row // 8) * 30 + column // 8) * 32 + (row % 8) * 4 + column % 8 // 2
                        color = (tiles[offset] >> (4 * (column % 2))) & 15
                        if color:
                            if color != 1:
                                raise AssertionError("Unexpected glyph palette index")
                            actual.putpixel((column, row), 255)
                if actual.tobytes() != expected.tobytes():
                    actual.save(output_dir / "failed-actual.png")
                    expected.save(output_dir / "failed-expected.png")
                    raise AssertionError(f"Runtime glyph pixels differ: sample {sample_index}, x={left}")
                if left == 4:
                    sheet.paste("#f8f8f0", (0, sample_index * 18), actual.crop((0, top, 240, top + 10)))
                results.append({"text": text, "x_alignment": left, "pixels_match": True})
        core.unload()
    finally:
        core.close()
    sheet.resize((960, 384), Image.Resampling.NEAREST).save(output_dir / "rendered-samples.png")
    validation = {
        "rom_sha256": hashlib.sha256(rom).hexdigest(),
        "core_sha256": hashlib.sha256(core_path.read_bytes()).hexdigest(),
        "runtime_advance": advance, "glyph_stride": bank.stride,
        "scope": "Actual font initializer and glyph renderer; buffer accessors stubbed, positions supplied by fixture",
        "buffer_accessor_stubs": [hex(offset) for offset in stubs],
        "full_game_layout_verified": False, "user_saves_accessed": False,
        "cases_passed": len(results), "cases": results,
    }
    (output_dir / "validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in validation.items() if key != "cases"}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rom", type=Path)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--frames", nargs="+", type=int, default=[1, 30, 60, 120, 240, 360, 600])
    parser.add_argument("--press", nargs="*", type=int, default=[])
    parser.add_argument("--chapters", action="store_true")
    parser.add_argument("--typography", action="store_true")
    args = parser.parse_args()
    if not args.frames or min(args.frames) < 1:
        parser.error("Frames must be positive")
    if args.chapters and args.typography:
        parser.error("Choose only one validation mode")
    if args.typography:
        validate_typography(args.rom, args.core, args.output_dir)
    elif args.chapters:
        validate_chapters(args.rom, args.core, args.output_dir)
    else:
        capture(args.rom, args.core, args.output_dir, set(args.frames), set(args.press))


if __name__ == "__main__":
    main()