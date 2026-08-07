from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import numpy as np

from mdl_parser import PSGModel

# Scene scale for skinned exports.  The parser dequantizes S16 vertices to
# bone units (÷16384) and bone matrices are in bone units, so both are
# scaled by BONE_SCALE to keep mesh and armature in the same units.  This
# matches the scale used by the Blender reference importer
# (PsgMeshnBones-1.py, BONE_SCALE on bones, S16_SCALE on raw sub-units).
BONE_SCALE = 63.363

# Game Y-up -> Blender Z-up: +90deg about X, (x, y, z) -> (x, -z, y).
_YUP_TO_ZUP = np.array(
    [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]], dtype=np.float64
)
# Blender Z-up -> glTF Y-up preimage: R(-90, X).
_ZUP_TO_YUP = _YUP_TO_ZUP.T

# Reference importer pre-offsets bones DOWN by 0.8 in game Y before scaling.
BONE_PRE_OFFSET_DOWN_Y = 0.8

# The game's skeletons carry internal helper joints (twist/help/offset
# bones) that the reference importer keeps as tiny stubs.  The exported
# skeleton keeps them too, reparented to the skeleton root so Blender's
# min-child length heuristic still sizes the real chains from their
# nearest real child (the "two large arm bones" look).  Every leaf bone
# (helpers included) gets a stub child at the reference's default leaf
# length so it renders as a small bone instead of being stretched to its
# parent's length; a second, microscopic "tail end" child keeps the
# stub's own length from blowing up the same way.
_HELPER_TOKENS = ("TWIST", "HLP", "HELP", "OFFSET")
_TAIL_DOT_LENGTH = 0.005


def _is_helper_bone(name: str) -> bool:
    u = _norm(name)
    return any(token in u for token in _HELPER_TOKENS)

# Parent relationships by normalized bone name, matching the reference
# Blender importer.  Bones not listed here fall back to name-pattern
# guesses and then to a nearest-neighbor parent.
BONE_HIERARCHY: dict[str, str | None] = {
    "TRAJECTORY": None,
    "HIPS": None,
    "SPINE": "HIPS",
    "SPINE1": "SPINE",
    "SPINE2": "SPINE1",
    "SPINE3": "SPINE2",
    "NECK": "SPINE3",
    "NECK1": "NECK",
    "HEAD": "NECK1",
    "EYELIDS": "HEAD",
    "EYEBROW": "HEAD",
    "MOUTH": "HEAD",
    "HEADEND": "HEAD",
    "RIGHTSHOULDER": "SPINE3",
    "RIGHTARM": "RIGHTSHOULDER",
    "RIGHTFOREARM": "RIGHTARM",
    "RIGHTHAND": "RIGHTFOREARM",
    "LEFTSHOULDER": "SPINE3",
    "LEFTARM": "LEFTSHOULDER",
    "LEFTFOREARM": "LEFTARM",
    "LEFTHAND": "LEFTFOREARM",
    "LEFTHANDINDEX1": "LEFTHAND",
    "LEFTHANDINDEX2": "LEFTHANDINDEX1",
    "LEFTHANDRING1": "LEFTHAND",
    "LEFTHANDRING2": "LEFTHANDRING1",
    "RIGHTHANDINDEX1": "RIGHTHAND",
    "RIGHTHANDINDEX2": "RIGHTHANDINDEX1",
    "RIGHTHANDRING1": "RIGHTHAND",
    "RIGHTHANDRING2": "RIGHTHANDRING1",
    "LEFTHANDINDEX3": "LEFTHANDINDEX2",
    "LEFTHANDMIDDLE1": "LEFTHAND",
    "LEFTHANDMIDDLE2": "LEFTHANDMIDDLE1",
    "LEFTHANDMIDDLE3": "LEFTHANDMIDDLE2",
    "LEFTINHANDPINKY": "LEFTHAND",
    "LEFTHANDPINKY1": "LEFTINHANDPINKY",
    "LEFTHANDPINKY2": "LEFTHANDPINKY1",
    "LEFTHANDPINKY3": "LEFTHANDPINKY2",
    "LEFTINHANDRING": "LEFTHAND",
    "LEFTHANDRING3": "LEFTHANDRING2",
    "LEFTHANDTHUMB1": "LEFTHAND",
    "LEFTHANDTHUMB2": "LEFTHANDTHUMB1",
    "LEFTHANDTHUMB3": "LEFTHANDTHUMB2",
    "RIGHTHANDINDEX3": "RIGHTHANDINDEX2",
    "RIGHTHANDMIDDLE1": "RIGHTHAND",
    "RIGHTHANDMIDDLE2": "RIGHTHANDMIDDLE1",
    "RIGHTHANDMIDDLE3": "RIGHTHANDMIDDLE2",
    "RIGHTINHANDPINKY": "RIGHTHAND",
    "RIGHTHANDPINKY1": "RIGHTINHANDPINKY",
    "RIGHTHANDPINKY2": "RIGHTHANDPINKY1",
    "RIGHTHANDPINKY3": "RIGHTHANDPINKY2",
    "RIGHTINHANDRING": "RIGHTHAND",
    "RIGHTHANDRING": "RIGHTINHANDRING",
    "RIGHTHANDRING1": "RIGHTINHANDRING",
    "RIGHTHANDRING3": "RIGHTHANDRING2",
    "RIGHTHANDTHUMB1": "RIGHTHAND",
    "RIGHTHANDTHUMB2": "RIGHTHANDTHUMB1",
    "RIGHTHANDTHUMB3": "RIGHTHANDTHUMB2",
    "LEFTHANDTHUMB": "LEFTHAND",
    "RIGHTHANDTHUMB": "RIGHTHAND",
    "RIGHTUPLEG": "HIPS",
    "RIGHTLEG": "RIGHTUPLEG",
    "RIGHTFOOT": "RIGHTLEG",
    "RIGHTTOEBASE": "RIGHTFOOT",
    "LEFTUPLEG": "HIPS",
    "LEFTLEG": "LEFTUPLEG",
    "LEFTFOOT": "LEFTLEG",
    "LEFTTOEBASE": "LEFTFOOT",
    "RIGHTSHOULDERHLP": "SPINE3",
    "RIGHTARMTWIST": "RIGHTARM",
    "RIGHTFOREARMTWIST": "RIGHTFOREARM",
    "RIGHTFOREARMTWIST1": "RIGHTFOREARM",
    "LEFTSHOULDERHLP": "SPINE3",
    "LEFTARMTWIST": "LEFTARM",
    "LEFTFOREARMTWIST": "LEFTFOREARM",
    "LEFTFOREARMTWIST1": "LEFTFOREARM",
    "RIGHTUPLEGHLP": "HIPS",
    "RIGHTUPLEGTWIST": "RIGHTUPLEG",
    "LEFTUPLEGHLP": "HIPS",
    "LEFTUPLEGTWIST": "LEFTUPLEG",
}


