"""
rx2_glb_converter.py

Library: converts glTF/GLB models into Skate 3 Xbox 360 (.rx2) game mesh
files by patching a donor .rx2 template. GUI-free — no PyQt dependency.

Derived from GLBtoRX2-v1.0.py (SunJay, Dumbad, RenderWareGavin, Tuukkas).
"""

import os
import struct
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    import numpy
    import pygltflib
except ImportError as e:
    raise ImportError(
        f"Missing required library: {e}. Install with: pip install pygltflib numpy"
    ) from e

LogFn = Callable[[str], None]


@dataclass
class VDElem:
    vertex_type: int
    num_components: int
    stream: int
    offset: int
    stride: int
    type: int
    class_id: int
    raw_format: int = 0  # raw Xbox 360 VertexFormat constant from file


@dataclass
class VertexLayout:
    stride: int = 0
    elements: list[VDElem] = field(default_factory=list)


@dataclass
class ConversionResult:
    output_path: str
    vertex_count: int
    face_count: int
    skinned: bool
    bone_count: int = 0
    file_size: int = 0


# Xbox D3DDECLUSAGE -> PSG-compatible elem.type (so the shared packer works)
_XBOX_USAGE_TO_PSG_TYPE = {
    0: 0,   # POSITION    -> XYZ
    1: 1,   # BLENDWEIGHT -> WEIGHTS
    2: 7,   # BLENDINDICES-> BONEINDICES
    3: 2,   # NORMAL      -> NORMAL
    5: 8,   # TEXCOORD    -> TEX0
    6: 14,  # TANGENT     -> TANGENT
    7: 15,  # BINORMAL    -> BINORMAL
    10: 3,  # COLOR       -> VERTEXCOLOR
}


def _xbox_decltype_info(d3dtype, log: Optional[LogFn] = None):
    """Maps packed Xbox 360 VertexFormat constants to (PSG vertex_type, byte_size)."""
    if log is None:
        log = print
    table = {
        2917284: (0x02, 4),   # FLOAT1     -> PSG FLOAT, 4 bytes
        2892709: (0x02, 8),   # FLOAT2     -> PSG FLOAT, 8 bytes
        2761657: (0x02, 12),  # FLOAT3     -> PSG FLOAT, 12 bytes
        1713062: (0x02, 16),  # FLOAT4     -> PSG FLOAT, 16 bytes
        1583238: (0x04, 4),   # D3DCOLOR   -> PSG UBYTE4, 4 bytes
        1712774: (0x04, 4),   # UBYTE4     -> PSG UBYTE4 (bone indices), 4 bytes
        2892633: (0x05, 4),   # SHORT2     -> PSG SHORT, 4 bytes
        1712986: (0x05, 8),   # SHORT4     -> PSG SHORT, 8 bytes
        1712262: (0x07, 4),   # UBYTE4N    -> PSG UBYTE4N (weights), 4 bytes
        1712518: (0x07, 4),   # BYTE4N     -> PSG UBYTE4N, 4 bytes
        2892121: (0x05, 4),   # SHORT2N    -> PSG SHORT, 4 bytes
        1712474: (0x05, 8),   # SHORT4N    -> PSG SHORT, 8 bytes  (position: 4 shorts)
        2891865: (0x05, 4),   # USHORT2N   -> PSG SHORT, 4 bytes
        1712218: (0x05, 8),   # USHORT4N   -> PSG SHORT, 8 bytes
        2761351: (0x06, 4),   # UDEC3      -> PSG packed, 4 bytes
        2761095: (0x06, 4),   # DEC3N      -> PSG packed normal, 4 bytes
        2761104: (0x06, 4),   # DEC3N variant (Xbox GPU endian variant) -> treat as DEC3N
        2892639: (0x03, 4),   # FLOAT16_2  -> PSG HALF, 4 bytes
        1712992: (0x03, 8),   # FLOAT16_4  -> PSG HALF, 8 bytes
    }
    result = table.get(d3dtype)
    if result is None:
        log(f"WARNING: Unknown Xbox VertexFormat 0x{d3dtype:08X} ({d3dtype}), defaulting to SHORT 4B")
        return (0x05, 4)
    return result


