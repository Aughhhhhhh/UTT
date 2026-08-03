================================================================================
 GLB / glTF -> Skate 3 Converters — Library Guide
 (psg_glb_converter.py / rx2_glb_converter.py / dummy.py / dummy2.py)
================================================================================

--------------------------------------------------------------------------------
1. WHAT THESE FILES DO
--------------------------------------------------------------------------------

The two libraries convert a glTF/GLB model into a game-ready Skate 3 mesh file:

  psg_glb_converter.py   ->  glTF/GLB  + donor .psg  ->  .psg   (PlayStation 3)
  rx2_glb_converter.py   ->  glTF/GLB  + donor .rx2  ->  .rx2   (Xbox 360)

They are GUI-free rewrites of the original converters (GLBGLTFtoPSG-v1.4.py and
GLBtoRX2-v1.0.py). The GUI parts (PyQt) were removed; the binary logic is the
same. Instead of pop-ups they raise exceptions and emit log text through a
callable you provide.

  dummy.py    - small tkinter front-end for the PSG library
  dummy2.py   - small tkinter front-end for the RX2 library

--------------------------------------------------------------------------------
2. REQUIREMENTS
--------------------------------------------------------------------------------

  Python 3.10+ (uses "str | None" type hints)
  pip install pygltflib numpy
  (the tkinter UIs use only the standard library)