def _norm(name: str) -> str:
    return (name or "").replace(" ", "").replace("_", "").replace("-", "").upper()


def _pad4(length: int) -> int:
    """Round *length* up to the next multiple of 4."""
    return (length + 3) & ~3


def _guess_parent_name(u: str, by_name: dict[str, int]) -> int | None:
    """Fallback parent lookup for bones missing from BONE_HIERARCHY."""
    if u in {"EYELIDS", "EYEBROW", "MOUTH", "HEADEND"}:
        return by_name.get("HEAD") or by_name.get("NECK1") or by_name.get("NECK")
    if u.endswith("THUMB"):
        key = "LEFTHAND" if "LEFT" in u else "RIGHTHAND"
        return by_name.get(key)
    if "TOE" in u:
        key = "LEFTFOOT" if "LEFT" in u else "RIGHTFOOT"
        return by_name.get(key)
    if "HAND" in u:
        key = "LEFTHAND" if "LEFT" in u else "RIGHTHAND"
        return by_name.get(key)
    if any(token in u for token in ("JAW", "EYE", "FACE", "HEAD")):
        return by_name.get("HEAD")
    return None


def _axis_angle_matrix(axis: np.ndarray, theta: float) -> np.ndarray:
    """Rotation matrix (row-major) for *theta* radians about unit *axis*."""
    x, y, z = axis / float(np.linalg.norm(axis))
    c, s = math.cos(theta), math.sin(theta)
    cc = 1.0 - c
    return np.array(
        [
            [x * x * cc + c, x * y * cc - z * s, x * z * cc + y * s],
            [y * x * cc + z * s, y * y * cc + c, y * z * cc - x * s],
            [z * x * cc - y * s, z * y * cc + x * s, z * z * cc + c],
        ],
        dtype=np.float64,
    )


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v


def _rotate_about(v: np.ndarray, axis: np.ndarray, angle: float) -> np.ndarray:
    """Rotate *v* by *angle* radians about *axis* (Rodrigues)."""
    a = _unit(axis)
    c, s = math.cos(angle), math.sin(angle)
    return v * c + np.cross(a, v) * s + a * float(np.dot(a, v)) * (1.0 - c)