class Rx2TemplateParser:
    RW_GRAPHICS_VERTEXDESCRIPTOR = 0x000200E9
    RW_GRAPHICS_VERTEXBUFFER = 0x000200EA
    RW_GRAPHICS_INDEXBUFFER = 0x000200EB
    PEGASUS_OPTIMESHDATA = 0x00EB0023

    # OptiMesh block layout (reversed from sk82_na_m.xex template dumps):
    #   +0x00..0x1F  bbox (min xyz + pad + max xyz + pad, 4 floats each)
    #   +0x20..0x43  flags / misc u32 array
    #   +0x44        u32 offset to sub-block (=0x60)
    #   +0x48        u32 palette_count           <- PATCH with new palette size
    #   +0x4C        u32 palette_offset (=0x70)
    #   +0x50        u32 morph_count (=2 in donor)
    #   +0x54        u32 morph_offset (=0x88)
    #   +0x58        u32 string_table_offset (=0xA8)
    #   +0x60..0x6B  sub-block header (flags)
    #   +0x6C        u32 num_indices             <- PATCH with faces*3
    #   +0x70..      bone palette (u16 BE, exactly palette_count entries, NO terminator)
    #   +0x88..      morph metadata (u64 hash + u32 idx + u32 stroff) x morph_count
    #   +0xA8..      string table
    OM_NUM_VERTS_OFFSET = 0x20   # m_uiNumVerts
    OM_PALETTE_COUNT_OFFSET = 0x48  # m_uiNumRemapIndices (== bone palette count)
    OM_PALETTE_PTR_OFFSET = 0x4C  # m_pRemapTable (relative)
    OM_MORPH_COUNT_OFFSET = 0x50  # m_uiNumBlendShapes
    OM_NUM_INDICES_OFFSET = 0x6C  # island[0].num_indices
    OM_PALETTE_DATA_OFFSET = 0x70  # palette data (u16 BE array)
    OM_PALETTE_DATA_END = 0x88  # hard limit: palette must not run past morph table start

    def __init__(self, file_path, log: Optional[LogFn] = None):
        self._log = log if log is not None else print

        with open(file_path, 'rb') as f:
            self.data = bytearray(f.read())

        # Primary (first) VB/IB/VDesc — the one that receives new mesh data.
        self.vdes_offset = -1
        self.vertex_offset = -1
        self.face_offset = -1
        self.vbuff_dict_ptr = -1
        self.ibuff_dict_ptr = -1
        # File offset of u32 main_base pointer (trim point)
        self.main_baseresource_size = 0x44
        # File offset of u32 total-graphics-disposable-size field
        self.graphics_baseresource_size = 0x54
        self.vertex_buffer_size_offset = -1
        self.index_size_offset = -1
        self.index_count_offset = -1
        self.vb_fetch_const1_offset = -1
        self.optimesh_block_start = -1
        self.optimesh_palette_count_offset = -1
        self.optimesh_numindices_offset = -1
        self.optimesh_morph_count_offset = -1
        self.optimesh_block_size = 0
        self.bone_palette_data_offset = -1
        self.palette_max_entries = 0  # 12-slot region before morph hash table
        self.palette_hard_max_entries = 0  # whole block after +0x70 (morphs disabled)
        self.main_base = 0

        # All VB objects (for multi-VB templates: blend shape / shadow passes)
        self.all_vbs = []
        self.all_ibs = []
        self.all_vdes = []
        # All base-resource dict entries for graphics (VB/IB raw data)
        self.graphics_base_resources = []

        self.bone_names = []
        self.bone_palette = []

        self._parse_dictionary_and_skeleton()
        self.layout = self._parse_vdes()
        self._finalize_vb_strides()

    def _u16_be(self, offset):
        return struct.unpack('>H', self.data[offset:offset+2])[0]

    def _u32_be(self, offset):
        return struct.unpack('>I', self.data[offset:offset+4])[0]

    def _is_base_resource(self, type_id):
        return 0x00010030 <= type_id <= 0x0001003F

    def _parse_dictionary_and_skeleton(self):
        try:
            num_entries = self._u32_be(0x20)
            dict_start = self._u32_be(0x30)
            main_base = self._u32_be(0x44)
            self.main_base = main_base

            self._log("--- RX2 Dictionary & Skeleton Parsing ---")
            self._log(f"Number of entries: {num_entries}")
            self._log(f"Dictionary starts at offset: 0x{dict_start:X}")
            self._log(f"Main resource base at offset: 0x{main_base:X}")

            dict_entries = []
            for i in range(num_entries):
                entry_offset = dict_start + (i * 0x18)
                entry = {
                    "ptr": self._u32_be(entry_offset + 0x00),
                    "size": self._u32_be(entry_offset + 0x08),
                    "type_id": self._u32_be(entry_offset + 0x14),
                    "offset": entry_offset,
                }
                dict_entries.append(entry)

            carrier_entry = self._find_carrier(dict_entries, main_base)
            if carrier_entry:
                self._parse_carrier(carrier_entry, main_base)
            else:
                self._log("Warning: Could not find a valid skeleton 'Carrier' block. Skinning will not be possible.")

            palette_entry = next((e for e in dict_entries if e["type_id"] == self.PEGASUS_OPTIMESHDATA), None)
            if palette_entry:
                self._parse_bone_palette(palette_entry, main_base)
            else:
                self._log("Warning: Bone palette (type 0x00EB0023) not found. Assuming identity mapping.")
                self.bone_palette = list(range(len(self.bone_names)))

            # RX2 VB object (vertexBufferxbox) has no br_index pointer — the
            # base-resources appear in dict order after the object blocks.
            base_res = [e for e in dict_entries if self._is_base_resource(e["type_id"])]
            self.graphics_base_resources = base_res
            br_idx = 0

            for entry in dict_entries:
                type_id = entry["type_id"]
                ptr = entry["ptr"]
                block_start = (main_base + ptr) if self._is_base_resource(type_id) else ptr

                if type_id == self.RW_GRAPHICS_VERTEXDESCRIPTOR:
                    self.all_vdes.append({"obj_start": block_start, "entry": entry})
                    if self.vdes_offset == -1:
                        self.vdes_offset = block_start
                        self._log(f"VDesc[0] at 0x{block_start:X}")

                elif type_id == self.RW_GRAPHICS_VERTEXBUFFER:
                    br = base_res[br_idx]; br_idx += 1
                    vb_info = {
                        "obj_start": block_start,
                        "buffersize_offset": block_start + 0x20,
                        "fetch1_offset": block_start + 0x1C,
                        "br_dict_offset": br["offset"],
                        "br_ptr": br["ptr"],
                        "br_size": br["size"],
                        "raw_offset": main_base + br["ptr"],
                        "stride": 0,  # filled in _finalize_vb_strides
                    }
                    self.all_vbs.append(vb_info)
                    if self.vertex_offset == -1:
                        self.vertex_offset = main_base + br["ptr"]
                        self.vbuff_dict_ptr = br["offset"]
                        self.vertex_buffer_size_offset = block_start + 0x20
                        self.vb_fetch_const1_offset = block_start + 0x1C
                    self._log(f"VB[{len(self.all_vbs)-1}] obj=0x{block_start:X} raw=0x{vb_info['raw_offset']:X} size=0x{br['size']:X}")

                elif type_id == self.RW_GRAPHICS_INDEXBUFFER:
                    br = base_res[br_idx]; br_idx += 1
                    ib_info = {
                        "obj_start": block_start,
                        "size_offset": block_start + 0x1C,
                        "count_offset": block_start + 0x20,
                        "br_dict_offset": br["offset"],
                        "br_ptr": br["ptr"],
                        "br_size": br["size"],
                        "raw_offset": main_base + br["ptr"],
                    }
                    self.all_ibs.append(ib_info)
                    if self.face_offset == -1:
                        self.face_offset = main_base + br["ptr"]
                        self.ibuff_dict_ptr = br["offset"]
                        self.index_size_offset = block_start + 0x1C
                        self.index_count_offset = block_start + 0x20
                    self._log(f"IB[{len(self.all_ibs)-1}] obj=0x{block_start:X} raw=0x{ib_info['raw_offset']:X} size=0x{br['size']:X}")

                elif type_id == self.PEGASUS_OPTIMESHDATA and self.optimesh_block_start == -1:
                    self.optimesh_block_start = block_start
                    self.optimesh_numverts_offset = block_start + self.OM_NUM_VERTS_OFFSET
                    self.optimesh_palette_count_offset = block_start + self.OM_PALETTE_COUNT_OFFSET
                    self.optimesh_numindices_offset = block_start + self.OM_NUM_INDICES_OFFSET
                    self.bone_palette_data_offset = block_start + self.OM_PALETTE_DATA_OFFSET
                    self.optimesh_block_size = entry["size"]
                    self.optimesh_morph_count_offset = block_start + self.OM_MORPH_COUNT_OFFSET
                    # Soft limit: palette must not touch morph data.
                    self.palette_max_entries = (self.OM_PALETTE_DATA_END - self.OM_PALETTE_DATA_OFFSET) // 2
                    # Hard limit: palette can occupy the entire block after +0x70
                    # if morph_count at +0x50 is zeroed (engine then ignores morph data).
                    self.palette_hard_max_entries = (entry["size"] - self.OM_PALETTE_DATA_OFFSET) // 2

            if self.vdes_offset == -1:
                raise ValueError("Could not find a Vertex Descriptor (0x000200E9) in the RX2 template.")
            if self.vertex_offset == -1:
                raise ValueError("Could not find a Vertex Buffer (0x000200EA) in the RX2 template.")
            if self.face_offset == -1:
                raise ValueError("Could not find an Index Buffer (0x000200EB) in the RX2 template.")

            self._log(f"\nAuto-detected Vertex Buffer Offset: 0x{self.vertex_offset:X}")
            self._log(f"Auto-detected Index Buffer Offset:  0x{self.face_offset:X}")

        except (IndexError, struct.error) as e:
            raise ValueError(f"Failed to parse RX2 dictionary. The template may be corrupt or invalid. Details: {e}")

    def _find_carrier(self, dict_entries, main_base):
        """Finds the skeleton carrier block by checking for a valid header signature."""
        for entry in dict_entries:
            block_start = (main_base + entry["ptr"]) if self._is_base_resource(entry["type_id"]) else entry["ptr"]
            block_end = block_start + entry["size"]

            header_offset = block_start + 0x20
            if header_offset + 0x24 > len(self.data):
                continue

            bone_count = self._u16_be(header_offset + 0x14)
            if not (0 < bone_count <= 512):
                continue

            off_ibm = self._u32_be(header_offset + 0x00)
            off_tbl_idx = self._u32_be(header_offset + 0x08)

            ibm_abs = block_start + off_ibm
            idx_abs = block_start + off_tbl_idx

            if (ibm_abs + bone_count * 64 <= block_end) and (idx_abs + bone_count * 4 <= block_end):
                self._log(f"[Carrier] Found potential skeleton at dict entry offset 0x{entry['offset']:X} with {bone_count} bones.")
                return entry
        return None

    def _parse_carrier(self, carrier_entry, main_base):
        """Parses bone names from the found carrier block."""
        block_start = (main_base + carrier_entry["ptr"]) if self._is_base_resource(carrier_entry["type_id"]) else carrier_entry["ptr"]
        header_offset = block_start + 0x20

        bone_count = self._u16_be(header_offset + 0x14)
        off_tbl_idx = self._u32_be(header_offset + 0x08)
        idx_abs = block_start + off_tbl_idx

        self._log(f"\n--- Parsing Donor Skeleton ({bone_count} bones) ---")
        self.bone_names = []
        for i in range(bone_count):
            rel_offset = self._u32_be(idx_abs + 4 * i)
            name_offset = block_start + rel_offset

            end_offset = self.data.find(b'\x00', name_offset)
            name = self.data[name_offset:end_offset].decode('ascii', errors='ignore')
            self.bone_names.append(name)
            self._log(f"  Bone {i:3}: {name}")

    def _parse_bone_palette(self, palette_entry, main_base):
        """Parses the bone palette (map from palette index to global bone index)."""
        block_start = (main_base + palette_entry["ptr"]) if self._is_base_resource(palette_entry["type_id"]) else palette_entry["ptr"]
        # RX2 OptiMesh: palette_count at +0x48, palette data at +0x70 (no terminator).
        palette_count = self._u32_be(block_start + self.OM_PALETTE_COUNT_OFFSET)
        palette_offset = block_start + self.OM_PALETTE_DATA_OFFSET
        self.bone_palette_data_offset = palette_offset
        self.bone_palette = []
        # Trust count from +0x48 rather than walking-till-terminator (no terminator).
        for i in range(palette_count):
            p = palette_offset + i * 2
            if p + 1 >= len(self.data):
                break
            global_index = self._u16_be(p)
            if global_index >= len(self.bone_names):
                self._log(f"  Warning: palette entry {i} = {global_index} >= bone count, stopping.")
                break
            self.bone_palette.append(global_index)
        self._log(f"\n--- Parsing Donor Bone Palette ({len(self.bone_palette)} entries, declared {palette_count}) ---")
        for i, global_idx in enumerate(self.bone_palette):
            if i < 20:
                self._log(f"  Palette Slot {i:3} -> Global Bone {global_idx:3} ({self.bone_names[global_idx]})")
        if len(self.bone_palette) > 20:
            self._log("  ...")

    def _finalize_vb_strides(self):
        """
        Compute each VB's stride from its matching VDesc. Assumes VDescs appear
        in the same dict order as VBs (stream index 0 per VB).
        """
        for i, vb in enumerate(self.all_vbs):
            stride = 0
            if i < len(self.all_vdes):
                vdesc = self.all_vdes[i]["obj_start"]
                num_elements = self._u16_be(vdesc + 8)
                for j in range(num_elements):
                    e = vdesc + 0x10 + j * 0x10
                    offs = self._u16_be(e + 2)
                    fmt = self._u32_be(e + 4)
                    _, size = _xbox_decltype_info(fmt, log=self._log)
                    stride = max(stride, offs + size)
            if stride == 0:
                # Fallback: derive from original br_size (single-vertex buffer assumption)
                stride = vb["br_size"]
            vb["stride"] = stride
            self._log(f"  VB[{i}] stride = {stride} bytes")

    def _parse_vdes(self):
        """
        Parse Xbox vertex descriptor (renderengine::VertexDescriptor).
        Element layout (16 bytes):
          [0:2]  uint16 stream
          [2:4]  uint16 offset   <- byte offset of attribute in vertex
          [4:8]  uint32 format   <- Xbox 360 VertexFormat (packed GPU constant)
          [8]    uint8  method
          [9]    uint8  usage    <- D3DDECLUSAGE semantic
          [10]   uint8  usageIndex
          [11]   uint8  type
          [12:16]uint32 elementClass
        Header: m_numElements at header+8.
        Values are remapped to PSG-compatible IDs so the packer works unchanged.
        """
        header_offset = self.vdes_offset
        num_elements = self._u16_be(header_offset + 8)
        elements_offset = header_offset + 16  # header is 16 bytes
        parsed_elements = []
        strides = set()

        self._log("\n--- Vertex Descriptor Parsing ---")
        self._log(f"Number of elements: {num_elements}")
        self._log("Raw Element Data (after remap to PSG-compatible values):")
        self._log("  Stream | Type (ID) | Vtx Type | Offset | Stride | D3DDECLTYPE | D3DDECLUSAGE")
        self._log("  -------------------------------------------------------------------------")

        for i in range(num_elements):
            elem_offset = elements_offset + (i * 16)  # Xbox: 16 bytes per element

            xbox_stream = self._u16_be(elem_offset + 0)
            xbox_boffset = self._u16_be(elem_offset + 2)
            xbox_d3dtype = self._u32_be(elem_offset + 4)
            xbox_usage = self.data[elem_offset + 9]

            psg_vtx_type, elem_size = _xbox_decltype_info(xbox_d3dtype, log=self._log)
            computed_stride = xbox_boffset + elem_size

            psg_type = _XBOX_USAGE_TO_PSG_TYPE.get(xbox_usage, xbox_usage)

            e = VDElem(
                vertex_type=psg_vtx_type,
                num_components=1,
                stream=xbox_stream,
                offset=xbox_boffset,
                stride=computed_stride,
                type=psg_type,
                class_id=self._u32_be(elem_offset + 12),
                raw_format=xbox_d3dtype,
            )
            parsed_elements.append(e)
            if computed_stride > 0:
                strides.add(computed_stride)

            self._log(f"  {xbox_stream:<6} | {psg_type:<10} | 0x{psg_vtx_type:02X}     | 0x{xbox_boffset:02X}   | {computed_stride:<6} | D3D={xbox_d3dtype}  | usage={xbox_usage}")

        if not strides:
            raise ValueError("Vertex descriptor in RX2 template has no valid stride defined.")

        resolved_stride = max(strides)
        self._log(f"\nDetermined vertex stride from RX2 template: {resolved_stride} bytes")

        return VertexLayout(stride=resolved_stride, elements=parsed_elements)