--------------------------------------------------------------------------------
3. LIBRARY API (both libraries are identical in shape)
--------------------------------------------------------------------------------

  convert_glb_to_psg(glb_path, psg_template_path, output_path,
                     scale_xyz=256.0, bin_path=None, log=None) -> ConversionResult

  convert_glb_to_rx2(glb_path, rx2_template_path, output_path,
                     scale_xyz=256.0, bin_path=None, log=None) -> ConversionResult

  glb_path            : .glb (self-contained) or .gltf file.
  *_template_path     : a donor game file whose skeleton, palette, vertex
                        descriptor and container headers are reused.
  output_path         : where the converted mesh is written.
  scale_xyz           : multiplier applied to vertex XYZ when packing to
                        SHORT (default 256.0 — the game's fixed-point scale).
  bin_path            : optional .bin companion for .gltf input. If omitted
                        the library auto-detects it from the glTF buffer URI.
  log                 : optional callable log(str). Defaults to print(). Pass
                        any function if you want the text in a GUI widget.

  Raises:
    ValueError  - bad input paths, invalid template, unsupported model, palette
                  too large for the donor's OptiMesh block, etc.
    OSError     - file I/O failures.

  Return value: ConversionResult dataclass
    output_path : str   (where the file was written)
    vertex_count: int
    face_count  : int
    skinned     : bool  (whether skinning data was processed)
    bone_count  : int   (number of glTF bones used, 0 for static meshes)
    file_size   : int   (bytes written)

  Lower-level pieces are also importable if you need them:

    PsgTemplateParser / Rx2TemplateParser  - parse a donor file into a
        template object (offsets, vertex layout, bone names, bone palette).
        Exposes: .layout (VertexLayout), .bone_names, .bone_palette, and the
        discovered patch offsets used during assembly.
    parse_gltf_to_data(gltf_path, bin_path=None, log=None)
        - returns (vertices, uvs, normals, tangents, binormals, faces,
                   joints|None, weights|None, glb_bone_map|None).
    remap_skin_to_donor_palette(...) - per-platform skin remapping.
    make_vertex_bin_dynamic(...)     - pack vertices per the template layout.
    make_face_bin(faces)             - pack faces as u16 indices.
    detect_bin_path(gltf_path)       - resolve the .bin for a .gltf.
    normalize_bone_name(name)        - alphanumeric-only, lowercased.

--------------------------------------------------------------------------------
4. MINIMAL EXAMPLE
--------------------------------------------------------------------------------

    import psg_glb_converter          # or rx2_glb_converter

    result = psg_glb_converter.convert_glb_to_psg(
        "my_model.glb",
        "donor_from_game.psg",
        "out.psg",
        scale_xyz=256.0,
        log=print,
    )
    print(result.vertex_count, result.face_count, result.skinned)

--------------------------------------------------------------------------------
5. HOW THE CONVERSION WORKS (shared pipeline)
--------------------------------------------------------------------------------

The tool does not build a PSG/RX2 from scratch. It takes a real game file (the
"donor template") that already contains the skeleton, vertex layout, and
container structures, and rewrites the geometry inside it.

Step 1 - Parse the donor template
  * Reads the RenderWare-style dictionary from the header:
      num_entries @ 0x20, dict start @ 0x30, main resource base @ 0x44.
    Each entry (0x18 bytes) has a pointer, a size, and a type id; entries whose
    type is 0x00010030-0x0001003F are base resources located relative to the
    main base.
  * Locates the graphics blocks by type:
      Vertex Descriptor 0x000200E9
      Vertex Buffer     0x000200EA
      Index Buffer      0x000200EB
      OptiMesh          0x00EB0023
  * Finds the skeleton "Carrier" block by scanning entries for a valid header
    (1-512 bones, and in-bounds IBM + name-index tables) and reads every bone
    name. Bone names are used to match the GLB's bones to the game's skeleton.
  * Reads the bone palette (OptiMesh): the donor mesh only uses a subset of
    skeleton bones; the palette maps "palette slot" -> "global bone index".
  * Parses the vertex descriptor into a VertexLayout (stride + elements).
    The converter never hard-codes vertex formats — it reads them from the
    donor, so any donor file works.

Step 2 - Load the glTF/GLB
  * Reads POSITION, NORMAL, TEXCOORD_0, indices, and (if present)
    JOINTS_0/WEIGHTS_0 accessors straight from the binary blob.
  * Normalizes UBYTE/USHORT weights to [0,1] floats.
  * Computes per-vertex tangents from UV gradients, and binormals as the cross
    product of normal and tangent.
  * Keeps the model's indexed structure (no per-face vertex explosion), so
    vertex counts stay small enough for the game's mesh allocators.

Step 3 - Skin remapping (skinned models only)
  * Normalizes every bone name (strip non-alphanumerics, lowercase) for robust
    matching.
  * Maps each GLB bone to a donor skeleton bone, then to a donor palette slot.
  * For each vertex, aggregates the up-to-4 joint weights into the matched
    palette slots, keeps the top 4, and re-normalizes.
  * Bones that cannot be matched are ignored (with a warning). In the RX2
    library, weights to bones outside the donor palette are dropped — the
    Xbox side caches a fixed number of skinning matrices, so oversized
    palettes would reference matrices that are never uploaded.

Step 4 - Pack vertex data
  * For every vertex and every descriptor element, writes the matching byte
    pattern (SHORT positions scaled by scale_xyz, packed 11:11:10 normals,
    half-float or SHORT UVs, UBYTE weights/indices, etc.) at the element's
    byte offset inside a fixed-stride vertex.
  * Faces become big-endian u16 index triangles.

Step 5 - Assemble the output file
  * Loads the donor file, truncates it at the main resource base, and appends
    the new raw vertex + index buffers.
  * Patches every size/count/pointer field in place: dictionary base-resource
    pointers and sizes, buffer object sizes, index counts, OptiMesh face
    count, and the graphics block totals.

--------------------------------------------------------------------------------
6. PLATFORM DIFFERENCES
--------------------------------------------------------------------------------

PS3 (.psg)
  * 8-byte vertex descriptor elements, native PSG vertex type ids.
  * Vertex buffer object: base-resource index at +0, VB byte size at +8.
  * Index buffer object: base-resource index at +0, index count at +8.
  * OptiMesh bone palette lives at block +0x6C as u16 big-endian global bone
    indices terminated by 0xFFFF (or an out-of-range index).
  * XYZ packs as 3 shorts; binormal = cross(normal, tangent).

Xbox 360 (.rx2)
  * 16-byte vertex descriptor elements. The format field stores packed Xbox
    GPU VertexFormat constants (e.g. SHORT4N = 1712474), not simple D3D9
    values; a lookup table remaps them to the PSG-compatible internal type ids
    used by the shared packer. D3DDECLUSAGE semantics are remapped the same way.
  * XYZ packs as 4 shorts (x,y,z,w=0); blend weights/indices use reversed byte
    order vs PS3; binormal = cross(tangent, normal).
  * Vertex buffer object: FETCH_CONSTANT[1] size field at +0x1C and
    m_bufferSize at +0x20; index buffer object: size at +0x1C, count at +0x20.
  * OptiMesh layout: palette count at +0x48, num_indices at +0x6C, palette
    data at +0x70 (declared count, no terminator).
  * Palette size limits: 12 entries fit before the morph hash table at +0x88
    (soft limit). If the model needs more, the library overwrites the morph
    region and disables morph targets (morph count zeroed) up to the hard
    limit = (OptiMesh block size - 0x70) / 2 entries.
  * Extra vertex buffers (shadow/blend-shape passes) are preserved as
    zero-filled blocks with the correct stride so the engine does not read
    past EOF.

--------------------------------------------------------------------------------
7. THE TKINTER UIs (dummy.py / dummy2.py)
--------------------------------------------------------------------------------

  python dummy.py     # PS3
  python dummy2.py    # Xbox 360

Both offer:
  * Browse buttons for the GLB/glTF, the donor template, and the output path.
  * A Vertex Scale field (default 256.0).
  * A live log window (the library's log callback is routed there via a queue
    so the UI never blocks).
  * Conversion runs in a background thread with an indeterminate progress bar.
  * When a .gltf is chosen, a .bin row appears and is auto-filled from the
    buffer URI.

--------------------------------------------------------------------------------
8. TROUBLESHOOTING / NOTES
--------------------------------------------------------------------------------

  * "Could not find a Vertex Descriptor / Vertex Buffer / Index Buffer"
    - The file is not a valid PSG/RX2 container, or the dictionary is corrupt.
  * "No skeleton or palette was loaded from the donor"
    - The donor has no recognizable Carrier block; skinned models cannot be
      converted with it. Static meshes still work.
  * "Model uses N bones but donor OptiMesh block has room for at most M"
    - RX2 only. Use a donor with a larger OptiMesh block or fewer weighted
      bones.
  * Only the first mesh / first primitive of a glTF is used; multiple-part
    models print a warning and convert the first part.
  * Vertices with UV or normal data outside [-1,1] get clamped during packing.
  * The template files are read-only — nothing is ever written back into them.

--------------------------------------------------------------------------------
9. CREDITS
--------------------------------------------------------------------------------

Original converters by SunJay, Dumbad, RenderWareGavin and Tuukkas (with
Wissp). These libraries are ports of that work with the GUI removed.
