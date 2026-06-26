import bpy
import os
import re
import xml.etree.ElementTree as ET
from math import ceil
from mathutils import Vector

# ---------- Settings ----------
output_dir = bpy.path.abspath("//freestyle_scaled_svg")
dpi = 300
unit = "mm"  # "mm", "cm", or "in"
render_margin_mm = 2.0
label_font_size_mm = 5.0
label_halo_width_mm = 0.6
label_grid_spacing_mm = 100.0


# ---------- Helpers ----------
def meters_to_unit(value_m, output_unit):
    if output_unit == "mm":
        return value_m * 1000
    if output_unit == "cm":
        return value_m * 100
    if output_unit == "in":
        return value_m / 0.0254
    raise ValueError("Unsupported unit")


def get_svg_path_bounds(root):
    number_re = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
    xs = []
    ys = []

    for path in root.findall(".//{http://www.w3.org/2000/svg}path"):
        values = [float(value) for value in number_re.findall(path.get("d", ""))]
        xs.extend(values[0::2])
        ys.extend(values[1::2])

    if not xs or not ys:
        raise Exception("Could not find SVG path coordinates.")

    return min(xs), min(ys), max(xs), max(ys)


def svg_has_paths(root):
    return any(
        path.get("d", "").strip()
        for path in root.findall(".//{http://www.w3.org/2000/svg}path")
    )


def get_y_axis_projection(obj):
    bbox = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    min_x = min(v.x for v in bbox)
    max_x = max(v.x for v in bbox)
    min_y = min(v.y for v in bbox)
    min_z = min(v.z for v in bbox)
    max_z = max(v.z for v in bbox)

    center = Vector((
        (min_x + max_x) / 2,
        min_y,
        (min_z + max_z) / 2,
    ))
    return center, max_x - min_x, max_z - min_z


# ---------- Camera ----------
cam_front = bpy.data.objects.get("Camera-Front")
cam_back = bpy.data.objects.get("Camera-Back")

cam_data_front = cam_front.data
cam_data_front.type = "ORTHO"
cam_data_front.sensor_fit = "VERTICAL"

cam_data_back = cam_back.data
cam_data_back.type = "ORTHO"
cam_data_back.sensor_fit = "VERTICAL"

bpy.context.scene.camera = cam_front

# ---------- Freestyle ----------
scene = bpy.context.scene
scene.render.use_freestyle = True
bpy.context.view_layer.use_freestyle = True

os.makedirs(output_dir, exist_ok=True)