def normalize_bone_name(name: str | None) -> str | None:
    if name is None:
        return None
    # Remove all non-alphanumerics and lowercase to create a robust canonical form
    return ''.join(ch for ch in name if ch.isalnum()).lower()


def remap_skin_to_donor_palette(gltf_joints, gltf_weights, glb_bone_map,
                                donor_bone_names, donor_bone_palette,
                                log: Optional[LogFn] = None):
    """
    Remaps GLB joints onto the donor's exact bone palette. Weights to bones
    outside the donor palette are dropped (Skate 2 Xbox caches ~12 skinning
    matrices per render group; palettes larger than the donor's produce bones
    that are never uploaded to GPU constants).
    Returns (joints, weights, new_palette) where new_palette is the reused
    donor palette (list of global bone indices).
    """
    if log is None:
        log = print

    # Map normalized bone name -> donor global bone index using the FULL
    # skeleton, not just the donor's palette.
    donor_name_to_global_idx = {normalize_bone_name(name): i for i, name in enumerate(donor_bone_names)}

    # Map each glTF bone index -> donor global bone index
    gltf_to_global_map = {}
    unmapped_bones = set()
    log("--- GLTF to Donor Skeleton Bone Index Mapping ---")
    log("glTF Bone Name       -> glTF Idx -> Donor Global Idx")
    for gltf_idx, gltf_name in glb_bone_map.items():
        norm_name = normalize_bone_name(gltf_name)
        global_idx = donor_name_to_global_idx.get(norm_name)
        if global_idx is not None:
            gltf_to_global_map[gltf_idx] = global_idx
        else:
            unmapped_bones.add(gltf_name)
        log(f"{str(gltf_name):<20} -> {gltf_idx:<9} -> {str(global_idx)}")
    log("---------------------------------------------------------------------")

    if unmapped_bones:
        log(f"\nWarning: {len(unmapped_bones)} bones from the GLTF file were not found in the donor skeleton and were ignored:")
        for bone in sorted(list(unmapped_bones)):
            log(f" - {bone}")

    new_palette = list(donor_bone_palette)
    global_to_new_palette_idx = {g: slot for slot, g in enumerate(new_palette)}

    log(f"\n--- Bone Palette (reusing donor's {len(new_palette)} entries) ---")
    for slot, g in enumerate(new_palette):
        log(f"  Palette Slot {slot:3} -> Global Bone {g:3} ({donor_bone_names[g]})")

    dropped_bones = set()
    for indices, weights in zip(gltf_joints, gltf_weights):
        for i in range(4):
            if float(weights[i]) > 1e-6:
                g = gltf_to_global_map.get(int(indices[i]))
                if g is not None and g not in global_to_new_palette_idx:
                    dropped_bones.add(donor_bone_names[g])
    if dropped_bones:
        log(f"\nDropped weights to {len(dropped_bones)} bones not in donor palette:")
        for b in sorted(dropped_bones):
            log(f"  - {b}")

    # Second pass: remap vertex bone indices to new palette slots
    final_palette_indices = []
    final_weights = []

    for indices, weights in zip(gltf_joints, gltf_weights):
        weight_by_slot = {}
        for i in range(4):
            w = float(weights[i])
            if w <= 1e-6:
                continue
            global_idx = gltf_to_global_map.get(int(indices[i]))
            if global_idx is not None:
                slot = global_to_new_palette_idx.get(global_idx)
                if slot is not None:
                    weight_by_slot[slot] = weight_by_slot.get(slot, 0.0) + w

        sorted_pairs = sorted(weight_by_slot.items(), key=lambda x: x[1], reverse=True)[:4]

        palette_indices_per_vertex = [0] * 4
        weights_per_vertex = [0.0] * 4

        for i, (slot, w) in enumerate(sorted_pairs):
            palette_indices_per_vertex[i] = int(slot)
            weights_per_vertex[i] = float(w)

        total_weight = sum(weights_per_vertex)
        if total_weight > 1e-6:
            inv = 1.0 / total_weight
            weights_per_vertex = [w * inv for w in weights_per_vertex]
        else:
            palette_indices_per_vertex = [0, 0, 0, 0]
            weights_per_vertex = [1.0, 0.0, 0.0, 0.0]

        final_palette_indices.append(palette_indices_per_vertex)
        final_weights.append(weights_per_vertex)

    return (numpy.array(final_palette_indices, dtype=numpy.uint8),
            numpy.array(final_weights, dtype=numpy.float32),
            new_palette)


