import argparse
import copy
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"

ET.register_namespace("", SVG_NS)
ET.register_namespace("inkscape", INKSCAPE_NS)

PAPER_SIZES_MM = {
    "a5": (148.0, 210.0),
    "a4": (210.0, 297.0),
    "a3": (297.0, 420.0),
    "a2": (420.0, 594.0),
    "a1": (594.0, 841.0),
    "a0": (841.0, 1189.0),
}


@dataclass(frozen=True)
class Shape:
    path: Path
    width_mm: float
    height_mm: float
    root: ET.Element


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Placement:
    shape: Shape
    x: float
    y: float
    rotated: bool


def length_to_mm(value: str) -> float:
    match = re.fullmatch(
        r"\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*"
        r"(mm|cm|in|px)?\s*",
        value or "",
    )
    if not match:
        raise ValueError(f"Cannot parse SVG length: {value!r}")

    number = float(match.group(1))
    unit = match.group(2) or "px"
    if unit == "px":
        return number * 25.4 / 96.0
    return number * {"mm": 1.0, "cm": 10.0, "in": 25.4}[unit]


def natural_sort_key(path: Path):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path.name)
    ]


def load_shapes(input_dir: Path, glob_pattern: str) -> list[Shape]:
    shapes = []
    for path in sorted(input_dir.glob(glob_pattern), key=natural_sort_key):
        root = ET.parse(path).getroot()
        width = length_to_mm(root.get("width", ""))
        height = length_to_mm(root.get("height", ""))
        shapes.append(Shape(path, width, height, root))

    if not shapes:
        raise RuntimeError(f"No SVG files found in {input_dir} matching {glob_pattern!r}")

    return sorted(shapes, key=lambda shape: shape.width_mm * shape.height_mm, reverse=True)


def intersects(a: Rect, b: Rect) -> bool:
    return not (
        a.x >= b.x + b.width
        or a.x + a.width <= b.x
        or a.y >= b.y + b.height
        or a.y + a.height <= b.y
    )


def contains(outer: Rect, inner: Rect) -> bool:
    return (
        inner.x >= outer.x
        and inner.y >= outer.y
        and inner.x + inner.width <= outer.x + outer.width
        and inner.y + inner.height <= outer.y + outer.height
    )


def split_free_rect(free_rect: Rect, used: Rect) -> list[Rect]:
    if not intersects(free_rect, used):
        return [free_rect]

    pieces = []
    if used.x > free_rect.x:
        pieces.append(Rect(free_rect.x, free_rect.y, used.x - free_rect.x, free_rect.height))
    if used.x + used.width < free_rect.x + free_rect.width:
        pieces.append(
            Rect(
                used.x + used.width,
                free_rect.y,
                free_rect.x + free_rect.width - used.x - used.width,
                free_rect.height,
            )
        )
    if used.y > free_rect.y:
        pieces.append(Rect(free_rect.x, free_rect.y, free_rect.width, used.y - free_rect.y))
    if used.y + used.height < free_rect.y + free_rect.height:
        pieces.append(
            Rect(
                free_rect.x,
                used.y + used.height,
                free_rect.width,
                free_rect.y + free_rect.height - used.y - used.height,
            )
        )
    return [piece for piece in pieces if piece.width > 0 and piece.height > 0]


def prune_free_rects(rects: list[Rect]) -> list[Rect]:
    return [
        rect
        for index, rect in enumerate(rects)
        if not any(
            index != other_index and contains(other, rect)
            for other_index, other in enumerate(rects)
        )
    ]


def find_position(
    free_rects: list[Rect],
    shape: Shape,
    spacing_mm: float,
    allow_rotation: bool,
) -> tuple[Rect, bool] | None:
    candidates = [(shape.width_mm, shape.height_mm, False)]
    if allow_rotation and shape.width_mm != shape.height_mm:
        candidates.append((shape.height_mm, shape.width_mm, True))

    best = None
    for free_rect in free_rects:
        for width, height, rotated in candidates:
            packed_width = width + spacing_mm
            packed_height = height + spacing_mm
            if packed_width > free_rect.width or packed_height > free_rect.height:
                continue

            leftover_x = free_rect.width - packed_width
            leftover_y = free_rect.height - packed_height
            score = (
                min(leftover_x, leftover_y),
                max(leftover_x, leftover_y),
                free_rect.y,
                free_rect.x,
            )
            if best is None or score < best[0]:
                best = (score, Rect(free_rect.x, free_rect.y, packed_width, packed_height), rotated)

    if best is None:
        return None
    return best[1], best[2]


