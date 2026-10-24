"""Run isolated enemy HP-setter checks in an external mGBA libretro core."""

import argparse
import ctypes
import hashlib
import json
import struct
from pathlib import Path


PATCH_OFFSET = 0x81494
ORIGINAL = bytes.fromhex(
    "30b5051c0c1cf1f70df90006000e012804d10120a04200da201c041c"
    "281c211cf3f71cfe30bc01bc00470000"
)
ROM_HASHES = {
    "b3ed0f1c5c8d53a9537bee32c993b986f38ae57453c882499191f1dbafb31fb3",
    "a2fe74acbb3e9403fb573ca27b3e385a0a6c08bc9108fcdc80a0c43b434adda3",
}
ENVIRONMENT = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_uint, ctypes.c_void_p)
VIDEO = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_size_t
)
AUDIO = ctypes.CFUNCTYPE(None, ctypes.c_int16, ctypes.c_int16)
AUDIO_BATCH = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t)
INPUT_POLL = ctypes.CFUNCTYPE(None)
INPUT_STATE = ctypes.CFUNCTYPE(
    ctypes.c_int16, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint
)


def cheat_code(patch):
    if len(patch) != len(ORIGINAL):
        raise ValueError("Patch must fit the original 44-byte function")
    lines = []
    for offset in range(0, len(patch), 2):
        value = struct.unpack_from("<H", patch, offset)[0]
        if patch[offset:offset + 2] != ORIGINAL[offset:offset + 2]:
            address = 0x18000000 | ((PATCH_OFFSET + offset) >> 1)
            lines.extend((f"00000000+{address:08X}", f"{value:08X}+00000000"))
    if not lines:
        raise ValueError("Patch contains no changes")
    return "+".join(lines)


def cheat_file(patch):
    return (
        'cheats = 1\n\n'
        'cheat0_desc = "Enemy-only one-hit KO - Chinese v1.2 - EXPERIMENTAL"\n'
        'cheat0_enable = false\n'
        'cheat0_handler = 0\n'
        f'cheat0_code = "{cheat_code(patch)}"\n'
    )


class GameInfo(ctypes.Structure):
    _fields_ = [
        ("path", ctypes.c_char_p),
        ("data", ctypes.c_void_p),
        ("size", ctypes.c_size_t),
        ("meta", ctypes.c_char_p),
    ]


class Core:
    def __init__(self, path):
        self.library = ctypes.CDLL(str(path.resolve()))
        self.callbacks = [
            ENVIRONMENT(self.environment),
            VIDEO(lambda *args: None),
            AUDIO(lambda *args: None),
            AUDIO_BATCH(lambda data, frames: frames),
            INPUT_POLL(lambda: None),
            INPUT_STATE(lambda *args: 0),
        ]
        for name, callback in zip(
            ("environment", "video_refresh", "audio_sample", "audio_sample_batch",
             "input_poll", "input_state"), self.callbacks
        ):
            setter = getattr(self.library, "retro_set_" + name)
            setter.argtypes = [type(callback)]
            setter.restype = None
            setter(callback)
        self.library.retro_load_game.argtypes = [ctypes.POINTER(GameInfo)]
        self.library.retro_load_game.restype = ctypes.c_bool
        self.library.retro_serialize_size.restype = ctypes.c_size_t
        for name in ("retro_serialize", "retro_unserialize"):
            function = getattr(self.library, name)
            function.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            function.restype = ctypes.c_bool
        self.library.retro_cheat_set.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_char_p]
        self.library.retro_cheat_set.restype = None
        self.library.retro_init()

    @staticmethod
    def environment(command, data):
        if command in (9, 30, 31):
            ctypes.cast(data, ctypes.POINTER(ctypes.c_char_p))[0] = b"/tmp"
            return True
        return command in (10, 11, 16, 18)

    def load(self, rom):
        self.rom_buffer = ctypes.create_string_buffer(bytes(rom))
        info = GameInfo(
            b"mother3-isolated-test.gba", ctypes.cast(self.rom_buffer, ctypes.c_void_p),
            len(rom), None
        )
        if not self.library.retro_load_game(ctypes.byref(info)):
            raise RuntimeError("mGBA refused the in-memory test ROM")
        self.library.retro_run()
        self.baseline = self.save()
        if len(self.baseline) < 0x61000:
            raise RuntimeError("Unexpected mGBA state size")

    def save(self):
        buffer = ctypes.create_string_buffer(self.library.retro_serialize_size())
        if not self.library.retro_serialize(buffer, len(buffer)):
            raise RuntimeError("mGBA could not serialize the test state")
        return buffer.raw

    def invoke(self, entry, current, requested, maximum):
        state = bytearray(self.baseline)

        def word(offset, value):
            struct.pack_into("<I", state, offset, value & 0xFFFFFFFF)

        def ram_word(address, value):
            word(0x21000 + address - 0x02000000, value)

        target = 0x02001000
        vtable = 0x02002000
        sentinel = 0x02003000
        getter = 0x02003010
        ram_word(target + 0x1C, vtable)
        ram_word(target + 0x2C, current)
        ram_word(target + 0x30, maximum)
        ram_word(vtable + 0x1A8, 0)
        ram_word(vtable + 0x1AC, getter | 1)
        ram_word(vtable + 0x198, 0)
        ram_word(vtable + 0x19C, (getter + 4) | 1)
        ram_word(sentinel, 0xE7FEE7FE)
        ram_word(getter, 0x47706B00)
        ram_word(getter + 4, 0x47706AC0)
        for index in range(16):
            word(0x20 + index * 4, 0)
        word(0x20, target)
        word(0x24, requested)
        word(0x54, 0x03007000)
        word(0x58, sentinel | 1)
        word(0x5C, entry + 2)
        word(0x60, 0xFF)
        word(0x2F8, 0)
        word(0x2FC, 0)
        word(0x31C, 2)
        struct.pack_into("<H", state, 0x608, 0)
        buffer = ctypes.create_string_buffer(bytes(state))
        if not self.library.retro_unserialize(buffer, len(state)):
            raise RuntimeError("mGBA refused the synthetic function-call state")
        for _ in range(3):
            self.library.retro_run()
            result = self.save()
            program_counter = struct.unpack_from("<I", result, 0x5C)[0]
            if program_counter in (sentinel + 2, sentinel + 4):
                break
        if program_counter not in (sentinel + 2, sentinel + 4):
            raise AssertionError(f"Function did not return: PC={program_counter:#x}")
        preserved = struct.unpack_from("<8I", result, 0x30)
        stack_pointer = struct.unpack_from("<I", result, 0x54)[0]
        if any(preserved) or stack_pointer != 0x03007000:
            raise AssertionError("Function corrupted preserved registers or the stack")
        return struct.unpack_from("<I", result, 0x21000 + 0x102C)[0]

    def unload(self):
        self.library.retro_unload_game()

    def close(self):
        self.library.retro_deinit()


