from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
FAVICONS = STATIC / "favicons"
ICONS = STATIC / "icons"

TEAL = (15, 118, 110, 255)
MINT = (45, 212, 191, 255)
INK = (18, 35, 44, 255)
CREAM = (248, 250, 252, 255)
AMBER = (245, 158, 11, 255)


def rounded_gradient(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gradient = Image.new("RGBA", (size, size))
    pixels = gradient.load()
    for y in range(size):
        for x in range(size):
            t = (x * 0.42 + y * 0.58) / size
            r = int(TEAL[0] * (1 - t) + MINT[0] * t)
            g = int(TEAL[1] * (1 - t) + MINT[1] * t)
            b = int(TEAL[2] * (1 - t) + MINT[2] * t)
            pixels[x, y] = (r, g, b, 255)
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=255)
    image.alpha_composite(gradient)
    image.putalpha(mask)
    return image


def draw_mark(size: int = 1024) -> Image.Image:
    scale = size / 1024
    image = rounded_gradient(size)
    draw = ImageDraw.Draw(image)

    def box(values: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return tuple(int(value * scale) for value in values)

    def points(values: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [(int(x * scale), int(y * scale)) for x, y in values]

    line = max(10, int(36 * scale))
    shadow = (7, 58, 55, 92)

    draw.rounded_rectangle(box((248, 248, 776, 785)), radius=int(250 * scale), fill=shadow)
    draw.rectangle(box((248, 468, 776, 754)), fill=shadow)
    draw.polygon(points([(206, 742), (818, 742), (744, 858), (280, 858)]), fill=shadow)

    draw.rounded_rectangle(box((235, 230, 789, 766)), radius=int(252 * scale), fill=CREAM)
    draw.rectangle(box((235, 450, 789, 738)), fill=CREAM)
    draw.polygon(points([(190, 726), (834, 726), (760, 846), (264, 846)]), fill=CREAM)
    draw.rounded_rectangle(box((438, 132, 586, 282)), radius=int(74 * scale), fill=CREAM)
    draw.rounded_rectangle(box((474, 106, 550, 182)), radius=int(38 * scale), fill=CREAM)

    draw.arc(box((78, 272, 394, 694)), start=112, end=248, fill=CREAM, width=line)
    draw.arc(box((630, 272, 946, 694)), start=-68, end=68, fill=CREAM, width=line)
    draw.arc(box((146, 346, 402, 622)), start=120, end=238, fill=AMBER, width=max(8, int(26 * scale)))
    draw.arc(box((622, 346, 878, 622)), start=-58, end=58, fill=AMBER, width=max(8, int(26 * scale)))

    draw.ellipse(box((452, 782, 572, 902)), fill=AMBER)
    draw.rounded_rectangle(box((394, 654, 630, 706)), radius=int(26 * scale), fill=INK)
    draw.ellipse(box((706, 182, 762, 238)), fill=AMBER)
    draw.line(points([(638, 244), (720, 210)]), fill=AMBER, width=max(7, int(18 * scale)))
    return image


def write_svg() -> None:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" role="img" aria-label="PiBells logo">
  <defs>
    <linearGradient id="bg" x1="120" y1="80" x2="900" y2="940" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#0f766e"/>
      <stop offset="1" stop-color="#2dd4bf"/>
    </linearGradient>
  </defs>
  <rect width="1024" height="1024" rx="210" fill="url(#bg)"/>
  <path d="M248 248c0-76 62-138 138-138h252c76 0 138 62 138 138v210h18v286l40 82H190l40-82V458h18V248Z" fill="#f8fafc"/>
  <path d="M438 132h148v150H438z" rx="74" fill="#f8fafc"/>
  <path d="M474 106h76v76h-76z" rx="38" fill="#f8fafc"/>
  <path d="M394 654h236v52H394z" rx="26" fill="#12232c"/>
  <circle cx="512" cy="842" r="60" fill="#f59e0b"/>
  <path d="M252 668a220 220 0 0 1 0-312" fill="none" stroke="#f8fafc" stroke-width="36" stroke-linecap="round"/>
  <path d="M772 356a220 220 0 0 1 0 312" fill="none" stroke="#f8fafc" stroke-width="36" stroke-linecap="round"/>
  <path d="M315 610a140 140 0 0 1 0-196" fill="none" stroke="#f59e0b" stroke-width="26" stroke-linecap="round"/>
  <path d="M709 414a140 140 0 0 1 0 196" fill="none" stroke="#f59e0b" stroke-width="26" stroke-linecap="round"/>
  <circle cx="734" cy="210" r="28" fill="#f59e0b"/>
  <path d="M638 244l82-34" fill="none" stroke="#f59e0b" stroke-width="18" stroke-linecap="round"/>
</svg>
"""
    (STATIC / "logo.svg").write_text(svg)
    (FAVICONS / "favicon.svg").write_text(svg)

    pinned = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <path d="M0 210C0 94 94 0 210 0h604c116 0 210 94 210 210v604c0 116-94 210-210 210H210C94 1024 0 930 0 814V210Zm248 38v210h-18v286l-40 82h262a60 60 0 0 0 120 0h262l-40-82V458h-18V248c0-76-62-138-138-138H386c-76 0-138 62-138 138Zm146 406v52h236v-52H394Z"/>
</svg>
"""
    (FAVICONS / "safari-pinned-tab.svg").write_text(pinned)


def save_resized(base: Image.Image, path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    base.resize((size, size), Image.Resampling.LANCZOS).save(path)


def main() -> None:
    FAVICONS.mkdir(parents=True, exist_ok=True)
    ICONS.mkdir(parents=True, exist_ok=True)
    base = draw_mark()

    save_resized(base, STATIC / "pibells-logo.png", 512)
    save_resized(base, STATIC / "smallroundlogo.png", 512)
    save_resized(base, ICONS / "icon-192.png", 192)
    save_resized(base, ICONS / "icon-512.png", 512)
    save_resized(base, ICONS / "icon-1024.png", 1024)
    save_resized(base, FAVICONS / "favicon-16x16.png", 16)
    save_resized(base, FAVICONS / "favicon-32x32.png", 32)
    save_resized(base, FAVICONS / "favicon-48x48.png", 48)
    save_resized(base, FAVICONS / "apple-touch-icon.png", 180)
    save_resized(base, FAVICONS / "mstile-150x150.png", 150)
    base.save(
        FAVICONS / "favicon.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    write_svg()


if __name__ == "__main__":
    main()
