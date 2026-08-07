"""Import a UTT-exported .glb with the icosphere bone shapes disabled.

Run this in Blender (Scripting workspace -> Text Editor -> Run Script),
or pass the file as an argument:

    blender --python import_utt_glb.py

It behaves like File > Import > glTF 2.0 but with "Bone Shape" unchecked,
so bones import as plain sticks instead of icospheres.  (You can also
import normally and just uncheck "Bone Shape" in the import dialog.)
"""
import bpy
import sys

if len(sys.argv) > 1 and sys.argv[-1].endswith(".glb"):
    filepath = sys.argv[-1]
else:
    filepath = bpy.path.abspath("//") or ""

bpy.ops.import_scene.gltf(
    filepath=filepath,
    disable_bone_shape=True,
)