def validate(rom_path, patch_path, core_path):
    rom = rom_path.read_bytes()
    rom_hash = hashlib.sha256(rom).hexdigest()
    if rom_hash not in ROM_HASHES:
        raise ValueError("Unsupported ROM checksum")
    if rom[PATCH_OFFSET:PATCH_OFFSET + len(ORIGINAL)] != ORIGINAL:
        raise ValueError("Enemy HP setter does not match the verified instructions")
    patch = patch_path.read_bytes()
    if len(patch) != len(ORIGINAL):
        raise ValueError("Patch must fit the original 44-byte function")
    cases = [
        ("damage", 100, 90, 100),
        ("one-point-damage", 100, 99, 100),
        ("lethal-damage", 100, 0, 100),
        ("overkill", 100, -20, 100),
        ("zero-damage", 100, 100, 100),
        ("healing", 50, 70, 100),
        ("healing-cap", 50, 150, 100),
        ("initialization", 1, 1200, 1200),
        ("revival", 0, 100, 100),
        ("already-zero", 0, 0, 100),
    ]
    results = []
    core = Core(core_path)
    try:
        for mode in ("original", "patched-buffer", "libretro-cheat"):
            patched = mode != "original"
            for protected in (False, True):
                fixture = bytearray(rom)
                if mode == "patched-buffer":
                    fixture[PATCH_OFFSET:PATCH_OFFSET + len(patch)] = patch
                fixture[0x726B8:0x726BC] = struct.pack("<HH", 0x2000 | protected, 0x4770)
                core.load(fixture)
                try:
                    if mode == "libretro-cheat":
                        core.library.retro_cheat_set(0, True, cheat_code(patch).encode("ascii"))
                    for name, current, requested, maximum in cases:
                        for enemy in (False, True):
                            expected = requested
                            if enemy and patched and not protected and requested < current:
                                expected = 0
                            if enemy and protected:
                                expected = max(1, expected)
                            expected = max(0, min(maximum, expected))
                            actual = core.invoke(
                                0x08081494 if enemy else 0x0807BBA4,
                                current, requested, maximum
                            )
                            result = dict(case=name, mode=mode, protected=protected,
                                          enemy=enemy, expected=expected, actual=actual)
                            if actual != expected:
                                raise AssertionError(result)
                            results.append(result)
                finally:
                    core.library.retro_cheat_reset()
                    core.unload()
    finally:
        core.close()
    return {
        "rom_sha256": rom_hash,
        "core_sha256": hashlib.sha256(core_path.read_bytes()).hexdigest(),
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "cheat_file_sha256": hashlib.sha256(cheat_file(patch).encode("ascii")).hexdigest(),
        "patch_offset": hex(PATCH_OFFSET),
        "patch_original_hex": ORIGINAL.hex(),
        "patch_replacement_hex": patch.hex(),
        "validation_scope": "Isolated synthetic objects; protection function stubbed; no gameplay",
        "gameplay_verified": False,
        "device_deployed": False,
        "limitations": [
            "Any HP decrease through Monster::setHP triggers KO, including non-attack damage.",
            "Custom enemy setters and scripted/boss encounters are not gameplay-verified.",
            "Disable before scripted/boss battles; restart content to clear ROM code patches.",
        ],
        "cases_passed": len(results),
        "cases": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rom", type=Path)
    parser.add_argument("--patch", required=True, type=Path)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.output_dir is not None and args.output_dir.exists():
        parser.error("Output directory already exists; refusing to overwrite")
    report = validate(args.rom, args.patch, args.core)
    report_json = json.dumps(report, indent=2) + "\n"
    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=False)
        cheat_path = args.output_dir / "Mother 3 (Japan) - Enemy One Hit EXPERIMENTAL.cht"
        with cheat_path.open("x", encoding="ascii") as output:
            output.write(cheat_file(args.patch.read_bytes()))
        with (args.output_dir / "validation.json").open("x", encoding="ascii") as output:
            output.write(report_json)
        print(f"Passed {report['cases_passed']} isolated mGBA checks; wrote {cheat_path}")
    else:
        print(report_json, end="")


if __name__ == "__main__":
    main()