def pack_normal_dec3n(n):
    """
    Xbox 360 DEC3N: 11:11:10 signed, big-endian u32.
    Layout: X in bits [10:0] (11-bit signed), Y in bits [21:11] (11-bit signed),
            Z in bits [31:22] (10-bit signed).
    Range: X,Y in [-1024, 1023], Z in [-512, 511].
    """
    nx, ny, nz = n
    nx = max(-1.0, min(1.0, float(nx)))
    ny = max(-1.0, min(1.0, float(ny)))
    nz = max(-1.0, min(1.0, float(nz)))
    # Scale to integer range (11-bit: max 1023, 10-bit: max 511)
    ix = int(round(nx * 1023.0))
    iy = int(round(ny * 1023.0))
    iz = int(round(nz * 511.0))
    # Clamp to signed ranges
    ix = max(-1024, min(1023, ix))
    iy = max(-1024, min(1023, iy))
    iz = max(-512, min(511, iz))
    # Convert to unsigned 2's complement for masking
    if ix < 0: ix += (1 << 11)
    if iy < 0: iy += (1 << 11)
    if iz < 0: iz += (1 << 10)
    packed_val = ((iz & 0x3FF) << 22) | ((iy & 0x7FF) << 11) | (ix & 0x7FF)
    return struct.pack('>I', packed_val)