def _bone_base_matrix(direction: np.ndarray) -> np.ndarray:
    """Blender's vec_roll_to_mat3(direction, 0): Y axis = *direction*, roll 0.

    The base frame is the axis-angle rotation of the Y-up identity about
    cross((0,1,0), direction) by the angle between them.
    """
    d = _unit(direction)
    target = np.array([0.0, 1.0, 0.0])
    t = float(np.dot(target, d))
    if t > 1.0 - 1e-12:
        return np.eye(3)
    if t < -1.0 + 1e-12:
        # direction anti-parallel to the Y target: 180deg about Z
        return np.diag([-1.0, -1.0, 1.0])
    axis = _unit(np.cross(target, d))
    return _axis_angle_matrix(axis, math.acos(min(1.0, max(-1.0, t))))


def _reference_bone_frame(rot: np.ndarray, bind_trans: np.ndarray) -> np.ndarray:
    """Blender-space matrix_local of a bone, matching the reference importer.

    Reproduces PsgMeshnBones-1.py exactly: the head is the bind translation
    pre-offset by BONE_PRE_OFFSET_DOWN_Y in game Y, scaled by BONE_SCALE and
    rotated game-Y-up -> Blender-Z-up; the bone direction is the game's X
    axis (rot3 @ (1,0,0), i.e. the chain axis); the roll is the align_roll
    of the provisional Z-axis frame so the bone's Z axis points at the game
    X axis (projected onto the plane perpendicular to the direction).
    """
    offset = bind_trans - np.array([0.0, BONE_PRE_OFFSET_DOWN_Y, 0.0])
    head = _YUP_TO_ZUP @ (offset * BONE_SCALE)
    rot3 = _YUP_TO_ZUP @ rot
    z_axis = rot3[:, 2]
    x_axis = rot3[:, 0]
    current = _bone_base_matrix(z_axis)
    z_target = _unit(x_axis - float(np.dot(x_axis, z_axis)) * z_axis)
    z_cur = current[:, 2]
    roll = math.atan2(
        float(np.dot(np.cross(z_cur, z_target), z_axis)),
        float(np.dot(z_cur, z_target)),
    )
    direction = _unit(x_axis)
    base = _bone_base_matrix(direction)
    frame = np.column_stack(
        [
            _rotate_about(base[:, 0], direction, roll),
            direction,
            _rotate_about(base[:, 2], direction, roll),
        ]
    )
    out = np.eye(4)
    out[:3, :3] = frame
    out[:3, 3] = head
    return out


