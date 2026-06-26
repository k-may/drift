import copy
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


# ---------- Settings ----------
base_dir = Path(__file__).resolve().parent
svg_folder_path = base_dir / "freestyle_scaled_svg"
output_dir = base_dir / "print_ready_slices"

artboard_width_mm = 297.0
artboard_height_mm = 420.0
margin_mm = 10.0
spacing_mm = 2.0
allow_rotation = True

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
ET.register_namespace("", SVG_NS)
ET.register_namespace("inkscape", INKSCAPE_NS)


@dataclass
class Shape:
    path: Path
    width: float
    height: float
    root: ET.Element


@dataclass
class Rect:
    x: float
    y: float
    width: float
    height: float


@dataclass
class Placement:
    shape: Shape
    x: float
    y: float
    rotated: bool


def length_to_mm(value):
    match = re.fullmatch(
        r"\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*"
        r"(mm|cm|in)\s*",
        value,
    )
    if not match:
        raise ValueError(
            f"Expected an explicit physical SVG length in mm, cm, or in: {value!r}"
        )

    number = float(match.group(1))
    unit = match.group(2)
    return number * {"mm": 1.0, "cm": 10.0, "in": 25.4}[unit]


def load_shapes(folder):
    shapes = []
    for path in sorted(folder.glob("*.svg")):
        root = ET.parse(path).getroot()
        width = length_to_mm(root.get("width", ""))
        height = length_to_mm(root.get("height", ""))
        shapes.append(Shape(path, width, height, root))

    if not shapes:
        raise RuntimeError(f"No SVG files found in {folder}")

    # Larger shapes first generally produces fewer pages.
    return sorted(shapes, key=lambda shape: shape.width * shape.height, reverse=True)


def intersects(a, b):
    return not (
        a.x >= b.x + b.width
        or a.x + a.width <= b.x
        or a.y >= b.y + b.height
        or a.y + a.height <= b.y
    )


def contains(outer, inner):
    return (
        inner.x >= outer.x
        and inner.y >= outer.y
        and inner.x + inner.width <= outer.x + outer.width
        and inner.y + inner.height <= outer.y + outer.height
    )


def split_free_rect(free_rect, used):
    if not intersects(free_rect, used):
        return [free_rect]

    pieces = []
    if used.x > free_rect.x:
        pieces.append(
            Rect(free_rect.x, free_rect.y, used.x - free_rect.x, free_rect.height)
        )
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
        pieces.append(
            Rect(free_rect.x, free_rect.y, free_rect.width, used.y - free_rect.y)
        )
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


def prune_free_rects(rects):
    return [
        rect
        for index, rect in enumerate(rects)
        if not any(
            index != other_index and contains(other, rect)
            for other_index, other in enumerate(rects)
        )
    ]


def find_position(free_rects, shape):
    candidates = [(shape.width, shape.height, False)]
    if allow_rotation and shape.width != shape.height:
        candidates.append((shape.height, shape.width, True))

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
                best = (
                    score,
                    Rect(free_rect.x, free_rect.y, packed_width, packed_height),
                    rotated,
                )
    return best


def place_on_page(shape, free_rects):
    result = find_position(free_rects, shape)
    if result is None:
        return None

    _, used, rotated = result
    split_rects = []
    for free_rect in free_rects:
        split_rects.extend(split_free_rect(free_rect, used))
    free_rects[:] = prune_free_rects(split_rects)

    return Placement(shape, used.x, used.y, rotated)


def pack_pages(shapes):
    printable_width = artboard_width_mm - (2 * margin_mm)
    printable_height = artboard_height_mm - (2 * margin_mm)
    if printable_width <= 0 or printable_height <= 0:
        raise ValueError("The artboard margin leaves no printable area.")

    # One extra spacing allowance removes spacing from the outer right/bottom edge.
    bin_width = printable_width + spacing_mm
    bin_height = printable_height + spacing_mm
    pages = []

    for shape in shapes:
        placement = None
        for placements, free_rects in pages:
            placement = place_on_page(shape, free_rects)
            if placement:
                placements.append(placement)
                break

        if placement:
            continue

        free_rects = [Rect(0, 0, bin_width, bin_height)]
        placement = place_on_page(shape, free_rects)
        if placement is None:
            raise ValueError(
                f"{shape.path.name} is {shape.width:.2f} x {shape.height:.2f} mm "
                "and cannot fit on the printable A3 area at 100% scale."
            )
        pages.append(([placement], free_rects))

    return [placements for placements, _ in pages]


def append_shape(page_root, placement):
    shape = placement.shape
    group = ET.SubElement(
        page_root,
        f"{{{SVG_NS}}}g",
        {
            f"{{{INKSCAPE_NS}}}label": shape.path.stem,
            "data-source": shape.path.name,
        },
    )

    x = margin_mm + placement.x
    y = margin_mm + placement.y
    if placement.rotated:
        group.set("transform", f"translate({x + shape.height} {y}) rotate(90)")
    else:
        group.set("transform", f"translate({x} {y})")

    nested = ET.SubElement(
        group,
        f"{{{SVG_NS}}}svg",
        {
            "x": "0",
            "y": "0",
            "width": f"{shape.width}mm",
            "height": f"{shape.height}mm",
            "viewBox": shape.root.get("viewBox"),
            "preserveAspectRatio": shape.root.get("preserveAspectRatio", "none"),
        },
    )
    for child in shape.root:
        nested.append(copy.deepcopy(child))


def write_page(page_number, placements):
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "version": "1.1",
            "width": f"{artboard_width_mm}mm",
            "height": f"{artboard_height_mm}mm",
            "viewBox": f"0 0 {artboard_width_mm} {artboard_height_mm}",
        },
    )
    for placement in placements:
        append_shape(root, placement)

    path = output_dir / f"a3_page_{page_number:02d}.svg"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def main():
    shapes = load_shapes(svg_folder_path)
    pages = pack_pages(shapes)
    output_dir.mkdir(parents=True, exist_ok=True)

    for old_page in output_dir.glob("a3_page_*.svg"):
        old_page.unlink()

    for page_number, placements in enumerate(pages, start=1):
        path = write_page(page_number, placements)
        print(f"{path.name}: {len(placements)} shape(s)")
        for placement in placements:
            orientation = "rotated 90 degrees" if placement.rotated else "unrotated"
            print(
                f"  {placement.shape.path.name}: "
                f"x={margin_mm + placement.x:.2f}mm, "
                f"y={margin_mm + placement.y:.2f}mm, {orientation}"
            )

    print(f"Created {len(pages)} A3 page(s) in {output_dir}")


if __name__ == "__main__":
    main()