def make_vertex_bin_dynamic(vertices, uvs, normals, tangents, binormals,
                            joints, weights, layout: VertexLayout, scale_xyz=256):
    """
    Packs vertex data into the Xbox 360 vertex format described by `layout`.
    RX2-specific behaviour vs PSG:
      - XYZ:         4 shorts (x,y,z,w=0), format SHORT4N.
      - NORMAL/TANGENT/BINORMAL: DEC3N 11:11:10 signed big-endian u32.
      - WEIGHTS:     UBYTE4N — 4 unsigned bytes [0,255].
      - BONEINDICES: UBYTE4  — 4 unsigned bytes, raw palette indices.
      - TEX0:        SHORT2N signed or USHORT2N unsigned (detected via raw_format).
    """
    output = bytearray()
    elem_map = {
        'XYZ': 0, 'WEIGHTS': 1, 'NORMAL': 2, 'VERTEXCOLOR': 3, 'SPECULAR': 4,
        'BONEINDICES': 7, 'TEX0': 8, 'TEX1': 9, 'TEX2': 10, 'TEX3': 11, 'TEX4': 12, 'TEX5': 13,
        'TANGENT': 14, 'BINORMAL': 15,
    }
    is_skinned = joints is not None and weights is not None

    for i in range(len(vertices)):
        vertex_bytes = bytearray(layout.stride)
        for elem in layout.elements:
            packed_data = b''

            if elem.type == elem_map['XYZ']:
                # RX2: 4 shorts (x,y,z,w). W is typically 0 or a GPU remap value.
                x_s = max(-32768, min(32767, int(vertices[i][0] * scale_xyz)))
                y_s = max(-32768, min(32767, int(vertices[i][1] * scale_xyz)))
                z_s = max(-32768, min(32767, int(vertices[i][2] * scale_xyz)))
                if elem.vertex_type in [0x01, 0x05]:
                    packed_data = struct.pack('>hhhh', x_s, y_s, z_s, 0)
                elif elem.vertex_type == 0x02:
                    packed_data = struct.pack('>fff', *vertices[i])

            elif elem.type == elem_map['NORMAL']:
                if elem.vertex_type == 0x06:
                    packed_data = pack_normal_dec3n(normals[i])

            elif elem.type == elem_map['TANGENT']:
                if elem.vertex_type == 0x06:
                    packed_data = pack_normal_dec3n(tangents[i])

            elif elem.type == elem_map['BINORMAL']:
                if elem.vertex_type == 0x06:
                    packed_data = pack_normal_dec3n(binormals[i])

            elif elem.type == elem_map['TEX0']:
                u, v = uvs[i]
                if elem.vertex_type == 0x03:
                    packed_data = struct.pack('>ee', numpy.float16(u), numpy.float16(v))
                elif elem.vertex_type in [0x01, 0x05]:
                    _USHORT2N = 2891865   # USHORT2N: [0,1] -> [0, 65535] unsigned
                    _USHORT4N = 1712218   # USHORT4N: [0,1] -> [0, 65535] unsigned
                    if elem.raw_format in (_USHORT2N, _USHORT4N):
                        u_u = max(0, min(65535, int(round(max(0.0, min(1.0, u)) * 65535.0))))
                        v_u = max(0, min(65535, int(round(max(0.0, min(1.0, v)) * 65535.0))))
                        packed_data = struct.pack('>HH', u_u, v_u)
                    else:
                        u_s = max(-32768, min(32767, int(round(u * 32767.0))))
                        v_s = max(-32768, min(32767, int(round(v * 32767.0))))
                        packed_data = struct.pack('>hh', u_s, v_s)

            elif elem.type == elem_map['WEIGHTS']:
                w = weights[i] if is_skinned else [1.0, 0.0, 0.0, 0.0]
                if elem.vertex_type == 0x02:
                    packed_data = struct.pack('>ffff', *w)
                elif elem.vertex_type in [0x04, 0x07]:
                    w_u8 = [int(round(max(0.0, min(1.0, float(c))) * 255.0)) for c in w]
                    packed_data = struct.pack('>BBBB', *w_u8)

            elif elem.type == elem_map['BONEINDICES']:
                j = joints[i] if is_skinned else [0, 0, 0, 0]
                if elem.vertex_type in [0x04, 0x07]:
                    j_u8 = [int(max(0, min(255, int(c)))) for c in j]
                    packed_data = struct.pack('>BBBB', *j_u8)

            elif elem.type == elem_map['VERTEXCOLOR']:
                if elem.vertex_type in [0x04, 0x07]:
                    packed_data = struct.pack('>BBBB', 255, 255, 255, 255)

            elif elem.type == elem_map['SPECULAR']:
                if elem.vertex_type in [0x04, 0x07]:
                    packed_data = struct.pack('>BBBB', 0, 0, 0, 255)

            if packed_data:
                vertex_bytes[elem.offset:elem.offset + len(packed_data)] = packed_data
        output.extend(vertex_bytes)
    return output