def _build_skeleton(
    model: PSGModel,
) -> tuple[
    list[int],
    list[np.ndarray],
    list[np.ndarray],
    np.ndarray,
    list[str],
    dict[int, int],
]:
    """Compute the bone hierarchy and skinning transforms for *model*.

    Returns ``(parents, bind_worlds, local_matrices, ibm_data, names,
    remap)``:

    - ``parents[i]`` is the index of the parent bone (or -1 for roots);
    - ``bind_worlds[i]`` is the bind world matrix (bind translation scaled
      by BONE_SCALE, rotation = the game's stored joint frame);
    - ``local_matrices[i]`` is the glTF node matrix (column-major, parent
      space);
    - ``ibm_data`` is the packed inverse-bind-matrix accessor payload
      (column-major floats): the exact inverse of each bind world;
    - ``names[i]`` is the node name;
    - ``remap`` maps each original bone index to its index in the exported
      skeleton.  All bones (including the game's helper joints) are kept,
      so this is the identity map.

    Every bone is kept: the game's helper joints (twist/hlp/help/offset
    bones) are drawn as small stubs exactly like the reference importer.
    Helpers are reparented to the skeleton root so Blender's importer
    sizes the real chains from their nearest real child (the reference's
    "primary child" rule) instead of the closer helper joints.  Leaf
    bones (helpers, Head, Hands, Toes) get a stub child at the reference
    leaf length (PsgMeshnBones-1.py ``compute_default_bone_length``
    * 0.5, 1.184 for the OuterTorso skeleton) so Blender sizes them as
    small bones; the stub itself gets a microscopic child so it doesn't
    inherit the leaf's length.
    """
    bones = model.bones
    count = len(bones)

    # The stored matrices are inverse-bind, column-major in the file; the
    # parser reshapes them row-major, so `matrix` (row-major) = M^T, and
    # `matrix[:3, :3]` is the game-space bind rotation R (bind = M^-1).
    # Each bone's Blender-space frame is computed exactly like the
    # reference importer (PsgMeshnBones-1.py): head pre-offset by -0.8 in
    # game Y, scaled by BONE_SCALE, rotated Y-up -> Z-up; bone direction =
    # the game X axis (the chain axis); roll from Blender's align_roll
    # semantics.  The glTF node world is the Z-up -> Y-up preimage
    # (R(-90, X) @ frame), and the inverse-bind accessor stores inv(world):
    # Blender's importer derives the rest pose from that accessor and
    # converts Y-up -> Z-up, so the imported matrix_local matches the
    # reference bone-for-bone while the mesh skinning stays identity.
    worlds: list[np.ndarray] = []
    for bone in bones:
        matrix = np.asarray(bone.matrix, dtype=np.float64).reshape(4, 4)
        rot = matrix[:3, :3]
        bind_trans = np.linalg.inv(matrix.T)[:3, 3]
        frame = _reference_bone_frame(rot, bind_trans)
        world = np.eye(4)
        world[:3, :3] = _ZUP_TO_YUP @ frame[:3, :3]
        world[:3, 3] = _ZUP_TO_YUP @ frame[:3, 3]
        worlds.append(world)

    by_name: dict[str, int] = {}
    for index, bone in enumerate(bones):
        by_name.setdefault(_norm(bone.name), index)

    parents: list[int] = [-1] * count
    for child_index, bone in enumerate(bones):
        u = _norm(bone.name)
        parent_index: int | None = None
        if u in BONE_HIERARCHY:
            parent_u = BONE_HIERARCHY[u]
            if parent_u is not None:
                parent_index = by_name.get(parent_u)
            # Explicit None keeps the bone a root; no fallbacks.
        else:
            parent_index = _guess_parent_name(u, by_name)
        if parent_index is None and u not in BONE_HIERARCHY:
            # Nearest joint on the same side of the body.
            best: int | None = None
            best_distance = float("inf")
            side = "L" if "LEFT" in u else ("R" if "RIGHT" in u else None)
            for other_index, other in enumerate(bones):
                if other_index == child_index:
                    continue
                other_u = _norm(other.name)
                other_side = (
                    "L" if "LEFT" in other_u else ("R" if "RIGHT" in other_u else None)
                )
                if side and other_side and side != other_side:
                    continue
                distance = float(
                    np.linalg.norm(worlds[other_index][:3, 3] - worlds[child_index][:3, 3])
                )
                if distance < best_distance:
                    best_distance = distance
                    best = other_index
            parent_index = best
        if parent_index == child_index:
            parent_index = None
        parents[child_index] = -1 if parent_index is None else parent_index

    # Keep every bone.  The game's helper joints are reparented to the
    # skeleton root so Blender's min-child length heuristic still sizes
    # the real chains from their nearest real child (the reference's
    # "primary child" rule); the helpers themselves are drawn as small
    # stubs exactly like the reference importer.
    for index, bone in enumerate(bones):
        if _is_helper_bone(bone.name):
            parents[index] = -1

    kept = list(range(count))
    old_to_new = {old: new for new, old in enumerate(kept)}
    new_parents = [parents[old] for old in kept]
    remap = dict(old_to_new)  # identity: every bone stays in place

    child_count = [0] * count
    for new_parent in new_parents:
        if new_parent >= 0:
            child_count[new_parent] += 1

    # Leaf stub length, matching the reference importer
    # (PsgMeshnBones-1.py: compute_default_bone_length * 0.5, with a
    # 0.01 floor): 1.184 for the OuterTorso skeleton.
    heads_all = np.array([worlds[i][:3, 3] for i in range(count)])
    mn = heads_all.min(axis=0)
    mx = heads_all.max(axis=0)
    diag = float(np.linalg.norm(mx - mn))
    default_len = max(0.02 * diag, 0.05) if diag > 1e-6 else 0.1
    leaf_stub = max(0.5 * default_len, 0.01)

    names = [bones[old].name for old in kept]
    worlds_full = [worlds[old] for old in kept]
    parents_full = list(new_parents)
    for new_index, has_child in enumerate(child_count):
        if has_child:
            continue
        # Stub child at the reference leaf length: Blender sizes the leaf
        # to the distance of its nearest bone child, so the leaf renders
        # as a small stub instead of inheriting its parent's length.
        stub_world = worlds_full[new_index].copy()
        stub_world[:3, 3] += worlds_full[new_index][:3, 1] * leaf_stub
        worlds_full.append(stub_world)
        parents_full.append(new_index)
        names.append(f"{names[new_index]}_tail")
        # The stub is itself a leaf: give it a microscopic child so its
        # own length stays a dot instead of inheriting the leaf's stub.
        end_world = stub_world.copy()
        end_world[:3, 3] += stub_world[:3, 1] * _TAIL_DOT_LENGTH
        worlds_full.append(end_world)
        parents_full.append(len(names) - 1)
        names.append(f"{names[new_index]}_tailend")

    skeleton_count = len(names)
    locals_list: list[np.ndarray] = []
    for index in range(skeleton_count):
        parent = parents_full[index]
        if parent < 0:
            local = worlds_full[index]
        else:
            local = np.linalg.inv(worlds_full[parent]) @ worlds_full[index]
        locals_list.append(local)

    ibm_parts = [np.linalg.inv(world.T).astype(np.float32) for world in worlds_full]
    ibm_data = np.concatenate([part.ravel() for part in ibm_parts], axis=0)

    return parents_full, worlds_full, locals_list, ibm_data, names, remap