def place_on_page(
    shape: Shape,
    free_rects: list[Rect],
    spacing_mm: float,
    allow_rotation: bool,
) -> Placement | None:
    result = find_position(free_rects, shape, spacing_mm, allow_rotation)
    if result is None:
        return None

    used, rotated = result
    split_rects = []
    for free_rect in free_rects:
        split_rects.extend(split_free_rect(free_rect, used))
    free_rects[:] = prune_free_rects(split_rects)

    return Placement(shape, used.x, used.y, rotated)


def pack_pages(
    shapes: list[Shape],
    artboard_width_mm: float,
    artboard_height_mm: float,
    margin_mm: float,
    spacing_mm: float,
    allow_rotation: bool,
) -> tuple[list[list[Placement]], list[Shape]]:
    printable_width = artboard_width_mm - (2 * margin_mm)
    printable_height = artboard_height_mm - (2 * margin_mm)
    if printable_width <= 0 or printable_height <= 0:
        raise ValueError("The margin leaves no printable area.")

    bin_width = printable_width + spacing_mm
    bin_height = printable_height + spacing_mm
    pages: list[tuple[list[Placement], list[Rect]]] = []
    skipped = []

    for shape in shapes:
        fits_upright = shape.width_mm <= printable_width and shape.height_mm <= printable_height
        fits_rotated = (
            allow_rotation
            and shape.height_mm <= printable_width
            and shape.width_mm <= printable_height
        )
        if not fits_upright and not fits_rotated:
            skipped.append(shape)
            continue

        placement = None
        for placements, free_rects in pages:
            placement = place_on_page(shape, free_rects, spacing_mm, allow_rotation)
            if placement is not None:
                placements.append(placement)
                break

        if placement is not None:
            continue

        free_rects = [Rect(0, 0, bin_width, bin_height)]
        placement = place_on_page(shape, free_rects, spacing_mm, allow_rotation)
        if placement is None:
            skipped.append(shape)
            continue
        pages.append(([placement], free_rects))

    return [placements for placements, _ in pages], skipped


def append_shape(page_root: ET.Element, placement: Placement, margin_mm: float) -> None:
    shape = placement.shape
    x = margin_mm + placement.x
    y = margin_mm + placement.y
    if placement.rotated:
        parent = ET.SubElement(
            page_root,
            f"{{{SVG_NS}}}g",
            {
                f"{{{INKSCAPE_NS}}}label": shape.path.stem,
                "data-source": shape.path.name,
                "transform": f"translate({x + shape.height_mm:.6f} {y:.6f}) rotate(90)",
            },
        )
        nested_x = 0.0
        nested_y = 0.0
    else:
        parent = page_root
        nested_x = x
        nested_y = y

    nested = ET.SubElement(
        parent,
        f"{{{SVG_NS}}}svg",
        {
            f"{{{INKSCAPE_NS}}}label": shape.path.stem,
            "data-source": shape.path.name,
            "x": f"{nested_x:.6f}",
            "y": f"{nested_y:.6f}",
            "width": f"{shape.width_mm:.6f}",
            "height": f"{shape.height_mm:.6f}",
            "viewBox": shape.root.get("viewBox", f"0 0 {shape.width_mm} {shape.height_mm}"),
            "preserveAspectRatio": shape.root.get("preserveAspectRatio", "none"),
        },
    )

    for child in shape.root:
        nested.append(copy.deepcopy(child))