def safe_filename(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value or "unnamed"


def render_view(obj, camera, view_name, render_data):
    export_name = f"{safe_filename(obj.name)}_{view_name}"
    final_path = os.path.join(output_dir, export_name + ".svg")
    scene.render.filepath = os.path.join(output_dir, export_name)

    svg_state_before = {
        name: os.stat(os.path.join(output_dir, name)).st_mtime_ns
        for name in os.listdir(output_dir)
        if name.lower().endswith(".svg")
    }

    scene.camera = camera
    bpy.context.view_layer.update()

    tree = None
    root = None
    svg_path = None
    for attempt in range(2):
        bpy.ops.render.render(write_still=False)

        svgs = [
            os.path.join(output_dir, name)
            for name in os.listdir(output_dir)
            if name.lower().endswith(".svg")
            and os.stat(os.path.join(output_dir, name)).st_mtime_ns
            != svg_state_before.get(name)
        ]
        if not svgs:
            raise Exception(
                f"No SVG created for {obj.name} ({view_name}). "
                "Make sure the Freestyle SVG Exporter addon is enabled."
            )

        svg_path = max(svgs, key=os.path.getmtime)
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        tree = ET.parse(svg_path)
        root = tree.getroot()
        if svg_has_paths(root):
            break

        if attempt == 0:
            print(
                f"Empty SVG for {obj.name} ({view_name}); "
                "updating the view layer and retrying."
            )
            os.remove(svg_path)
            svg_state_before.pop(os.path.basename(svg_path), None)
            bpy.context.view_layer.update()
    else:
        raise Exception(
            f"Freestyle exported no paths for {obj.name} ({view_name}). "
            "Check that the object has renderable edges from this camera view."
        )

    # Freestyle's SVG coordinates can include centered overscan and therefore
    # do not always match 0..render_resolution. Use the actual path centerline
    # bounds, then convert the physical margin into the same coordinate space.
    min_path_x, min_path_y, max_path_x, max_path_y = get_svg_path_bounds(root)
    path_width = max_path_x - min_path_x
    path_height = max_path_y - min_path_y

    if path_width <= 0 or path_height <= 0:
        raise Exception("The exported SVG paths have invalid bounds.")

    margin_x = path_width * render_data["margin_m"] / render_data["width_m"]
    margin_y = path_height * render_data["margin_m"] / render_data["height_m"]
    view_min_x = min_path_x - margin_x
    view_min_y = min_path_y - margin_y
    view_width = path_width + (2 * margin_x)
    view_height = path_height + (2 * margin_y)

    root.set("width", f"{render_data['page_width_units']}{unit}")
    root.set("height", f"{render_data['page_height_units']}{unit}")
    root.set(
        "viewBox",
        f"{view_min_x} {view_min_y} {view_width} {view_height}",
    )
    root.set("preserveAspectRatio", "none")

    page_height_units = render_data["page_height_units"]
    font_size = (
        view_height
        * meters_to_unit(label_font_size_mm / 1000.0, unit)
        / page_height_units
    )
    halo_width = (
        view_height
        * meters_to_unit(label_halo_width_mm / 1000.0, unit)
        / page_height_units
    )
    shape_width_mm = render_data["width_m"] * 1000.0
    shape_height_mm = render_data["height_m"] * 1000.0
    label_columns = max(1, ceil(shape_width_mm / label_grid_spacing_mm))
    label_rows = max(1, ceil(shape_height_mm / label_grid_spacing_mm))
    label_text = obj.name + " (" + view_name.capitalize() + ")"

    label_group = ET.SubElement(
        root,
        "{http://www.w3.org/2000/svg}g",
        {"id": "slice-labels"},
    )
    for row in range(label_rows):
        y = min_path_y + path_height * ((row + 0.5) / label_rows)
        for column in range(label_columns):
            x = min_path_x + path_width * ((column + 0.5) / label_columns)
            label = ET.SubElement(
                label_group,
                "{http://www.w3.org/2000/svg}text",
                {
                    "x": f"{x}",
                    "y": f"{y}",
                    "text-anchor": "middle",
                    "dominant-baseline": "middle",
                    "font-family": "Arial, sans-serif",
                    "font-size": f"{font_size}",
                    "font-weight": "bold",
                    "fill": "black",
                    "stroke": "none",
                    "stroke-width": f"{halo_width}",
                    "paint-order": "stroke fill",
                },
            )
            label.text = label_text

    tree.write(final_path, encoding="utf-8", xml_declaration=True)

    if os.path.normcase(svg_path) != os.path.normcase(final_path):
        os.remove(svg_path)

    print("Exported scaled SVG:")
    print(final_path)
    print(f"View: {view_name}")
    print(f"Shape size: {render_data['width_units']:.2f}{unit} x "
          f"{render_data['height_units']:.2f}{unit}")
    print(
        f"Artboard size: {render_data['page_width_units']:.2f}{unit} x "
        f"{render_data['page_height_units']:.2f}{unit}"
    )


def render_front(obj, center, render_data):
    # Camera looks along +Y at the X/Z plane.
    cam_front.rotation_euler = (1.57079632679, 0, 0)
    cam_front.location = (center.x, center.y - 10, center.z)
    cam_data_front.ortho_scale = render_data["page_height_m"]
    render_view(obj, cam_front, "front", render_data)


def render_back(obj, center, render_data):
    # Camera looks along -Y while keeping world +Z upright.
    cam_back.rotation_euler = (1.57079632679, 0, 3.14159265359)
    cam_back.location = (center.x, center.y + 10, center.z)
    cam_data_back.ortho_scale = render_data["page_height_m"]
    render_view(obj, cam_back, "back", render_data)


def render_slice(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    # Applying scale makes the world-space dimensions reliable.
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    center, width_m, height_m = get_y_axis_projection(obj)
    margin_m = render_margin_mm / 1000.0
    page_width_m = width_m + (2 * margin_m)
    page_height_m = height_m + (2 * margin_m)

    render_data = {
        "width_m": width_m,
        "height_m": height_m,
        "margin_m": margin_m,
        "page_height_m": page_height_m,
        "width_units": meters_to_unit(width_m, unit),
        "height_units": meters_to_unit(height_m, unit),
        "page_width_units": meters_to_unit(page_width_m, unit),
        "page_height_units": meters_to_unit(page_height_m, unit),
    }

    pixels_per_meter = dpi / 0.0254
    scene.render.resolution_x = max(1, round(page_width_m * pixels_per_meter))
    scene.render.resolution_y = max(1, round(page_height_m * pixels_per_meter))
    scene.render.resolution_percentage = 100

    render_front(obj, center, render_data)
    render_back(obj, center, render_data)


container = bpy.data.collections.get("Collection - Scripting")
children = container.all_objects if container else []
mesh_objects = [obj for obj in children if obj.type == "MESH"]
if not mesh_objects:
    raise Exception("No mesh objects found in the 'Collection - Scripting' collection.")

original_render_visibility = {
    obj: obj.hide_render
    for obj in mesh_objects
}

try:
    for obj in mesh_objects:
        obj.hide_render = True
    bpy.context.view_layer.update()

    count = 0
    for obj in mesh_objects:
        obj.hide_render = False
        bpy.context.view_layer.update()
        render_slice(obj)
        obj.hide_render = True
        bpy.context.view_layer.update()
        count += 1
        # if count == 4:
        #     break
        
    print(f"Rendered {count} slices.")
finally:
    for obj, was_hidden in original_render_visibility.items():
        obj.hide_render = was_hidden

# obj = bpy.context.object
# if obj is None or obj.type != "MESH":
#     raise Exception("Select the mesh object you want to export.")

# render_slice(obj)
