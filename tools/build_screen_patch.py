"""Decode and replace verified Mother 3 screen graphics."""

import struct

from PIL import Image, ImageDraw, ImageFont, ImageOps


SCREEN_TABLE = 0x1C5FD2C
TITLE_TABLE = 0x1BCDD8C
WELCOME_TABLE = 0x1C8DE98
WELCOME_RELOCATED = 0x1F90000
RELEASE_VERSION = "2026.10.22.1"
MISC_TABLE = 0x1AF3790
CHAPTER_RELOCATED = 0x1F94000
CHAPTER_TITLES = (
    "\u5f14\u5501\u4e4b\u591c", "\u5c0f\u5077\u6b77\u96aa", "\u5947\u602a\u65c5\u884c\u5546\u4eba",
    "\u5947\u5947\u5e03\u7684\u5287\u5834", "\u96f7\u4e4b\u5854", "\u5411\u65e5\u8475\u7684\u9ad8\u539f",
    "\u4e03\u6839\u91dd", "\u4e00\u5207 \u4e00\u5207",
)


def padding_record(rom, offset, data):
    if not 0x1F86008 <= offset < offset + len(data) <= 0x1FE0000:
        raise ValueError("Replacement exceeds verified padding")
    if rom[offset:offset + len(data)] != b"\xff" * len(data):
        raise ValueError("Replacement space is not unused padding")
    return offset, data


def table_entries(rom, base):
    count = struct.unpack_from("<I", rom, base)[0]
    if not 0 < count < 4096:
        raise ValueError("Invalid offset table count")
    offsets = struct.unpack_from(f"<{count + 1}I", rom, base + 4)
    if any(start > end for start, end in zip(offsets, offsets[1:])):
        raise ValueError("Offset table is not monotonic")
    if offsets[0] < (count + 2) * 4 or base + offsets[-1] > len(rom):
        raise ValueError("Offset table exceeds its bounds")
    return [(base + start, base + end) for start, end in zip(offsets, offsets[1:])]


def decompress(data):
    if len(data) < 4 or data[0] != 0x10:
        raise ValueError("Expected GBA LZ77 header")
    size = int.from_bytes(data[1:4], "little")
    if not 0 < size <= 0x18000:
        raise ValueError("Unexpected decompressed graphics size")
    output = bytearray()
    position = 4
    try:
        while len(output) < size:
            flags = data[position]
            position += 1
            for bit in range(7, -1, -1):
                if len(output) == size:
                    break
                if flags & (1 << bit):
                    first, second = data[position:position + 2]
                    position += 2
                    length = (first >> 4) + 3
                    distance = ((first & 15) << 8 | second) + 1
                    if distance > len(output) or len(output) + length > size:
                        raise ValueError("Invalid LZ77 back-reference")
                    for _ in range(length):
                        output.append(output[-distance])
                else:
                    output.append(data[position])
                    position += 1
    except (IndexError, ValueError) as error:
        raise ValueError("Invalid or truncated LZ77 data") from error
    return bytes(output)