def make_face_bin(faces):
    output = bytearray()
    for face in faces:
        for idx in face:
            output.extend(struct.pack('>H', idx))
    return output


def detect_bin_path(gltf_path: str) -> Optional[str]:
    """Resolves the companion .bin file for a .gltf from its buffer URI."""
    if not gltf_path.lower().endswith('.gltf'):
        return None
    try:
        gltf = pygltflib.GLTF2.load(gltf_path)
        if gltf.buffers and gltf.buffers[0].uri:
            return os.path.join(os.path.dirname(gltf_path), gltf.buffers[0].uri)
    except Exception:
        pass
    return None


def parse_gltf_to_data(gltf_path: str, bin_path: Optional[str] = None,
                       log: Optional[LogFn] = None):
    """
    Loads a glTF/GLB and returns vertex/face/bone arrays as plain lists.
    RX2 difference from PSG: binormal = cross(tangent, normal) for Xbox.
    """
    if log is None:
        log = print

    gltf = pygltflib.GLTF2.load(gltf_path)

    blob = None
    if gltf_path.lower().endswith('.glb'):
        blob = gltf.binary_blob()
    elif bin_path and os.path.exists(bin_path):
        with open(bin_path, 'rb') as f:
            blob = f.read()
    else:
        auto = detect_bin_path(gltf_path)
        if auto and os.path.exists(auto):
            log(f"Auto-detected binary file: {auto}")
            with open(auto, 'rb') as f:
                blob = f.read()

    if blob is None:
        raise ValueError("Could not load binary data.")
    if not gltf.meshes:
        raise ValueError("No meshes found in file.")
    if len(gltf.meshes) > 1 or len(gltf.meshes[0].primitives) > 1:
        log("Warning: Model has multiple parts. Using first primitive of first mesh.")

    primitive = gltf.meshes[0].primitives[0]

    def get_accessor_data(accessor_id):
        accessor = gltf.accessors[accessor_id]
        buffer_view = gltf.bufferViews[accessor.bufferView]
        offset = (buffer_view.byteOffset or 0) + (accessor.byteOffset or 0)
        dtype_map = {5120: numpy.int8, 5121: numpy.uint8, 5122: numpy.int16,
                     5123: numpy.uint16, 5125: numpy.uint32, 5126: numpy.float32}
        dtype = dtype_map[accessor.componentType]
        num_components = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}[accessor.type]
        data = numpy.frombuffer(blob, dtype=dtype, count=accessor.count * num_components, offset=offset)
        return data.reshape(accessor.count, num_components) if num_components > 1 else data

    raw_vertices = get_accessor_data(primitive.attributes.POSITION)
    raw_uvs = (get_accessor_data(primitive.attributes.TEXCOORD_0)
               if primitive.attributes.TEXCOORD_0 is not None
               else numpy.zeros((len(raw_vertices), 2), dtype=numpy.float32))
    indices = get_accessor_data(primitive.indices)
    faces_indices = indices.reshape(-1, 3)

    if primitive.attributes.NORMAL is not None:
        raw_normals = get_accessor_data(primitive.attributes.NORMAL)
    else:
        log("Warning: Model missing NORMAL attribute. Computing per-vertex normals from geometry.")
        raw_normals = numpy.zeros_like(raw_vertices)
        v0 = raw_vertices[faces_indices[:, 0]]
        v1 = raw_vertices[faces_indices[:, 1]]
        v2 = raw_vertices[faces_indices[:, 2]]
        face_normals = numpy.cross(v1 - v0, v2 - v0)
        for i in range(3):
            numpy.add.at(raw_normals, faces_indices[:, i], face_normals)
        lengths = numpy.linalg.norm(raw_normals, axis=1, keepdims=True)
        raw_normals = raw_normals / (lengths + 1e-9)
        raw_normals = raw_normals.astype(numpy.float32)

    raw_joints, raw_weights, glb_bone_map = None, None, None

    if primitive.attributes.JOINTS_0 is not None and primitive.attributes.WEIGHTS_0 is not None:
        raw_joints = get_accessor_data(primitive.attributes.JOINTS_0)
        raw_weights = get_accessor_data(primitive.attributes.WEIGHTS_0)

        weights_accessor = gltf.accessors[primitive.attributes.WEIGHTS_0]
        if weights_accessor.componentType == 5121:  # UBYTE
            raw_weights = raw_weights.astype(numpy.float32) / 255.0
        elif weights_accessor.componentType == 5123:  # USHORT
            raw_weights = raw_weights.astype(numpy.float32) / 65535.0

        skin_index = None
        for node in gltf.nodes:
            if node.mesh == 0 and node.skin is not None:
                skin_index = node.skin
                break

        if skin_index is None:
            raise ValueError("Skinned mesh data found, but no node in the GLB uses this mesh with a skin.")

        if gltf.skins and len(gltf.skins) > skin_index:
            skin = gltf.skins[skin_index]
            glb_bone_map = {i: gltf.nodes[joint_index].name for i, joint_index in enumerate(skin.joints)}
        else:
            raise ValueError("Skinned data found, but no valid skin definition was found in the GLB.")

    tangent_acc = numpy.zeros_like(raw_vertices)
    for i0, i1, i2 in faces_indices:
        p0, p1, p2 = raw_vertices[[i0, i1, i2]]
        uv0, uv1, uv2 = raw_uvs[[i0, i1, i2]]
        edge1, edge2 = p1 - p0, p2 - p0
        delta_uv1, delta_uv2 = uv1 - uv0, uv2 - uv0
        f = delta_uv1[0] * delta_uv2[1] - delta_uv2[0] * delta_uv1[1]
        if abs(f) > 1e-6:
            r = 1.0 / f
            tangent = (edge1 * delta_uv2[1] - edge2 * delta_uv1[1]) * r
            tangent_acc[[i0, i1, i2]] += tangent

    t_ortho = tangent_acc - raw_normals * numpy.sum(tangent_acc * raw_normals, axis=1, keepdims=True)
    final_raw_tangents = t_ortho / (numpy.linalg.norm(t_ortho, axis=1, keepdims=True) + 1e-9)
    # RX2: cross(tangent, normal). PSG uses cross(normal, tangent).
    final_raw_binormals = numpy.cross(final_raw_tangents, raw_normals)

    # Keep the glTF's indexed structure (avoids per-face vertex explosion).
    is_skinned = raw_joints is not None
    final_vertices = [raw_vertices[i] for i in range(len(raw_vertices))]
    final_normals = [raw_normals[i] for i in range(len(raw_normals))]
    final_uvs = [raw_uvs[i] for i in range(len(raw_uvs))]
    final_tangents = [final_raw_tangents[i] for i in range(len(final_raw_tangents))]
    final_binormals = [final_raw_binormals[i] for i in range(len(final_raw_binormals))]
    joints_out = [raw_joints[i] for i in range(len(raw_joints))] if is_skinned else None
    weights_out = [raw_weights[i] for i in range(len(raw_weights))] if is_skinned else None
    final_faces = faces_indices.tolist()

    return (final_vertices, final_uvs, final_normals,
            final_tangents, final_binormals,
            final_faces, joints_out, weights_out, glb_bone_map)