def _map_skin_joints(
    joints: np.ndarray,
    weights: np.ndarray,
    palette: list[int],
    bone_count: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Map raw skin slots to global bone indices.

    Raw indices are palette slots; ``palette[slot]`` is the global bone.
    Without a usable palette the raw index is used directly.  Out-of-range
    slots are pinned to bone 0 with a zero weight.
    """
    raw = joints.astype(np.int32)
    mapped = raw.copy()
    if palette:
        palette_array = np.asarray(palette, dtype=np.int32)
        in_range = raw < len(palette_array)
        remapped = palette_array[np.clip(raw, 0, len(palette_array) - 1)]
        mapped = np.where(in_range, remapped, 0)
    invalid = mapped >= bone_count
    mapped[invalid] = 0
    out_weights = weights.astype(np.uint8).copy()
    out_weights[invalid] = 0
    return mapped.astype(np.uint8), out_weights


def _build_gltf_dict(
    model: PSGModel,
    stem: str,
    with_skin: bool = False,
) -> tuple[dict, bytes]:
    usable = [m for m in model.meshes if m.vertex_count > 0 and m.triangle_count > 0]
    if not usable:
        raise ValueError("No mesh data to export")
    if with_skin:
        return _build_skinned_gltf_dict(model, stem, usable)

    merged_verts: list[np.ndarray] = []
    merged_norms: list[np.ndarray] = []
    merged_uvs: list[np.ndarray] = []
    merged_faces: list[np.ndarray] = []
    all_have_normals = all(m.normals is not None for m in usable)
    all_have_uvs = all(m.uvs is not None for m in usable)
    vertex_offset = 0
    for mesh in usable:
        merged_verts.append(mesh.vertices)
        if all_have_normals and mesh.normals is not None:
            merged_norms.append(mesh.normals)
        if all_have_uvs and mesh.uvs is not None:
            merged_uvs.append(mesh.uvs)
        merged_faces.append(mesh.faces + vertex_offset)
        vertex_offset += mesh.vertex_count

    vertices = np.concatenate(merged_verts, axis=0)
    faces = np.concatenate(merged_faces, axis=0)
    normals = np.concatenate(merged_norms, axis=0) if merged_norms else None
    uvs = np.concatenate(merged_uvs, axis=0) if merged_uvs else None

    # Normalize to [-0.5, 0.5] so mesh is visible in Blender
    # PSG stores vertices as int16; the parser casts raw (no scaling),
    # making positions span ~65535 units. Noesis divides by ~32768.
    center = (vertices.max(axis=0) + vertices.min(axis=0)) * 0.5
    vertices -= center
    span = float(np.max(np.ptp(vertices, axis=0)))
    if span > 1e-12:
        vertices *= 0.5 / span

    vertex_count = vertices.shape[0]
    index_count = faces.size

    use_uint32 = vertex_count > 65535
    idx_dtype = np.uint32 if use_uint32 else np.uint16
    index_component = 5125 if use_uint32 else 5123

    idx_bytes = np.ascontiguousarray(faces, dtype=idx_dtype).tobytes()
    pos_bytes = np.ascontiguousarray(vertices, dtype=np.float32).tobytes()
    norm_bytes = (
        np.ascontiguousarray(normals, dtype=np.float32).tobytes()
        if normals is not None
        else b""
    )
    uv_bytes = (
        np.ascontiguousarray(uvs, dtype=np.float32).tobytes()
        if uvs is not None
        else b""
    )

    # Layout matches Noesis: indices, positions, normals, UVs
    bin_data = idx_bytes + pos_bytes + norm_bytes + uv_bytes

    # --- buffer views (indices first, matching Noesis) ---
    offset = 0

    buffer_views = [
        {  # 0: indices
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(idx_bytes),
            "target": 34963,
        },
    ]
    acc_idx = {
        "bufferView": 0,
        "byteOffset": 0,
        "componentType": index_component,
        "count": index_count,
        "type": "SCALAR",
        "min": [int(faces.min())],
        "max": [int(faces.max())],
    }
    offset += len(idx_bytes)

    buffer_views.append({  # 1: positions
        "buffer": 0,
        "byteOffset": offset,
        "byteLength": len(pos_bytes),
        "byteStride": 12,
        "target": 34962,
    })
    acc_pos = {
        "bufferView": 1,
        "byteOffset": 0,
        "componentType": 5126,
        "count": vertex_count,
        "type": "VEC3",
        "min": vertices.min(axis=0).round(6).tolist(),
        "max": vertices.max(axis=0).round(6).tolist(),
    }
    offset += len(pos_bytes)

    accessors = [acc_idx, acc_pos]
    attributes: dict[str, int] = {"POSITION": 1}
    next_attr = 2

    if norm_bytes:
        buffer_views.append({  # 2: normals
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(norm_bytes),
            "byteStride": 12,
            "target": 34962,
        })
        accessors.append({
            "bufferView": 2,
            "byteOffset": 0,
            "componentType": 5126,
            "count": vertex_count,
            "type": "VEC3",
        })
        attributes["NORMAL"] = next_attr
        next_attr += 1
        offset += len(norm_bytes)

    if uv_bytes:
        buffer_views.append({  # 3 or 4: UVs
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(uv_bytes),
            "byteStride": 8,
            "target": 34962,
        })
        accessors.append({
            "bufferView": len(buffer_views) - 1,
            "byteOffset": 0,
            "componentType": 5126,
            "count": vertex_count,
            "type": "VEC2",
        })
        attributes["TEXCOORD_0"] = next_attr
        next_attr += 1

    gltf: dict = {
        "asset": {"version": "2.0", "generator": "UTT"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": stem}],
        "meshes": [{
            "primitives": [{
                "attributes": attributes,
                "indices": 0,
                "mode": 4,
            }],
        }],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_data)}],
    }

    return gltf, bin_data


def _build_skinned_gltf_dict(model: PSGModel, stem: str, usable) -> tuple[dict, bytes]:
    if not model.bones:
        raise ValueError("Model has no skeleton to export")
    if any(mesh.joints is None or mesh.weights is None for mesh in usable):
        raise ValueError("Some meshes have no skin data; cannot export with bones")

    parents, _worlds, local_matrices, ibm_data, bone_names, remap = _build_skeleton(model)
    bone_count = len(bone_names)

    merged_verts: list[np.ndarray] = []
    merged_norms: list[np.ndarray] = []
    merged_uvs: list[np.ndarray] = []
    merged_faces: list[np.ndarray] = []
    merged_joints: list[np.ndarray] = []
    merged_weights: list[np.ndarray] = []
    all_have_normals = all(m.normals is not None for m in usable)
    all_have_uvs = all(m.uvs is not None for m in usable)
    vertex_offset = 0
    for mesh in usable:
        joints, weights = _map_skin_joints(
            mesh.joints, mesh.weights, model.palette, bone_count
        )
        merged_verts.append(mesh.vertices)
        if all_have_normals and mesh.normals is not None:
            merged_norms.append(mesh.normals)
        if all_have_uvs and mesh.uvs is not None:
            merged_uvs.append(mesh.uvs)
        merged_joints.append(joints)
        merged_weights.append(weights)
        merged_faces.append(mesh.faces + vertex_offset)
        vertex_offset += mesh.vertex_count

    vertices = np.concatenate(merged_verts, axis=0)
    faces = np.concatenate(merged_faces, axis=0)
    normals = np.concatenate(merged_norms, axis=0) if merged_norms else None
    uvs = np.concatenate(merged_uvs, axis=0) if merged_uvs else None
    joints = np.concatenate(merged_joints, axis=0).astype(np.int32)
    weights = np.concatenate(merged_weights, axis=0).astype(np.int32)

    # The exported skeleton keeps every game bone (helpers included), so
    # the remap table is the identity and influences pass through
    # untouched, exactly like the reference importer's bind.
    if any(old != new for old, new in remap.items()):
        remapped_joints = np.zeros_like(joints)
        remapped_weights = np.zeros_like(weights)
        for vertex in range(joints.shape[0]):
            bucket: dict[int, int] = {}
            for slot in range(4):
                weight = int(weights[vertex, slot])
                if weight <= 0:
                    continue
                new_joint = remap[int(joints[vertex, slot])]
                bucket[new_joint] = bucket.get(new_joint, 0) + weight
            top = sorted(bucket.items(), key=lambda kv: -kv[1])[:4]
            total = sum(weight for _, weight in top) or 1
            for slot, (joint, weight) in enumerate(top):
                remapped_joints[vertex, slot] = joint
                remapped_weights[vertex, slot] = max(
                    0, min(255, int(round(weight * 255 / total)))
                )
        joints = remapped_joints.astype(np.uint8)
        weights = remapped_weights.astype(np.uint8)

    # The parser already dequantizes S16 vertices to bone units (÷16384),
    # so scale by BONE_SCALE to match the armature (same as the reference
    # importer, which uses S16_SCALE on raw sub-units).
    vertices = vertices.astype(np.float32, copy=True) * np.float32(BONE_SCALE)

    vertex_count = vertices.shape[0]
    index_count = faces.size

    use_uint32 = vertex_count > 65535
    idx_dtype = np.uint32 if use_uint32 else np.uint16
    index_component = 5125 if use_uint32 else 5123

    idx_bytes = np.ascontiguousarray(faces, dtype=idx_dtype).tobytes()
    pos_bytes = np.ascontiguousarray(vertices, dtype=np.float32).tobytes()
    norm_bytes = (
        np.ascontiguousarray(normals, dtype=np.float32).tobytes()
        if normals is not None
        else b""
    )
    uv_bytes = (
        np.ascontiguousarray(uvs, dtype=np.float32).tobytes()
        if uvs is not None
        else b""
    )
    joint_bytes = np.ascontiguousarray(joints, dtype=np.uint8).tobytes()
    weight_bytes = np.ascontiguousarray(weights, dtype=np.uint8).tobytes()
    ibm_bytes = np.ascontiguousarray(ibm_data, dtype=np.float32).tobytes()

    bin_data = (
        idx_bytes + pos_bytes + norm_bytes + uv_bytes
        + joint_bytes + weight_bytes + ibm_bytes
    )

    offset = 0
    buffer_views = [
        {  # 0: indices
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(idx_bytes),
            "target": 34963,
        },
    ]
    acc_idx = {
        "bufferView": 0,
        "byteOffset": 0,
        "componentType": index_component,
        "count": index_count,
        "type": "SCALAR",
        "min": [int(faces.min())],
        "max": [int(faces.max())],
    }
    offset += len(idx_bytes)

    buffer_views.append({  # 1: positions
        "buffer": 0,
        "byteOffset": offset,
        "byteLength": len(pos_bytes),
        "byteStride": 12,
        "target": 34962,
    })
    acc_pos = {
        "bufferView": 1,
        "byteOffset": 0,
        "componentType": 5126,
        "count": vertex_count,
        "type": "VEC3",
        "min": vertices.min(axis=0).round(6).tolist(),
        "max": vertices.max(axis=0).round(6).tolist(),
    }
    offset += len(pos_bytes)

    accessors = [acc_idx, acc_pos]
    attributes: dict[str, int] = {"POSITION": 1}
    next_attr = 2

    if norm_bytes:
        buffer_views.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(norm_bytes),
            "byteStride": 12,
            "target": 34962,
        })
        accessors.append({
            "bufferView": len(buffer_views) - 1,
            "byteOffset": 0,
            "componentType": 5126,
            "count": vertex_count,
            "type": "VEC3",
        })
        attributes["NORMAL"] = next_attr
        next_attr += 1
        offset += len(norm_bytes)

    if uv_bytes:
        buffer_views.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(uv_bytes),
            "byteStride": 8,
            "target": 34962,
        })
        accessors.append({
            "bufferView": len(buffer_views) - 1,
            "byteOffset": 0,
            "componentType": 5126,
            "count": vertex_count,
            "type": "VEC2",
        })
        attributes["TEXCOORD_0"] = next_attr
        next_attr += 1
        offset += len(uv_bytes)

    if joint_bytes:
        buffer_views.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(joint_bytes),
            "byteStride": 4,
            "target": 34962,
        })
        accessors.append({
            "bufferView": len(buffer_views) - 1,
            "byteOffset": 0,
            "componentType": 5121,
            "count": vertex_count,
            "type": "VEC4",
        })
        attributes["JOINTS_0"] = next_attr
        next_attr += 1
        offset += len(joint_bytes)

    if weight_bytes:
        buffer_views.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(weight_bytes),
            "byteStride": 4,
            "target": 34962,
        })
        accessors.append({
            "bufferView": len(buffer_views) - 1,
            "byteOffset": 0,
            "componentType": 5121,
            "normalized": True,
            "count": vertex_count,
            "type": "VEC4",
        })
        attributes["WEIGHTS_0"] = next_attr
        next_attr += 1
        offset += len(weight_bytes)

    if ibm_bytes:
        buffer_views.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(ibm_bytes),
            "target": 34962,
        })
        accessors.append({
            "bufferView": len(buffer_views) - 1,
            "byteOffset": 0,
            "componentType": 5126,
            "count": bone_count,
            "type": "MAT4",
        })
        ibm_accessor = len(accessors) - 1
        offset += len(ibm_bytes)

    # Node 0 is the skeleton root (TRAJECTORY, identity); bones 1..N; the
    # mesh node is the scene root and holds the skeleton.
    trajectory_index = 0
    nodes: list[dict] = [{"name": "TRAJECTORY"}]
    children_of: dict[int, list[int]] = {}
    for index in range(bone_count):
        matrix = local_matrices[index].T.ravel().astype(np.float32).round(6).tolist()
        nodes.append({"name": bone_names[index], "matrix": matrix})
        parent = parents[index]
        holder = children_of.setdefault(parent, [])
        holder.append(index + 1)
    nodes[0]["children"] = list(children_of.get(-1, []))
    for parent in sorted(index for index in children_of if index >= 0):
        nodes[parent + 1]["children"] = list(children_of[parent])

    mesh_node_index = len(nodes)
    nodes.append({
        "mesh": 0,
        "name": stem,
        "skin": 0,
        "children": [trajectory_index],
    })

    gltf: dict = {
        "asset": {"version": "2.0", "generator": "UTT"},
        "scene": 0,
        "scenes": [{"nodes": [mesh_node_index]}],
        "nodes": nodes,
        "meshes": [{
            "primitives": [{
                "attributes": attributes,
                "indices": 0,
                "mode": 4,
            }],
        }],
        "skins": [{
            "inverseBindMatrices": ibm_accessor,
            "skeleton": trajectory_index,
            "joints": list(range(1, bone_count + 1)),
        }],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_data)}],
    }

    return gltf, bin_data


def export_glb(model: PSGModel, output_path: str | Path, *, with_skin: bool = False) -> str:
    """Export a PSGModel to a self-contained glTF Binary (.glb) file."""
    output = Path(output_path)
    if output.suffix.lower() not in (".glb",):
        output = output.with_suffix(".glb")
    output.parent.mkdir(parents=True, exist_ok=True)

    gltf, bin_data = _build_gltf_dict(model, output.stem, with_skin=with_skin)
    json_str = json.dumps(gltf, separators=(",", ":")).encode("utf-8")

    json_pad = _pad4(len(json_str))
    bin_pad = _pad4(len(bin_data))

    header = struct.pack(
        "<III",
        0x46546C67,           # magic  "glTF"
        2,                    # version
        12 + 8 + json_pad + 8 + bin_pad,  # total length
    )
    json_chunk = (
        struct.pack("<I", json_pad)  # padded length (Blender uses this for next-chunk offset)
        + b"JSON"
        + json_str
        + b" " * (json_pad - len(json_str))  # spaces, not nulls — Blender passes to json.loads()
    )
    bin_chunk = (
        struct.pack("<I", bin_pad)   # padded length
        + b"BIN\x00"
        + bin_data
        + b"\x00" * (bin_pad - len(bin_data))
    )

    output.write_bytes(header + json_chunk + bin_chunk)
    return str(output)


def export_gltf(
    model: PSGModel,
    output_path: str | Path,
    *,
    with_skin: bool = False,
) -> str:
    """Export a PSGModel to glTF.

    When *output_path* ends with ``.glb`` the result is a self-contained
    binary file.  Otherwise a ``.glb`` file is written with the same stem
    (GLB is more reliable across importers).

    With *with_skin* the export adds the bone hierarchy, a skin, and
    per-vertex JOINTS/WEIGHTS so the model imports rigged in Blender.
    """
    output = Path(output_path)
    if output.suffix.lower() == ".glb":
        return export_glb(model, output_path, with_skin=with_skin)
    # Force .glb regardless of the extension the caller provided
    return export_glb(model, output.with_suffix(".glb"), with_skin=with_skin)
