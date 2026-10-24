"""Render local typography mockups without modifying ROMs or device files."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import struct

from PIL import Image, ImageDraw, ImageFont

from tools.build_font_patch import BANKS, ROOT, encode_glyph, read_bdf
from tools.build_text_patch import character_runs, corrected_charset, target_glyphs


def preview(build_dir, output_dir, typefaces=False, alternative_font=None, raster_font=None):
    rom = (build_dir / "mother3-zh-Hant-reviewed.gba").read_bytes()
    report = json.loads((build_dir / "report.json").read_text())
    assert sha256(rom).hexdigest() == report["output_sha256"]
    targets = {entry.index: entry.target for entry in corrected_charset()}
    targets.update({int(slot, 16): character
                    for character, slot in report["text"]["glyph_slots"].items()})
    slots = {}
    for slot, character in sorted(targets.items()):
        slots.setdefault(character, slot)
    directory = ROOT / "data/fonts"
    fonts = {
        12: read_bdf(directory / "fusion-pixel-12px-monospaced-zh_hant.bdf"),
        10: read_bdf(directory / "fusion-pixel-10px-monospaced-zh_hant.bdf"),
        "full": read_bdf(directory / "mother3-traditional-full-cell-10px.bdf"),
    }
    if alternative_font is not None:
        if not typefaces:
            raise ValueError("Alternative fonts require --typefaces")
        fonts["alternative"] = read_bdf(alternative_font)
    bank = BANKS[1]
    candidate_cache = {}
    raster_cache = {}
    raster = None
    if raster_font is not None:
        if not typefaces or alternative_font is not None:
            raise ValueError("Use --raster-font with --typefaces and without --alternative-font")
        raster = ImageFont.truetype(str(raster_font), bank.rows, index=2)

    def bitmap(slot, style):
        assert 0 <= slot < 0x1000
        if style == "song" and slot in targets:
            character = targets[slot]
            if character not in raster_cache:
                assert bytes(raster.getmask(character, mode="1")) != bytes(raster.getmask("\uffff", mode="1"))
                scratch = Image.new("1", (32, 32))
                ImageDraw.Draw(scratch).text((8, 8), character, font=raster, anchor="lt", fill=1)
                bounds = scratch.getbbox()
                assert bounds is not None, character
                ink = scratch.crop(bounds)
                assert ink.width <= bank.width and ink.height <= bank.rows, character
                mask = Image.new("1", (bank.width, bank.rows))
                mask.paste(ink, ((bank.width - ink.width) // 2, bank.rows - ink.height))
                raster_cache[character] = mask
            return raster_cache[character]
        offset = bank.offset + slot * bank.stride
        raw = rom[offset:offset + bank.stride]
        if style != "current" and slot in targets:
            character = targets[slot]
            key = (style, character)
            if key not in candidate_cache:
                candidate_cache[key] = (target_glyphs(character, fonts)[1] if style == "full"
                                        else encode_glyph(fonts[10 if style == "native" else "alternative"],
                                                          character, bank))
            raw = candidate_cache[key]
        assert len(raw) == bank.stride and any(raw)
        mask = Image.new("1", (bank.width, bank.rows))
        mask.putdata([bool(raw[row * 2 + column // 8] & (128 >> (column % 8)))
                      for row in range(bank.rows) for column in range(bank.width)])
        return mask

    def literal(text):
        return [slots[character] for character in text]

    def from_rom(offset):
        start, end = next(character_runs(rom, offset, offset + 0x100))
        assert start == offset
        return [slot for slot, in struct.iter_unpack("<H", rom[start:end])]

    samples = [
        ("item-1", from_rom(0xD21E3E), (96, 31), 208),
        ("item-2", from_rom(0xD219AC), (96, 48), 200),
        ("item-3", from_rom(0xD218C2), (96, 65), 208),
        ("description", from_rom(0xD23B5E), (11, 94), 229),
        ("battle", literal("擋住了去路"), (11, 137), 229),
    ]
    variants = (
        ("current", "current", 10, "A  目前：原版與繁化字形混用"),
        ("unified", "full", 10, "B  統一像素字體：維持目前字距"),
        ("spaced", "full", 11, "C  統一像素字體：字距增加 1 像素"),
    )
    if typefaces:
        variants = (
            ("fusion-full", "full", 11, "C1  Fusion Pixel 滿格版（10×10 字形，原 C）"),
            ("fusion-native", "native", 11, "C2  Fusion Pixel 原生版（9×9 字形，較小）"),
        )
        if alternative_font is not None:
            variants += (("ark-native", "alternative", 11, "C3  Ark Pixel 10px 原生版"),)
        if raster_font is not None:
            variants += (("song-mono", "song", 11, "C3  繁體宋體粗體（10px 單色化，非原生像素）"),)
    output_dir.mkdir(parents=True, exist_ok=False)
    checks = []
    panels = []
    rendered = []
    heading_font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 22)
    label_font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 9)
    for name, style, advance, heading in variants:
        image = Image.new("RGB", (240, 160), "#171a18")
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((3, 24, 82, 79), fill="#e4ecdf")
        drawing.rectangle((88, 24, 237, 79), fill="#e4ecdf")
        drawing.rectangle((3, 87, 237, 112), fill="#e4ecdf")
        drawing.rectangle((3, 130, 237, 155), fill="#e4ecdf")
        drawing.text((6, 8), "商店文字樣本", font=label_font, fill="#ffffff")
        drawing.text((6, 117), "戰鬥訊息樣本", font=label_font, fill="#ffffff")
        for row, text in enumerate(("買", "賣", "取消")):
            for column, slot in enumerate(literal(text)):
                image.paste("#101612", (20 + column * advance, 31 + row * 17), bitmap(slot, style))
        for row, price in enumerate(("100", "1000", "540")):
            drawing.text((231, 31 + row * 17), price, font=label_font,
                         fill="#101612", anchor="rt")
        for sample_name, glyphs, (left, top), limit in samples:
            right = left + (len(glyphs) - 1) * advance + bank.width
            assert right <= limit, (name, sample_name, right, limit)
            for column, slot in enumerate(glyphs):
                image.paste("#101612", (left + column * advance, top), bitmap(slot, style))
            checks.append({"variant": name, "sample": sample_name,
                           "text": "".join(targets.get(slot, f"<{slot:04x}>") for slot in glyphs),
                           "right": right, "limit": limit})
        enlarged = image.resize((720, 480), Image.Resampling.NEAREST)
        rendered.append(image.tobytes())
        enlarged.save(output_dir / f"{name}.png")
        panel = Image.new("RGB", (744, 532), "#f3f4f2")
        ImageDraw.Draw(panel).text((12, 10), heading, font=heading_font, fill="#17251d")
        panel.paste(enlarged, (12, 40))
        panels.append(panel)
    assert len(set(rendered)) == len(rendered), "Two preview choices render identically"
    comparison = Image.new("RGB", (744, 532 * len(panels)))
    for index, panel in enumerate(panels):
        comparison.paste(panel, (0, index * 532))
    comparison.save(output_dir / "comparison.png")
    validation = {
        "rom_sha256": report["output_sha256"],
        "glyph_size": [bank.width, bank.rows],
        "scale": 3,
        "mockup_only": True,
        "rom_and_device_modified": False,
        "current_advance_assumed_from_screenshot": 10,
        "candidate_advance": 11,
        "mode": "typefaces" if typefaces else "spacing",
        "variants": [{"name": name, "style": style, "advance": advance, "label": heading}
                 for name, style, advance, heading in variants],
        "alternative_font_sha256": (sha256(alternative_font.read_bytes()).hexdigest()
                        if alternative_font is not None else None),
        "raster_font_sha256": (sha256(raster_font.read_bytes()).hexdigest()
                       if raster_font is not None else None),
        "raster_face": raster.getname() if raster is not None else None,
        "sample_bounds": checks,
    }
    (output_dir / "validation.json").write_text(
        json.dumps(validation, indent=2, ensure_ascii=False) + "\n")
    assert sha256((build_dir / "mother3-zh-Hant-reviewed.gba").read_bytes()).hexdigest() == report["output_sha256"]
    print(json.dumps(validation, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build/reviewed-20261021-text-fix-2")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build/font-spacing-preview")
    parser.add_argument("--typefaces", action="store_true")
    parser.add_argument("--alternative-font", type=Path)
    parser.add_argument("--raster-font", type=Path)
    arguments = parser.parse_args()
    preview(arguments.build_dir, arguments.output_dir, arguments.typefaces,
            arguments.alternative_font, arguments.raster_font)


if __name__ == "__main__":
    main()