def convert_glb_to_rx2(glb_path: str, rx2_template_path: str, output_path: str,
                       scale_xyz: float = 256.0, bin_path: Optional[str] = None,
                       log: Optional[LogFn] = None) -> ConversionResult:
    """
    Converts a glTF/GLB model into a Skate 3 Xbox 360 (.rx2) file by patching
    a donor .rx2 template. Returns a ConversionResult on success.

    Raises ValueError on invalid input/template and OSError on I/O failures.
    """
    if log is None:
        log = print

    if not all([glb_path, rx2_template_path, output_path]):
        raise ValueError("glb_path, rx2_template_path and output_path are all required.")

    scale_xyz = float(scale_xyz)

    log("--- Starting RX2 Conversion Process ---\n")
    log(f"Parsing RX2 Template: {os.path.basename(rx2_template_path)}")
    template = Rx2TemplateParser(rx2_template_path, log=log)
    log("RX2 Template parsed successfully.\n")

    log(f"Loading glTF/GLB file: {os.path.basename(glb_path)}")
    model_data = parse_gltf_to_data(glb_path, bin_path, log=log)
    (final_vertices, final_uvs, final_normals, final_tangents, final_binormals,
     final_faces, final_joints, final_weights, glb_bone_map) = model_data

    log("--- GLTF Data Loaded ---")
    log(f"Total Vertices: {len(final_vertices)}")
    log(f"Total Faces: {len(final_faces)}")
    is_skinned = final_joints is not None
    if is_skinned:
        if not template.bone_names or not template.bone_palette:
            raise ValueError("Skinning data found in GLB, but no skeleton or palette was loaded from the donor RX2.")
        log(f"Skinned model detected with {len(glb_bone_map)} bones.")
    else:
        log("No skinning data found in GLB. Processing as a static mesh.")
    log("\n")

    remapped_joints, remapped_weights, new_palette = None, None, []
    if is_skinned:
        log("--- Bone & Skin Remapping (Donor-based) ---")
        remapped_joints, remapped_weights, new_palette = remap_skin_to_donor_palette(
            final_joints, final_weights, glb_bone_map,
            template.bone_names, template.bone_palette, log=log)
        log("Bone remapping complete.\n")

        log("--- Sample Vertex Data (First 10) ---")
        for i in range(min(10, len(final_vertices))):
            log(f"Vertex {i}:")
            log(f"  Position:  ({final_vertices[i][0]:.3f}, {final_vertices[i][1]:.3f}, {final_vertices[i][2]:.3f})")
            log(f"  Normal:    ({final_normals[i][0]:.3f}, {final_normals[i][1]:.3f}, {final_normals[i][2]:.3f})")
            log(f"  UVs:       ({final_uvs[i][0]:.3f}, {final_uvs[i][1]:.3f})")
            raw_j_names = [glb_bone_map.get(idx, 'N/A') for idx in final_joints[i]]
            log(f"  Raw Joints:  {list(final_joints[i])} -> Names: {raw_j_names}")
            log(f"  Raw Weights: {[f'{w:.3f}' for w in final_weights[i]]}")
            remap_names = [template.bone_names[new_palette[int(s)]] if int(s) < len(new_palette) else '?' for s in remapped_joints[i]]
            log(f"  Remapped Palette Joints: {list(remapped_joints[i])} -> Names: {remap_names}")
            log(f"  Remapped Final Weights: {[f'{w:.3f}' for w in remapped_weights[i]]}")
            log("-----------------------------")

    log("--- Generating Binary Data Blocks ---")
    vertex_data = make_vertex_bin_dynamic(
        final_vertices, final_uvs, final_normals, final_tangents, final_binormals,
        remapped_joints, remapped_weights, template.layout,
        scale_xyz=scale_xyz)
    face_data = make_face_bin(final_faces)
    log(f"Vertex block size: {len(vertex_data)} bytes")
    log(f"Face block size: {len(face_data)} bytes\n")

    log("--- Assembling Final RX2 File ---")
    with open(rx2_template_path, 'rb') as f:
        rx2_data = bytearray(f.read())
    log(f"Read {len(rx2_data)} bytes from template.")

    # Truncate to just before the first raw graphics base-resource (main_base).
    # Everything past it gets rebuilt below.
    rx2_data = rx2_data[0:template.main_base]

    # ------------------------------------------------------------------
    # Patch OptiMesh (palette + num_indices). +0x48 = palette count,
    # +0x6C = num indices, +0x70 = palette data (no terminator).
    # ------------------------------------------------------------------
    num_indices = len(final_faces) * 3
    if template.optimesh_numindices_offset > 0:
        rx2_data[template.optimesh_numindices_offset:template.optimesh_numindices_offset+4] = \
            struct.pack(">I", num_indices)
    # m_uiNumVerts (+0x20) is left at 0 — engine derives count from
    # VB.bufferSize / stride instead.

    if is_skinned and new_palette and template.bone_palette_data_offset > 0:
        if len(new_palette) > template.palette_hard_max_entries:
            raise ValueError(
                f"Model uses {len(new_palette)} bones but donor OptiMesh block "
                f"({template.optimesh_block_size} bytes) has room for at most "
                f"{template.palette_hard_max_entries} palette entries. "
                f"Use a larger donor OptiMesh template or reduce weighted bones.")
        palette_bytes = bytearray()
        for global_idx in new_palette:
            palette_bytes += struct.pack(">H", global_idx)
        off = template.bone_palette_data_offset

        if len(new_palette) <= template.palette_max_entries:
            # Palette fits in the 12-slot region before the morph hash table
            # at +0x88. Only clear the palette slot region itself.
            clear_len = template.palette_max_entries * 2
            rx2_data[off:off + clear_len] = b'\x00' * clear_len
            rx2_data[off:off + len(palette_bytes)] = palette_bytes
        else:
            # Palette overflows into the morph region. Overwrite morph/string
            # data and disable morphs so the engine doesn't try to resolve
            # stale hash table entries.
            clear_len = template.palette_hard_max_entries * 2
            rx2_data[off:off + clear_len] = b'\x00' * clear_len
            rx2_data[off:off + len(palette_bytes)] = palette_bytes
            rx2_data[template.optimesh_morph_count_offset:template.optimesh_morph_count_offset+4] = \
                b'\x00\x00\x00\x00'
            log(f"Palette size {len(new_palette)} > {template.palette_max_entries}: "
                f"disabled morph targets at 0x{template.optimesh_morph_count_offset:X}.")

        rx2_data[template.optimesh_palette_count_offset:template.optimesh_palette_count_offset+4] = \
            struct.pack(">I", len(new_palette))
        log(f"Wrote bone palette count={len(new_palette)} at 0x{template.optimesh_palette_count_offset:X}, data at 0x{off:X}")
    elif template.optimesh_palette_count_offset > 0:
        # Not skinned: zero the palette count so engine doesn't index
        rx2_data[template.optimesh_palette_count_offset:template.optimesh_palette_count_offset+4] = b'\x00\x00\x00\x00'

    # ------------------------------------------------------------------
    # Align face byte size to 16 (matches donor encoding: 0x32F4 -> 0x3300)
    # ------------------------------------------------------------------
    ib_aligned_size = (len(face_data) + 15) & ~15
    if ib_aligned_size > len(face_data):
        face_data = face_data + bytes(ib_aligned_size - len(face_data))

    # ------------------------------------------------------------------
    # Build new raw graphics region: VB[0] + IB[0] + zero-filled VB[1..N]
    # ------------------------------------------------------------------
    new_vertex_count = len(final_vertices)
    raw_blocks = []  # list of (kind, obj_info, data_bytes)

    # Primary VB[0] gets the real vertex data
    vb0 = template.all_vbs[0]
    raw_blocks.append(("vb", vb0, bytes(vertex_data)))

    # Primary IB[0] gets the real face data
    ib0 = template.all_ibs[0]
    raw_blocks.append(("ib", ib0, bytes(face_data)))

    # Additional VBs: zero-fill with (stride * new_vertex_count) bytes.
    for i in range(1, len(template.all_vbs)):
        vbx = template.all_vbs[i]
        stride = vbx["stride"] if vbx["stride"] > 0 else 16
        new_size = stride * new_vertex_count
        new_size_aligned = (new_size + 15) & ~15
        raw_blocks.append(("vb", vbx, bytes(new_size_aligned)))

    # Additional IBs (rare; pad with zeros to preserve struct size)
    for i in range(1, len(template.all_ibs)):
        ibx = template.all_ibs[i]
        raw_blocks.append(("ib", ibx, bytes(ibx["br_size"])))

    # ------------------------------------------------------------------
    # Append raw blocks and patch every VB/IB object + BR dict entry.
    # File layout after this loop:
    #   [0, main_base)       = header + dict + small blocks (untouched)
    #   [main_base, EOF)     = concatenated raw blocks in order
    # ------------------------------------------------------------------
    running_ptr = 0  # offset relative to main_base
    total_disposable = 0
    for kind, info, data in raw_blocks:
        rx2_data.extend(data)
        size = len(data)
        total_disposable += size

        br_off = info["br_dict_offset"]
        rx2_data[br_off:br_off+4] = struct.pack(">I", running_ptr)
        rx2_data[br_off+8:br_off+12] = struct.pack(">I", size)

        if kind == "vb":
            rx2_data[info["buffersize_offset"]:info["buffersize_offset"]+4] = \
                struct.pack(">I", size)
            # FETCH_CONST[1] encoding (empirical from donor dumps):
            #   bits [1:0]    = Type (2 = vertex fetch, preserved)
            #   bits [29:2]   = (size_bytes_in_dwords << 2) — i.e. byte_size aligned to 4
            #   bit  [28]     = vertex-buffer flag (preserved)
            #   bits [31:30]  = Endian (preserved)
            fetch_off = info["fetch1_offset"]
            orig_fetch = struct.unpack(">I", rx2_data[fetch_off:fetch_off+4])[0]
            preserve_mask = 0xD0000003  # bits 31:30 + 28 + 1:0
            size_mask = 0x2FFFFFFC      # bits 29 + 27:2
            new_fetch = (orig_fetch & preserve_mask) | ((size + 2) & size_mask)
            rx2_data[fetch_off:fetch_off+4] = struct.pack(">I", new_fetch)
        else:  # ib
            rx2_data[info["size_offset"]:info["size_offset"]+4] = \
                struct.pack(">I", size)
            rx2_data[info["count_offset"]:info["count_offset"]+4] = \
                struct.pack(">I", num_indices)

        running_ptr += size

    # ------------------------------------------------------------------
    # Patch total disposable size at +0x54
    # ------------------------------------------------------------------
    rx2_data[template.graphics_baseresource_size:template.graphics_baseresource_size+4] = \
        struct.pack(">I", total_disposable)
    log(f"Total disposable graphics size: 0x{total_disposable:X} bytes across {len(raw_blocks)} blocks.")

    log(f"Final RX2 size: {len(rx2_data)} bytes")
    log(f"Writing output to: {output_path}")
    with open(output_path, 'wb') as f:
        f.write(rx2_data)

    log("\n--- CONVERSION SUCCESSFUL ---")
    return ConversionResult(
        output_path=output_path,
        vertex_count=len(final_vertices),
        face_count=len(final_faces),
        skinned=is_skinned,
        bone_count=len(glb_bone_map) if glb_bone_map else 0,
        file_size=len(rx2_data),
    )
