from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
BRAND = STATIC / "brand"
FAVICONS = STATIC / "favicons"
ICONS = STATIC / "icons"

MARK_SOURCE = BRAND / "pibells-logo-mark-master.png"
STACKED_SOURCE = BRAND / "pibells-logo-stacked-master.png"
HORIZONTAL_SOURCE = BRAND / "pibells-logo-horizontal-master.png"

BLUE = "#2f80ed"


def load_trimmed(path: Path) -> Image.Image:
    image = Image.open(path).convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        raise ValueError(f"{path} has no visible pixels")
    return image.crop(bbox)


def square_icon(source: Image.Image, size: int, fill_ratio: float = 0.78) -> Image.Image:
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mark = source.copy()
    mark.thumbnail((int(size * fill_ratio), int(size * fill_ratio)), Image.Resampling.LANCZOS)
    icon.alpha_composite(mark, ((size - mark.width) // 2, (size - mark.height) // 2))
    return icon


def save_square(source: Image.Image, path: Path, size: int, fill_ratio: float = 0.78) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    square_icon(source, size, fill_ratio).save(path, optimize=True)


def save_fitted(source: Image.Image, path: Path, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    logo = source.copy()
    logo.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas.alpha_composite(logo, ((width - logo.width) // 2, (height - logo.height) // 2))
    canvas.save(path, optimize=True)


def write_png_svg(svg_path: Path, png_path: Path, label: str) -> None:
    encoded = base64.b64encode(png_path.read_bytes()).decode("ascii")
    svg_path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" role="img" aria-label="{label}">'
        f'<image width="1024" height="1024" href="data:image/png;base64,{encoded}"/>'
        "</svg>\n"
    )


def write_pinned_tab(source: Image.Image) -> None:
    target = FAVICONS / "safari-pinned-tab.svg"
    if shutil.which("potrace") is None:
        target.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
            '<path d="M512 92c186 0 337 151 337 337 0 169-125 309-287 333v86H462v-86c-162-24-287-164-287-333C175 243 326 92 512 92Z"/>'
            "</svg>\n"
        )
        return

    silhouette = square_icon(source, 1024, 0.84)
    alpha = silhouette.getchannel("A")
    bitmap = Image.new("1", alpha.size, 1)
    bitmap.paste(0, mask=alpha.point(lambda value: 255 if value > 20 else 0))

    with tempfile.TemporaryDirectory() as tmp:
        pbm_path = Path(tmp) / "pibells-mask.pbm"
        svg_path = Path(tmp) / "pibells-mask.svg"
        bitmap.save(pbm_path)
        subprocess.run(
            ["potrace", "--svg", "--turdsize", "8", "-o", str(svg_path), str(pbm_path)],
            check=True,
        )
        svg = svg_path.read_text()

    # Potrace emits a complete SVG; the fill is what Safari uses for mask-icon tinting.
    target.write_text(svg)


def main() -> None:
    FAVICONS.mkdir(parents=True, exist_ok=True)
    ICONS.mkdir(parents=True, exist_ok=True)

    mark = load_trimmed(MARK_SOURCE)
    stacked = load_trimmed(STACKED_SOURCE)
    horizontal = load_trimmed(HORIZONTAL_SOURCE)

    save_square(mark, STATIC / "pibells-logo.png", 1024)
    save_square(mark, STATIC / "smallroundlogo.png", 512)
    save_fitted(stacked, BRAND / "pibells-logo-stacked.png", 1600, 1600)
    save_fitted(horizontal, BRAND / "pibells-logo-horizontal.png", 2400, 640)

    save_square(mark, ICONS / "icon-192.png", 192)
    save_square(mark, ICONS / "icon-512.png", 512)
    save_square(mark, ICONS / "icon-1024.png", 1024)

    save_square(mark, FAVICONS / "favicon-16x16.png", 16, 0.88)
    save_square(mark, FAVICONS / "favicon-32x32.png", 32, 0.84)
    save_square(mark, FAVICONS / "favicon-48x48.png", 48, 0.82)
    save_square(mark, FAVICONS / "apple-touch-icon.png", 180, 0.76)
    save_square(mark, FAVICONS / "mstile-150x150.png", 150, 0.76)

    ico_base = square_icon(mark, 256, 0.82)
    ico_base.save(
        FAVICONS / "favicon.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    write_png_svg(STATIC / "logo.svg", STATIC / "pibells-logo.png", "PiBells logo")
    write_png_svg(FAVICONS / "favicon.svg", STATIC / "pibells-logo.png", "PiBells logo")
    write_pinned_tab(mark)

    print(f"Generated PiBells brand assets from {MARK_SOURCE}")
    print(f"Mask icon tint: {BLUE}")


if __name__ == "__main__":
    main()
