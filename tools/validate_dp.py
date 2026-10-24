"""Check DP cheats in an isolated mGBA state without loading user saves."""

import argparse
import configparser
import ctypes
import hashlib
import json
from pathlib import Path
import struct

from tools.validate_enemy_one_hit import Core, ROM_HASHES


def probe(core, codes):
    state = bytearray(core.baseline)
    struct.pack_into("<I", state, 0x24000, 0xE7FEE7FE)
    struct.pack_into("<I", state, 0x5C, 0x02003002)
    struct.pack_into("<I", state, 0x60, 0xFF)
    struct.pack_into("<II", state, 0x2F8, 0, 0)
    struct.pack_into("<I", state, 0x31C, 2)
    struct.pack_into("<H", state, 0x608, 0)
    struct.pack_into("<I", state, 0x21000 + 0x19544, 123)
    buffer = ctypes.create_string_buffer(bytes(state))
    core.library.retro_cheat_reset()
    if not core.library.retro_unserialize(buffer, len(state)):
        raise RuntimeError("mGBA refused the isolated DP state")
    for index, code in enumerate(codes):
        core.library.retro_cheat_set(index, True, code.encode("ascii"))
    for _ in range(3):
        core.library.retro_run()
    result = core.save()
    return struct.unpack_from("<I", result, 0x21000 + 0x19544)[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rom", type=Path)
    parser.add_argument("cheat", type=Path)
    parser.add_argument("--core", type=Path, required=True)
    args = parser.parse_args()
    rom = args.rom.read_bytes()
    checksum = hashlib.sha256(rom).hexdigest()
    if checksum not in ROM_HASHES:
        raise ValueError("Unsupported ROM checksum")
    config = configparser.ConfigParser(interpolation=None)
    config.read_string("[cheats]\n" + args.cheat.read_text())
    money_code = json.loads(config["cheats"]["cheat2_code"])
    core = Core(args.core)
    try:
        core.load(rom)
        try:
            for _ in range(120):
                core.library.retro_run()
            core.baseline = core.save()
            control = probe(core, [])
            actual = probe(core, [money_code])
            print(json.dumps({"rom_sha256": checksum, "code": money_code,
                              "address": "0x02019544", "control": control,
                              "with_cheat": actual, "gameplay_verified": False}, indent=2))
            assert control == 123, control
            assert actual == 999999, actual
        finally:
            core.library.retro_cheat_reset()
            core.unload()
    finally:
        core.close()


if __name__ == "__main__":
    main()