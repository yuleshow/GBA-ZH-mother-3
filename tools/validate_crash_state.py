"""Inspect a copied RetroArch state and replay it in a bounded local process."""

import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import zlib

from tools.validate_screens import ScreenCore


class ReplayCore(ScreenCore):
    def __init__(self, path, system_dir):
        self.system_dir = str(system_dir.resolve()).encode()
        super().__init__(path)

    def environment(self, command, data):
        if command in (9, 30, 31):
            ctypes.cast(data, ctypes.POINTER(ctypes.c_char_p))[0] = self.system_dir
            return True
        return super().environment(command, data)


def decode_state(data):
    if data.startswith(b"#RZIPv\x01#"):
        block_size, expected_size = struct.unpack_from("<IQ", data, 8)
        if not 0 < expected_size <= 16 * 1024 * 1024:
            raise ValueError("Unexpected state size")
        position = 20
        chunks = []
        while position < len(data):
            size = struct.unpack_from("<I", data, position)[0]
            position += 4
            if not 0 < size <= len(data) - position:
                raise ValueError("Invalid RZIP block size")
            decoder = zlib.decompressobj()
            chunk = decoder.decompress(data[position:position + size], block_size + 1)
            if not decoder.eof or decoder.unused_data or len(chunk) > block_size:
                raise ValueError("Invalid RZIP block")
            chunks.append(chunk)
            position += size
        data = b"".join(chunks)
        if len(data) != expected_size:
            raise ValueError("RZIP size mismatch")
    if data.startswith(b"RASTATE\x01"):
        position = 8
        memory = None
        while position + 8 <= len(data):
            tag, size = struct.unpack_from("<4sI", data, position)
            position += 8
            if size > len(data) - position:
                raise ValueError("Truncated RetroArch block")
            if tag == b"MEM ":
                if memory is not None:
                    raise ValueError("Duplicate memory block")
                memory = data[position:position + size]
            position += (size + 7) & ~7
            if tag == b"END ":
                break
        if memory is None:
            raise ValueError("Missing core state")
        data = memory
    if len(data) < 0x61000:
        raise ValueError("State too small for the mGBA layout")
    return data


def snapshot(state, frame):
    return {
        "frame": frame,
        "registers": [hex(value) for value in struct.unpack_from("<16I", state, 0x20)],
        "cpsr": hex(struct.unpack_from("<I", state, 0x60)[0]),
        "ewram_sha256": hashlib.sha256(state[0x21000:0x61000]).hexdigest(),
        "vram_sha256": hashlib.sha256(state[0x1000:0x19000]).hexdigest(),
    }


def replay(args):
    rom = args.rom.read_bytes()
    encoded = args.state.read_bytes()
    state = decode_state(encoded)
    print(json.dumps({"rom_sha256": hashlib.sha256(rom).hexdigest(),
                      "state_sha256": hashlib.sha256(encoded).hexdigest(),
                      "core_sha256": hashlib.sha256(args.core.read_bytes()).hexdigest(),
                      "decoded_bytes": len(state), "initial": snapshot(state, 0)}), flush=True)
    core = ReplayCore(args.core, args.system_dir)
    loaded = False
    try:
        core.load(rom)
        loaded = True
        core.library.retro_cheat_reset()
        buffer = ctypes.create_string_buffer(state)
        accepted = core.library.retro_unserialize(buffer, len(state))
        print(json.dumps({"state_accepted": accepted}), flush=True)
        if not accepted:
            raise ValueError("mGBA rejected state")
        checkpoints = {1, 60, 600, args.frames}
        for frame in range(1, args.frames + 1):
            core.capture_video = frame in checkpoints
            core.buttons = {8} if args.press_a and frame % 30 < 2 else set()
            core.library.retro_run()
            if frame in checkpoints:
                record = snapshot(core.save(), frame)
                if core.image is not None:
                    record["image_sha256"] = hashlib.sha256(core.image.tobytes()).hexdigest()
                    core.image.save(args.output_dir / f"frame-{frame:05d}.png")
                print(json.dumps(record), flush=True)
    finally:
        if loaded:
            core.unload()
        core.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rom", type=Path)
    parser.add_argument("state", type=Path)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--system-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=600)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--press-a", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.frames < 1 or args.timeout <= 0:
        parser.error("frames and timeout must be positive")
    if args.worker:
        replay(args)
        return
    args.output_dir.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, "-m", "tools.validate_crash_state", *sys.argv[1:], "--worker"]
    with (args.output_dir / "replay.log").open("x") as log:
        try:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=args.timeout, check=False)
            outcome = {"timed_out": False, "returncode": result.returncode}
        except subprocess.TimeoutExpired:
            outcome = {"timed_out": True, "returncode": None}
    outcome.update({"frames_requested": args.frames, "timeout_seconds": args.timeout,
                    "press_a": args.press_a, "cheats_enabled_by_probe": False,
                    "state_modified_on_disk": False})
    (args.output_dir / "outcome.json").write_text(json.dumps(outcome, indent=2) + "\n")
    print(json.dumps(outcome))
    print((args.output_dir / "replay.log").read_text())


if __name__ == "__main__":
    main()