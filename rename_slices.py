import bpy

container = bpy.data.collections.get("Collection - Scripting")
children = container.all_objects if container else []
mesh_objects = [obj for obj in children if obj.type == "MESH"]

#todo rename slices numerically, from front to back

#sort mesh objects by their location on the y-axis (front to back)
mesh_objects.sort(key=lambda obj: obj.location.y)

for mesh in mesh_objects:
    #get the current name of the mesh
    current_name = mesh.name
    #get the index of the mesh in the sorted list
    index = mesh_objects.index(mesh)
    #rename the mesh to 1, 2, 3, 4, etc. based on its index
    new_name = f"Slice{index + 1}"
    mesh.name = new_name
    print(f"Renamed {current_name} to {new_name}")
    