def render(tiles, palette, tilemap, depth=4):
    if depth not in (4, 8) or len(tilemap) != 0x800:
        raise ValueError("Unsupported tilemap format")
    colors = [tuple(((value >> shift) & 31) * 255 // 31 for shift in (0, 5, 10))
              for (value,) in struct.iter_unpack("<H", palette)]
    image = Image.new("RGB", (256, 256))
    pixels = image.load()
    for index, (entry,) in enumerate(struct.iter_unpack("<H", tilemap)):
        tile = entry & 1023
        for row in range(8):
            for column in range(8):
                source_row = 7 - row if entry & 0x800 else row
                source_column = 7 - column if entry & 0x400 else column
                position = tile * 8 * depth + source_row * depth + source_column * depth // 8
                value = tiles[position]
                if depth == 4:
                    value = ((value >> (4 * (source_column % 2))) & 15) + (entry >> 12) * 16
                pixels[(index % 32) * 8 + column, (index // 32) * 8 + row] = colors[value]
    return image


def compress(data):
    output = bytearray(b"\x10" + len(data).to_bytes(3, "little"))
    position = 0
    while position < len(data):
        flag_position = len(output)
        output.append(0)
        for bit in range(7, -1, -1):
            if position == len(data):
                break
            for length in range(min(18, len(data) - position), 2, -1):
                previous = data.rfind(data[position:position + length],
                                      max(0, position - 4096), position + length - 2)
                if previous >= 0:
                    distance = position - previous - 1
                    output[flag_position] |= 1 << bit
                    output.extend((((length - 3) << 4) | (distance >> 8), distance & 255))
                    position += length
                    break
            else:
                output.append(data[position])
                position += 1
    output.extend(bytes(-len(output) % 4))
    if decompress(output) != data:
        raise AssertionError("LZ77 round-trip failed")
    return bytes(output)


def encode_image(image, tile_capacity):
    if image.mode != "P" or image.size != (256, 256):
        raise ValueError("Expected a 256x256 indexed image")
    pixels = image.load()
    tiles = bytearray()
    unique = {}
    tilemap = bytearray()
    for tile_row in range(32):
        for tile_column in range(32):
            tile = bytes(
                pixels[tile_column * 8 + column, tile_row * 8 + row]
                | pixels[tile_column * 8 + column + 1, tile_row * 8 + row] << 4
                for row in range(8) for column in range(0, 8, 2)
            )
            if tile not in unique:
                unique[tile] = len(unique)
                tiles.extend(tile)
            tilemap.extend(struct.pack("<H", unique[tile]))
    if len(unique) > tile_capacity:
        raise ValueError("Image exceeds the existing tile capacity")
    tiles.extend(bytes(tile_capacity * 32 - len(tiles)))
    return bytes(tiles), bytes(tilemap)


def welcome_records(rom, logo_path):
    entries = table_entries(rom, WELCOME_TABLE)
    if len(entries) != 5:
        raise ValueError("Unexpected welcome resource table")
    original_tiles = decompress(rom[slice(*entries[0])])
    with Image.open(logo_path) as source:
        source = source.convert("RGBA")
        source = ImageOps.contain(source, (128, 128), Image.Resampling.LANCZOS)
        flattened = Image.new("RGBA", source.size, "white")
        flattened.alpha_composite(source)
        reduced = flattened.convert("RGB").quantize(colors=12, dither=Image.Dither.NONE)
    palette = [255, 255, 255, 0, 0, 0] + reduced.getpalette()[:36]
    palette += [255, 255, 255, 165, 165, 165]
    canvas = Image.new("P", (256, 256), 14)
    canvas.putpalette(palette + [0] * (768 - len(palette)))
    logo = reduced.point(lambda value: value + 2)
    canvas.paste(logo, ((240 - logo.width) // 2, (128 - logo.height) // 2))
    draw = ImageDraw.Draw(canvas)
    draw.fontmode = "1"
    label = f"v{RELEASE_VERSION}"
    font = ImageFont.load_default()
    left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
    if right - left > 224 or bottom - top > 8:
        raise ValueError("Welcome version label exceeds its reserved area")
    draw.text(((240 - (right - left)) // 2 - left, 132 - top), label,
              font=font, fill=1)
    tiles, tilemap = encode_image(canvas, len(original_tiles) // 32)
    palette_bytes = b"".join(struct.pack("<H", sum(
        (palette[index + channel] >> 3) << (channel * 5) for channel in range(3)
    )) for index in range(0, 48, 3))
    resources = [compress(tiles), palette_bytes, compress(tilemap),
                 rom[slice(*entries[3])], rom[slice(*entries[4])]]
    container = bytearray(struct.pack("<I", len(resources)) + bytes((len(resources) + 1) * 4))
    for index, resource in enumerate(resources):
        struct.pack_into("<I", container, 4 + index * 4, len(container))
        container.extend(resource)
    struct.pack_into("<I", container, 4 + len(resources) * 4, len(container))
    if struct.unpack_from("<I", rom, 0x5A61C)[0] != 0x08000000 + WELCOME_TABLE:
        raise ValueError("Welcome loader pointer differs from the verified ROM")
    records = [padding_record(rom, WELCOME_RELOCATED, bytes(container)),
               (0x5A61C, struct.pack("<I", 0x08000000 + WELCOME_RELOCATED))]
    preview = render(tiles, palette_bytes, tilemap).crop((0, 0, 240, 160))
    return records, preview


def chapter_records(rom, font_path):
    entries = table_entries(rom, MISC_TABLE)
    original_tiles = decompress(rom[slice(*entries[44])])
    palette = rom[slice(*entries[45])]
    shades = [((value & 31) * 255 // 31)
              for (value,) in struct.iter_unpack("<H", palette[:32])]
    font = ImageFont.truetype(str(font_path), 56)
    tiles = bytearray()
    unique = {}
    maps = []
    previews = []
    for chapter in range(8):
        original_map = rom[slice(*entries[46 + chapter])]
        original = render(original_tiles, palette, original_map)
        image = original.convert("L")
        image.paste(0, (0, 72, 240, 88))
        title = CHAPTER_TITLES[chapter]
        left = (240 - sum(11 if character == " " else 16 for character in title)) // 2
        for character in title:
            if character == " ":
                left += 11
                continue
            glyph = Image.new("L", (64, 64))
            draw = ImageDraw.Draw(glyph)
            bounds = draw.textbbox((4, 52), character, font=font, anchor="ls", stroke_width=2)
            if min(bounds[:2]) < 0 or max(bounds[2:]) > 64:
                raise ValueError("Chapter glyph exceeds its cell")
            draw.text((4, 52), character, font=font, fill=255, anchor="ls",
                      stroke_width=2, stroke_fill=255)
            glyph = glyph.resize((16, 16), Image.Resampling.LANCZOS)
            image.paste(glyph, (left, 72))
            left += 16
        indexed = Image.new("P", image.size)
        indexed.putdata([min(range(16), key=lambda index: abs(shades[index] - value))
                         for value in image.tobytes()])
        local_tiles, local_map = encode_image(indexed, len(original_tiles) // 32)
        mapping = bytearray()
        for (local_index,) in struct.iter_unpack("<H", local_map):
            tile = local_tiles[local_index * 32:(local_index + 1) * 32]
            if tile not in unique:
                unique[tile] = len(unique)
                tiles.extend(tile)
            mapping.extend(struct.pack("<H", unique[tile]))
        maps.append(bytes(mapping))
        preview = render(bytes(tiles), palette, maps[-1])
        if preview.crop((0, 0, 256, 72)).tobytes() != original.crop((0, 0, 256, 72)).tobytes():
            raise AssertionError("Chapter number line changed")
        previews.append(preview.crop((0, 0, 240, 160)))
    if len(tiles) > len(original_tiles):
        raise ValueError("Chapter tiles exceed the existing VRAM allocation")
    tiles.extend(bytes(len(original_tiles) - len(tiles)))
    compressed = compress(tiles)
    records = [padding_record(rom, CHAPTER_RELOCATED, compressed),
               (MISC_TABLE + 4 + 44 * 4, struct.pack("<I", CHAPTER_RELOCATED - MISC_TABLE))]
    records.extend((entries[46 + chapter][0], mapping) for chapter, mapping in enumerate(maps))
    return records, previews