def write_page(
    output_dir: Path,
    page_number: int,
    placements: list[Placement],
    artboard_width_mm: float,
    artboard_height_mm: float,
    margin_mm: float,
) -> Path:
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "version": "1.1",
            "width": f"{artboard_width_mm:.6f}mm",
            "height": f"{artboard_height_mm:.6f}mm",
            "viewBox": f"0 0 {artboard_width_mm:.6f} {artboard_height_mm:.6f}",
        },
    )
    for placement in placements:
        append_shape(root, placement, margin_mm)

    path = output_dir / f"a3_artboard_{page_number:02d}.svg"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def export_pdf(svg_path: Path, pdf_path: Path, inkscape_path: str = "inkscape") -> None:
    if shutil.which(inkscape_path) is None and not Path(inkscape_path).exists():
        raise RuntimeError("Inkscape was not found on PATH; cannot export PDFs.")

    subprocess.run(
        [
            inkscape_path,
            str(svg_path),
            "--export-type=pdf",
            f"--export-filename={pdf_path}",
        ],
        check=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pack SVG files into fixed-size SVG artboards without scaling."
    )
    parser.add_argument("--input", type=Path, default=Path("freestyle_scaled_svg"))
    parser.add_argument("--output", type=Path, default=Path("a3_artboards"))
    parser.add_argument("--glob", default="*.svg", help="Input SVG glob, e.g. 'Slice*_front.svg'.")
    parser.add_argument(
        "--paper",
        choices=sorted(PAPER_SIZES_MM),
        default="a3",
        help="Named paper size. Ignored when --width and --height are both set.",
    )
    parser.add_argument("--width", type=float, help="Custom artboard width in mm.")
    parser.add_argument("--height", type=float, help="Custom artboard height in mm.")
    parser.add_argument("--margin", type=float, default=10.0, help="Artboard margin in mm.")
    parser.add_argument("--spacing", type=float, default=2.0, help="Spacing between SVGs in mm.")
    parser.add_argument("--landscape", action="store_true", help="Swap artboard width and height.")
    parser.add_argument("--no-rotation", action="store_true", help="Do not rotate SVGs during packing.")
    parser.add_argument("--pdf", action="store_true", help="Also export each artboard to PDF with Inkscape.")
    parser.add_argument("--inkscape", default="inkscape", help="Path to Inkscape executable.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_dir = args.input.resolve()
    output_dir = args.output.resolve()
    if (args.width is None) != (args.height is None):
        raise ValueError("Use --width and --height together for a custom artboard size.")

    if args.width is not None and args.height is not None:
        artboard_width, artboard_height = args.width, args.height
    else:
        artboard_width, artboard_height = PAPER_SIZES_MM[args.paper]

    if args.landscape:
        artboard_width, artboard_height = artboard_height, artboard_width

    shapes = load_shapes(input_dir, args.glob)
    pages, skipped = pack_pages(
        shapes,
        artboard_width,
        artboard_height,
        args.margin,
        args.spacing,
        not args.no_rotation,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    written_pages = []
    for page_number, placements in enumerate(pages, start=1):
        path = write_page(
            output_dir,
            page_number,
            placements,
            artboard_width,
            artboard_height,
            args.margin,
        )
        written_pages.append(path)

        print(f"{path.name}: {len(placements)} SVG(s)")
        for placement in placements:
            orientation = "rotated" if placement.rotated else "upright"
            print(
                f"  {placement.shape.path.name}: "
                f"x={args.margin + placement.x:.2f}mm, "
                f"y={args.margin + placement.y:.2f}mm, "
                f"{orientation}"
            )

    if args.pdf:
        for svg_path in written_pages:
            export_pdf(svg_path, svg_path.with_suffix(".pdf"), args.inkscape)

    if skipped:
        skipped_path = output_dir / "skipped_oversize.txt"
        with skipped_path.open("w", encoding="utf-8") as handle:
            handle.write(
                f"Skipped SVGs too large for {artboard_width:.2f} x {artboard_height:.2f} mm "
                f"artboards with {args.margin:.2f} mm margins.\n"
            )
            handle.write(
                f"Printable area: {artboard_width - (2 * args.margin):.2f} x "
                f"{artboard_height - (2 * args.margin):.2f} mm\n\n"
            )
            for shape in skipped:
                handle.write(f"{shape.path.name}: {shape.width_mm:.2f} x {shape.height_mm:.2f} mm\n")

        print(f"Skipped {len(skipped)} oversize SVG(s); see {skipped_path}")

    print(
        f"Created {len(written_pages)} artboard SVG(s) "
        f"({artboard_width:.2f} x {artboard_height:.2f} mm) in {output_dir}"
    )


if __name__ == "__main__":
    main()
