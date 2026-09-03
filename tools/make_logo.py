"""Generate the project icon and logo.

Kept in the repo so the artwork can be regenerated rather than being an opaque
binary. Run: python tools/make_logo.py

The mark is an antenna over a square wave: this integration is about the pulse
train itself rather than any decoded protocol.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "custom_components" / "rfxcom_commands" / "brand"

BLUE = (3, 169, 244)
WHITE = (255, 255, 255)
# Near-black for the light-theme plate, lifted for the dark one so the plate
# still has an edge against a dark surface.
PLATE = ((28, 34, 43), (17, 21, 27))
PLATE_DARK = ((58, 69, 84), (38, 46, 57))

SS = 4  # supersample factor, for smooth curves without antialiasing tricks


def _plate(size: int, palette: tuple[tuple[int, int, int], tuple[int, int, int]]) -> Image.Image:
    """Vertical gradient clipped to a rounded square."""
    top, bottom = palette
    gradient = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(gradient)
    for y in range(size):
        t = y / max(size - 1, 1)
        colour = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
        draw.line([(0, y), (size, y)], fill=(*colour, 255))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255
    )

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    image.paste(gradient, (0, 0), mask)
    return image


def _antenna(draw: ImageDraw.ImageDraw, size: int) -> None:
    """A mast with two radiating arcs on each side."""
    cx = size / 2
    mast_top = size * 0.20
    mast_bottom = size * 0.56
    width = size * 0.045

    draw.rounded_rectangle(
        [cx - width / 2, mast_top, cx + width / 2, mast_bottom],
        radius=width / 2,
        fill=(*WHITE, 255),
    )
    # Base, so the mast does not float above the wave.
    foot = size * 0.11
    draw.polygon(
        [
            (cx - foot, mast_bottom),
            (cx + foot, mast_bottom),
            (cx + width / 2, mast_bottom - size * 0.12),
            (cx - width / 2, mast_bottom - size * 0.12),
        ],
        fill=(*WHITE, 255),
    )

    cy = mast_top + size * 0.04
    for index, radius in enumerate((size * 0.14, size * 0.23)):
        thickness = int(size * (0.032 - index * 0.006))
        box = [cx - radius, cy - radius, cx + radius, cy + radius]
        for start, end in ((205, 245), (295, 335)):
            draw.arc(box, start=start, end=end, fill=(*BLUE, 255), width=thickness)


def _pulse_train(draw: ImageDraw.ImageDraw, size: int) -> None:
    """A square wave along the bottom, short and long marks as the RF carries."""
    low = size * 0.80
    high = size * 0.64
    left = size * 0.13
    right = size * 0.87
    thickness = max(int(size * 0.035), 1)

    # Mirrors the 1:3 ratio of a real OOK remote.
    pattern = [1, 3, 1, 3, 3, 1, 1, 3, 3, 1]
    unit = (right - left) / sum(pattern)

    x = left
    y = low
    points = [(x, y)]
    for index, width in enumerate(pattern):
        target = high if index % 2 == 0 else low
        if target != y:
            points.append((x, target))
            y = target
        x += width * unit
        points.append((x, y))
    if y != low:
        points.append((x, low))

    draw.line(points, fill=(*BLUE, 255), width=thickness, joint="curve")


def _icon(size: int, palette) -> Image.Image:
    canvas = size * SS
    image = _plate(canvas, palette)
    draw = ImageDraw.Draw(image)
    _antenna(draw, canvas)
    _pulse_train(draw, canvas)
    return image.resize((size, size), Image.LANCZOS)


def _logo(width: int, height: int, palette) -> Image.Image:
    """The icon centred on a transparent canvas; HA logos may be wider."""
    icon = _icon(height, palette)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    image.paste(icon, ((width - height) // 2, 0), icon)
    return image


def main() -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    for name, palette in (("", PLATE), ("dark_", PLATE_DARK)):
        _icon(256, palette).save(BRAND / f"{name}icon.png")
        _icon(512, palette).save(BRAND / f"{name}icon@2x.png")
        _logo(256, 256, palette).save(BRAND / f"{name}logo.png")
        _logo(512, 512, palette).save(BRAND / f"{name}logo@2x.png")

    # The README header image.
    _logo(512, 256, PLATE).save(ROOT / "logo.png")
    print(f"Written to {BRAND} and {ROOT / 'logo.png'}")


if __name__ == "__main__":
    main()
