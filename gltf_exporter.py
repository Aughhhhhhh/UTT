from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np

from mdl_parser import PSGModel


def _pad4(length: int) -> int:
    """Round *length* up to the next multiple of 4."""
    return (length + 3) & ~3


def _build_gltf_dict(
    model: PSGModel,
    stem: str,
) -> tuple[dict, bytes]:
    usable = [m for m in model.meshes if m.vertex_count > 0 and m.triangle_count > 0]
    if not usable:
        raise ValueError("No mesh data to export")

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


def export_glb(model: PSGModel, output_path: str | Path) -> str:
    """Export a PSGModel to a self-contained glTF Binary (.glb) file."""
    output = Path(output_path)
    if output.suffix.lower() not in (".glb",):
        output = output.with_suffix(".glb")
    output.parent.mkdir(parents=True, exist_ok=True)

    gltf, bin_data = _build_gltf_dict(model, output.stem)
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


def export_gltf(model: PSGModel, output_path: str | Path) -> str:
    """Export a PSGModel to glTF.

    When *output_path* ends with ``.glb`` the result is a self-contained
    binary file.  Otherwise a ``.glb`` file is written with the same stem
    (GLB is more reliable across importers).
    """
    output = Path(output_path)
    if output.suffix.lower() == ".glb":
        return export_glb(model, output_path)
    # Force .glb regardless of the extension the caller provided
    return export_glb(model, output.with_suffix(".glb"))
