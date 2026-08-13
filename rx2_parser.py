"""Standalone parser for EA Skate 1/2/3 RenderWare4 .rx2 files (Xbox 360).

Pure-Python reimplementation of the Xbox 360 RX2 reading logic from the
Noesis plugin mdl_EA_SKATE_4_7_0.py (Beedy / GHFear / tuukkas). It does not
require Noesis or the plugin.

Scope: the .rx2 container (Xbox 360). The PS3 .psg container is not covered.

What it can do:
  * read the RX2 header (file count, file table, data base, container type)
  * walk the 24-byte file table (TOC) records
  * find and decode texture entries (DXT1 / DXT3 / DXT5 / ATI2 /
    A8R8G8B8 / B5G6R5 / A8) into plain RGBA8888 byte buffers, including the
    Xbox 360 GPU tiled-memory layout (XGAddress2DTiled swizzle)

Performance: when numpy is installed, the untiling and DXT/raw decoders run
on vectorized numpy paths (same math, byte-identical output); otherwise the
pure-Python fallback is used. All decoders only touch the base mip level,
so buffers that include a full mip chain are sliced first.

Typical use:

    import rx2_parser
    rx2 = rx2_parser.parse_rx2("some_file.rx2")
    for tex in rx2.textures:
        print(tex)          # e.g. "tex0 | 512x512 | DXT1"
        tex.save_png("tex%d.png" % tex.index)

Run `python rx2_parser.py file.rx2 --toc --out-dir out` for a CLI dump.
See rx2parserexample.txt for the file format notes.
"""

import struct
from pathlib import Path

try:
    import numpy as _np
except ImportError:
    _np = None

MAGIC_X360 = b"\x89RW4xb2"

TYPE_SIM = 0x00000001
TYPE_MESH = 0x00000004
TYPE_TEXTURE = 0x00001000
TYPE_NHL_LEGACY_MESH = 0x00000800

TYPE_NAMES = {
    TYPE_SIM: "Sim / collision",
    TYPE_MESH: "3D model",
    TYPE_TEXTURE: "Texture",
    TYPE_NHL_LEGACY_MESH: "3D model (NHL legacy)",
}

TOC_TEXTURE_SKATE = 0x000200E8
TOC_TEXTURE_NHL = 0x00020003
TOC_MESH_INFO = 0x000200E9
TOC_VERTICES = 0x000200EA
TOC_FACES = 0x000200EB
TOC_MATERIALS = 0x00EB0005
TOC_BONES = 0x00EB0001
TOC_SIM = 0x00EB000B

TOC_TYPE_NAMES = {
    TOC_TEXTURE_SKATE: "texture (Skate)",
    TOC_TEXTURE_NHL: "texture (NHL)",
    TOC_MESH_INFO: "mesh info",
    TOC_VERTICES: "vertices",
    TOC_FACES: "faces",
    TOC_MATERIALS: "materials",
    TOC_BONES: "bones",
    TOC_SIM: "sim",
}

FMT_DXT1 = 0x52
FMT_DXT3 = 0x53
FMT_DXT5 = 0x54
FMT_ATI2 = 0x71
FMT_DXT1_NORMAL = 0x7C
FMT_A8R8G8B8 = 0x86
FMT_B5G6R5 = 0x44
FMT_A8 = 0x02

FMT_NAMES = {
    FMT_DXT1: "DXT1",
    FMT_DXT3: "DXT3",
    FMT_DXT5: "DXT5",
    FMT_ATI2: "ATI2",
    FMT_DXT1_NORMAL: "DXT1 normal",
    FMT_A8R8G8B8: "A8R8G8B8",
    FMT_B5G6R5: "B5G6R5",
    FMT_A8: "A8",
}


class RX2ParseError(Exception):
    pass


class TOCEntry(object):
    """One 24-byte record from the RX2 file table.

    Records come in pairs: the record directly before a type record carries
    the payload pointer/size for that type. Concretely, when record N's type
    matches:
        data offset (relative to the header-size value at 0x44) = record N-1 f0
        data size                                           = record N-1 f2
        info block offset (absolute)                        = record N   f0
    """

    __slots__ = ("index", "f0", "f1", "f2", "f3", "f4", "type_id",
                 "data_offset", "buffer_size", "info_offset", "is_texture")

    def __init__(self, index, f0, f1, f2, f3, f4, type_id):
        self.index = index
        self.f0 = f0
        self.f1 = f1
        self.f2 = f2
        self.f3 = f3
        self.f4 = f4
        self.type_id = type_id
        self.data_offset = None
        self.buffer_size = None
        self.info_offset = f0
        self.is_texture = type_id in (TOC_TEXTURE_SKATE, TOC_TEXTURE_NHL)

    @property
    def type_name(self):
        return TOC_TYPE_NAMES.get(self.type_id, "unknown")

    def __repr__(self):
        return ("TOCEntry(%d) type=0x%08X (%s) data_off=%s size=%s info_off=%s"
                % (self.index, self.type_id, self.type_name,
                   "0x%X" % self.data_offset if self.data_offset is not None else "-",
                   self.buffer_size if self.buffer_size is not None else "-",
                   "0x%X" % self.info_offset if self.info_offset is not None else "-"))


class Texture(object):
    """A decoded texture: width x height RGBA8888 pixels in `rgba`."""

    __slots__ = ("index", "width", "height", "fmt_id", "rgba",
                 "data_offset", "buffer_size")

    def __init__(self, index, width, height, fmt_id, rgba, data_offset, buffer_size):
        self.index = index
        self.width = width
        self.height = height
        self.fmt_id = fmt_id
        self.rgba = rgba
        self.data_offset = data_offset
        self.buffer_size = buffer_size

    @property
    def name(self):
        return "tex%d" % self.index

    @property
    def fmt_name(self):
        return FMT_NAMES.get(self.fmt_id, "0x%02X" % self.fmt_id)

    @property
    def size(self):
        return (self.width, self.height)

    def to_pil(self):
        """Return a Pillow RGBA image (requires Pillow)."""
        from PIL import Image
        return Image.frombytes("RGBA", self.size, self.rgba)

    def save_png(self, path):
        """Save the texture as a PNG (requires Pillow)."""
        self.to_pil().save(path, "PNG")

    def __str__(self):
        return "%s | %dx%d | %s" % (self.name, self.width, self.height, self.fmt_name)


class RX2File(object):
    """A parsed RX2 container: header fields, TOC entries, decoded textures."""

    def __init__(self, data, path=None):
        if isinstance(data, (bytes, bytearray)):
            self.data = bytes(data)
        else:
            self.data = Path(data).read_bytes()
            path = str(data)
        self.path = path
        self.file_count = 0
        self.file_table_offset = 0
        self.data_base = 0
        self.type_id = 0
        self.entries = []
        self.textures = []
        self.warnings = []

    @property
    def magic(self):
        return self.data[0:7]

    @property
    def type_name(self):
        return TYPE_NAMES.get(self.type_id, "unknown (0x%08X)" % self.type_id)

    def parse(self):
        """Read the header and the file table. Idempotent."""
        data = self.data
        if len(data) < 0x5C:
            raise RX2ParseError("file is too small to be an RX2")
        if data[0:7] != MAGIC_X360:
            raise RX2ParseError("not an Xbox 360 RX2 (bad magic 0x%s)" % data[0:7].hex())

        self.file_count = int.from_bytes(data[0x20:0x24], "big")
        self.file_table_offset = int.from_bytes(data[0x30:0x34], "big")
        self.data_base = int.from_bytes(data[0x44:0x48], "big")
        self.type_id = int.from_bytes(data[0x58:0x5C], "big")

        if self.file_count > (1 << 20):
            raise RX2ParseError("implausible file count: %d" % self.file_count)
        if self.file_table_offset + self.file_count * 24 > len(data):
            raise RX2ParseError("file table runs past the end of the file")

        self.entries = []
        prev_f0 = None
        prev_f2 = None
        for i in range(self.file_count):
            p = self.file_table_offset + i * 24
            f0, f1, f2, f3, f4 = struct.unpack_from(">iiiii", data, p)
            type_id = int.from_bytes(data[p + 20:p + 24], "big")
            entry = TOCEntry(i, f0, f1, f2, f3, f4, type_id)
            if i > 0:
                entry.data_offset = prev_f0
                entry.buffer_size = prev_f2
            self.entries.append(entry)
            prev_f0, prev_f2 = f0, f2
        return self

    def decode_textures(self):
        """Decode every texture entry found in the TOC. Idempotent."""
        self.textures = []
        for entry in self.entries:
            if entry.is_texture:
                tex = self._decode_texture(entry)
                if tex is not None:
                    self.textures.append(tex)
        return self.textures

    def _decode_texture(self, entry):
        data = self.data
        hdr_off = entry.info_offset
        if hdr_off is None or hdr_off + 40 > len(data):
            self.warnings.append("texture entry %d: bad info offset" % entry.index)
            return None
        hdr = data[hdr_off:hdr_off + 40]
        fmt = hdr[35]
        width = ((hdr[38] << 8) | hdr[39])
        width = (width + 1) & 0x1FFF
        height = (hdr[37] + 1) * 8
        dxt5_variant = hdr[28]
        if width <= 0 or height <= 0:
            self.warnings.append("texture entry %d: bad dimensions %dx%d"
                                 % (entry.index, width, height))
            return None
        base = self.data_base + entry.data_offset
        size = entry.buffer_size or 0
        raw = data[base:base + size]
        try:
            rgba = _decode_texture_data(raw, width, height, fmt, dxt5_variant)
        except Exception as exc:
            self.warnings.append("texture entry %d: decode failed: %s" % (entry.index, exc))
            return None
        return Texture(entry.index, width, height, fmt, rgba, base, size)

    def __str__(self):
        return ("RX2File(%s)\n"
                "  type: %s\n"
                "  file count: %d\n"
                "  file table: 0x%X\n"
                "  data base: 0x%X\n"
                "  TOC entries: %d\n"
                "  textures: %d"
                % (self.path, self.type_name, self.file_count,
                   self.file_table_offset, self.data_base,
                   len(self.entries), len(self.textures)))


def parse_rx2(source):
    """Parse an RX2 (path or raw bytes) and decode every texture in it."""
    rx2 = RX2File(source)
    rx2.parse()
    rx2.decode_textures()
    return rx2


# ---------------------------------------------------------------------------
# Byte order / tiling helpers (mirror the Noesis plugin's behaviour)
# ---------------------------------------------------------------------------

def _swap16(data):
    """Byte-swap every 16-bit word (X360 DXT/RGB565 storage order)."""
    n = len(data) - (len(data) % 2)
    b = bytearray(data)
    b[0:n:2], b[1:n:2] = b[1:n:2], b[0:n:2]
    return bytes(b)


def _base_level_size_dxt(width, height, block_bytes):
    """Bytes used by just the top mip level of a DXT surface. Buffers read
    from the RX2 file table include the FULL mip chain, so only this many
    bytes must be sliced off before untiling."""
    return ((width + 3) >> 2) * ((height + 3) >> 2) * block_bytes


def _base_level_size_raw(width, height, bpp):
    return width * height * bpp


def _x360_tiled_x(offset, width_units, texel_pitch):
    """XGAddress2DTiledX: swizzled column of a tiled 2D surface (X360 GPU)."""
    aligned_width = (width_units + 31) & ~31
    log_bpp = (texel_pitch >> 2) + ((texel_pitch >> 1) >> (texel_pitch >> 2))
    off_b = offset << log_bpp
    off_t = ((off_b & ~4095) >> 3) + ((off_b & 1792) >> 2) + (off_b & 63)
    off_m = off_t >> (7 + log_bpp)
    macro_x = (off_m % (aligned_width >> 5)) << 2
    tile = (((off_t >> (5 + log_bpp)) & 2) + (off_b >> 6)) & 3
    macro = (macro_x + tile) << 3
    micro = ((((off_t >> 1) & ~15) + (off_t & 15)) & ((texel_pitch << 3) - 1)) >> log_bpp
    return macro + micro


def _x360_tiled_y(offset, width_units, texel_pitch):
    """XGAddress2DTiledY: swizzled row of a tiled 2D surface (X360 GPU)."""
    aligned_width = (width_units + 31) & ~31
    log_bpp = (texel_pitch >> 2) + ((texel_pitch >> 1) >> (texel_pitch >> 2))
    off_b = offset << log_bpp
    off_t = ((off_b & ~4095) >> 3) + ((off_b & 1792) >> 2) + (off_b & 63)
    off_m = off_t >> (7 + log_bpp)
    macro_y = (off_m // (aligned_width >> 5)) << 2
    tile = ((off_t >> (6 + log_bpp)) & 1) + ((off_b & 2048) >> 10)
    macro = (macro_y + tile) << 3
    micro = ((((off_t & (((texel_pitch << 6) - 1) & ~31)) + ((off_t & 15) << 1)) >> (3 + log_bpp)) & ~1)
    return macro + micro + ((off_t & 16) >> 4)


def _py_untile360(src, width_units, texel_pitch):
    """Reverse the X360 GPU tiled-memory layout (XGAddress2DTiled swizzle).
    Pure-Python fallback used when numpy is unavailable.

    ``width_units`` is the surface width in units (4x4-pixel blocks for DXT
    formats, texels for raw formats) and ``texel_pitch`` is the size of one
    unit in bytes (8 or 16 for DXT, bytes-per-pixel for raw).
    """
    dst = bytearray(len(src))
    units = len(src) // texel_pitch
    for j in range(units // width_units):
        for i in range(width_units):
            x = _x360_tiled_x(j * width_units + i, width_units, texel_pitch)
            y = _x360_tiled_y(j * width_units + i, width_units, texel_pitch)
            dst_idx = (y * width_units + x) * texel_pitch
            src_idx = (j * width_units + i) * texel_pitch
            if dst_idx + texel_pitch <= len(dst) and src_idx + texel_pitch <= len(src):
                dst[dst_idx:dst_idx + texel_pitch] = src[src_idx:src_idx + texel_pitch]
    return bytes(dst)


def _np_untile360(src, width_units, texel_pitch):
    """Vectorized ``_py_untile360``. Computes the tiled x/y for every unit
    at once and rearranges the unit rows in one fancy-indexed pass."""
    if not src:
        return bytes()
    units = len(src) // texel_pitch
    used = (units // width_units) * width_units
    if used == 0:
        return bytes(len(src))
    k = _np.arange(used, dtype=_np.int64)
    aligned_width = (width_units + 31) & ~31
    log_bpp = (texel_pitch >> 2) + ((texel_pitch >> 1) >> (texel_pitch >> 2))
    off_b = k << log_bpp
    off_t = ((off_b & ~4095) >> 3) + ((off_b & 1792) >> 2) + (off_b & 63)
    off_m = off_t >> (7 + log_bpp)
    aw5 = aligned_width >> 5
    tile = (((off_t >> (5 + log_bpp)) & 2) + (off_b >> 6)) & 3
    x = (((off_m % aw5) << 2) + tile) << 3
    micro = ((((off_t >> 1) & ~15) + (off_t & 15)) & ((texel_pitch << 3) - 1)) >> log_bpp
    x = x + micro
    macro_y = (off_m // aw5) << 2
    tile_y = ((off_t >> (6 + log_bpp)) & 1) + ((off_b & 2048) >> 10)
    y = (macro_y + tile_y) << 3
    micro_y = ((((off_t & (((texel_pitch << 6) - 1) & ~31)) + ((off_t & 15) << 1)) >> (3 + log_bpp)) & ~1)
    y = y + micro_y + ((off_t & 16) >> 4)
    dst_pos = y * width_units + x
    src_arr = _np.frombuffer(src, _np.uint8, count=used * texel_pitch)
    dst = _np.zeros(len(src), _np.uint8)
    dst_units = dst[:used * texel_pitch].reshape(used, texel_pitch)
    valid = dst_pos < used
    dst_units[dst_pos[valid]] = src_arr.reshape(used, texel_pitch)[valid]
    return dst.tobytes()


def _untile360(src, width_units, texel_pitch):
    """Reverse the X360 GPU tiled-memory layout (XGAddress2DTiled swizzle).

    ``width_units`` is the surface width in units (4x4-pixel blocks for DXT
    formats, texels for raw formats) and ``texel_pitch`` is the size of one
    unit in bytes (8 or 16 for DXT, bytes-per-pixel for raw).
    """
    if _np is not None:
        return _np_untile360(src, width_units, texel_pitch)
    return _py_untile360(src, width_units, texel_pitch)


def _untile360_dxt(src, tex_w, tex_h, blk_size):
    """Reverse the X360 GPU tiling for DXT-style formats (units = 4x4 blocks)."""
    return _untile360(src, (tex_w + 3) >> 2, blk_size)


def _untile360_raw(src, tex_w, tex_h, bpp):
    """Reverse the X360 GPU tiling for raw pixel formats (units = texels)."""
    return _untile360(src, tex_w, bpp)


# ---------------------------------------------------------------------------
# DXT / raw decoders (all produce RGBA8888 row-major bytes)
#
# Two implementations with identical output:
#   *_py_*  pure-Python (original per-block loops, used without numpy)
#   *_np_*  vectorized numpy (all block math array-wide, exact same formulas)
# ---------------------------------------------------------------------------

def _rgb565(value):
    """Expand a 16-bit 565 value to 888 (v*255/denom, truncated).

    Verified against Noesis's imageDecodeDXT output: truncation, not
    D3DX-style rounding (rounding adds a constant +1 offset to ~1 in 10
    pixels vs the game's decoder)."""
    return ((((value >> 11) & 0x1F) * 255) // 31,
            (((value >> 5) & 0x3F) * 255) // 63,
            (((value & 0x1F) * 255) // 31))


def _rgb565_interp(a5, b5, a6, b6, a1, b1):
    """The two interpolated palette colours, computed from the 888 values:
    (2a+b+1)/3 resp. (a+2b+1)/3, integer rounding (matches Noesis)."""
    r0 = (a5 * 255) // 31
    r1 = (b5 * 255) // 31
    g0 = (a6 * 255) // 63
    g1 = (b6 * 255) // 63
    b0 = (a1 * 255) // 31
    b1v = (b1 * 255) // 31
    return ((2 * r0 + r1 + 1) // 3, (2 * g0 + g1 + 1) // 3, (2 * b0 + b1v + 1) // 3), \
           ((r0 + 2 * r1 + 1) // 3, (g0 + 2 * g1 + 1) // 3, (b0 + 2 * b1v + 1) // 3)


def _dxt1_block(blk, transparent):
    c0, c1 = struct.unpack_from(">HH", blk, 0)
    r0, g0, b0 = _rgb565(c0)
    r1, g1, b1 = _rgb565(c1)
    if c0 > c1:
        c2, c3 = _rgb565_interp((c0 >> 11) & 0x1F, (c1 >> 11) & 0x1F,
                                (c0 >> 5) & 0x3F, (c1 >> 5) & 0x3F,
                                c0 & 0x1F, c1 & 0x1F)
        palette = [(r0, g0, b0), (r1, g1, b1), c2, c3]
        alpha3 = 255
    else:
        palette = [(r0, g0, b0), (r1, g1, b1),
                   ((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2),
                   (0, 0, 0)]
        alpha3 = 0 if transparent else 255
    out = bytearray(64)
    for y in range(4):
        idx_byte = blk[4 + (y ^ 1)]
        for x in range(4):
            i = (idx_byte >> (x * 2)) & 3
            o = (y * 4 + x) * 4
            r, g, b = palette[i]
            out[o] = r
            out[o + 1] = g
            out[o + 2] = b
            out[o + 3] = alpha3 if i == 3 else 255
    return bytes(out)


def _dxt3_block(blk):
    color = _dxt1_block(blk[8:16], transparent=False)
    out = bytearray(64)
    for p in range(16):
        # alpha nibbles are word-swapped like the DXT5 alpha bits
        a = (blk[(p // 2) ^ 1] >> (4 * (p & 1))) & 0xF
        o = p * 4
        out[o] = color[o]
        out[o + 1] = color[o + 1]
        out[o + 2] = color[o + 2]
        out[o + 3] = a * 17
    return bytes(out)


def _alpha_table(a0, a1):
    if a0 > a1:
        return [a0, a1] + [((6 - i) * a0 + (i + 1) * a1) // 7 for i in range(6)]
    return [a0, a1,
            (4 * a0 + a1) // 5, (3 * a0 + 2 * a1) // 5,
            (2 * a0 + 3 * a1) // 5, (a0 + 4 * a1) // 5, 0, 255]


def _dxt5_block(blk):
    # the alpha endpoint bytes and the 48 index bits are 16-bit-word-swapped
    # (same swapEndianArray(data, 2) the Noesis plugin applies before decode)
    table = _alpha_table(blk[1], blk[0])
    bits = int.from_bytes(_swap16(blk[2:8]), "little")
    color = _dxt1_block(blk[8:16], transparent=False)
    out = bytearray(64)
    for p in range(16):
        o = p * 4
        out[o] = color[o]
        out[o + 1] = color[o + 1]
        out[o + 2] = color[o + 2]
        out[o + 3] = table[(bits >> (3 * p)) & 7]
    return bytes(out)


def _ati2_channel(blk, off):
    table = _alpha_table(blk[off + 1], blk[off])
    bits = int.from_bytes(_swap16(blk[off + 2:off + 8]), "little")
    return [table[(bits >> (3 * p)) & 7] for p in range(16)]


def _ati2_block(blk):
    r = _ati2_channel(blk, 0)
    g = _ati2_channel(blk, 8)
    out = bytearray(64)
    for p in range(16):
        o = p * 4
        out[o] = r[p]
        out[o + 1] = g[p]
        out[o + 2] = 0
        out[o + 3] = 255
    return bytes(out)


def _decode_blocks(data, width, height, block_size, block_fn):
    bw = (width + 3) >> 2
    bh = (height + 3) >> 2
    out = bytearray(width * height * 4)
    for by in range(bh):
        for bx in range(bw):
            off = (by * bw + bx) * block_size
            blk = data[off:off + block_size]
            if len(blk) < block_size:
                continue
            px = block_fn(blk)
            x0 = bx * 4
            y0 = by * 4
            for yy in range(4):
                row = y0 + yy
                if row >= height:
                    break
                base = (row * width + x0) * 4
                if x0 + 4 <= width:
                    out[base:base + 16] = px[yy * 16:(yy + 1) * 16]
                else:
                    for xx in range(width - x0):
                        s = (yy * 4 + xx) * 4
                        d = base + xx * 4
                        out[d:d + 4] = px[s:s + 4]
    return bytes(out)


# ------------------------- numpy decoders -------------------------

def _np_dxt1_color(w4, transparent):
    """DXT1 colour part from a (n,4) uint16 view of LE blocks (c0, c1, 4 index
    bytes). Returns (rgb (n,4,4,3), alpha (n,4,4))."""
    n = w4.shape[0]
    c0 = w4[:, 0].astype(_np.int32)
    c1 = w4[:, 1].astype(_np.int32)
    r0 = ((c0 >> 11) & 0x1F) * 255 // 31
    g0 = ((c0 >> 5) & 0x3F) * 255 // 63
    b0 = (c0 & 0x1F) * 255 // 31
    r1 = ((c1 >> 11) & 0x1F) * 255 // 31
    g1 = ((c1 >> 5) & 0x3F) * 255 // 63
    b1 = (c1 & 0x1F) * 255 // 31
    gt = c0 > c1
    pal = _np.stack(
        [r0, g0, b0, r1, g1, b1,
         _np.where(gt, (2 * r0 + r1 + 1) // 3, (r0 + r1) // 2),
         _np.where(gt, (2 * g0 + g1 + 1) // 3, (g0 + g1) // 2),
         _np.where(gt, (2 * b0 + b1 + 1) // 3, (b0 + b1) // 2),
         _np.where(gt, (r0 + 2 * r1 + 1) // 3, 0),
         _np.where(gt, (g0 + 2 * g1 + 1) // 3, 0),
         _np.where(gt, (b0 + 2 * b1 + 1) // 3, 0)],
        axis=1).reshape(n, 4, 3)
    idx8 = w4[:, 2:4].view(_np.uint8).reshape(n, 4)
    pix = (idx8[:, :, None] >> (2 * _np.arange(4, dtype=_np.int32))) & 3
    rgb = pal[_np.arange(n)[:, None, None], pix]
    alpha = _np.full((n, 4, 4), 255, _np.uint8)
    if transparent:
        alpha = _np.where((pix == 3) & ~gt[:, None, None], 0, alpha)
    return rgb, alpha


def _np_alpha_channel(b8):
    """One BC5/ATI2-style 8-byte channel (alpha0, alpha1, 6 index bytes in LE
    order). Returns the 16 alpha values per block (n,16)."""
    n = b8.shape[0]
    a0 = b8[:, 0].astype(_np.int32)
    a1 = b8[:, 1].astype(_np.int32)
    gt = a0 > a1
    t = _np.empty((n, 8), _np.int32)
    t[:, 0] = a0
    t[:, 1] = a1
    ii = _np.arange(6, dtype=_np.int32)
    t_gt = ((6 - ii) * a0[:, None] + (ii + 1) * a1[:, None]) // 7
    t_le = _np.stack(
        [(4 * a0 + a1) // 5, (3 * a0 + 2 * a1) // 5,
         (2 * a0 + 3 * a1) // 5, (a0 + 4 * a1) // 5,
         _np.zeros(n, _np.int32), _np.full(n, 255, _np.int32)], axis=1)
    t[:, 2:8] = _np.where(gt[:, None], t_gt, t_le)
    b6 = b8[:, 2:8].astype(_np.int64)
    bits = (b6 << _np.array([0, 8, 16, 24, 32, 40], _np.int64)).sum(axis=1)
    ai = (bits[:, None] >> (3 * _np.arange(16, dtype=_np.int64))) & 7
    return t[_np.arange(n)[:, None], ai]


def _np_assemble(rgba4, width, height, bw, bh, n):
    """Flatten (n,4,4,4) block-major RGBA into width*height*4 row-major bytes."""
    rgba4 = rgba4.astype(_np.uint8)
    if n < bw * bh:
        pad = _np.zeros((bw * bh - n, 4, 4, 4), _np.uint8)
        rgba4 = _np.concatenate([rgba4, pad])
    full = rgba4.reshape(bh, bw, 4, 4, 4)
    img = full.transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
    return img[:height, :width].tobytes()


def _np_decode_dxt1(blocks, width, height):
    bw = (width + 3) >> 2
    bh = (height + 3) >> 2
    n = min(bw * bh, len(blocks) // 8)
    if n <= 0:
        return bytes(width * height * 4)
    buf = _np.frombuffer(blocks, _np.uint8, count=n * 8)
    w4 = buf.view(_np.uint16).reshape(n, 4)
    rgb, alpha = _np_dxt1_color(w4, transparent=True)
    rgba = _np.concatenate([rgb, alpha[:, :, :, None]], axis=-1)
    return _np_assemble(rgba, width, height, bw, bh, n)


def _np_decode_dxt3(blocks, width, height):
    bw = (width + 3) >> 2
    bh = (height + 3) >> 2
    n = min(bw * bh, len(blocks) // 16)
    if n <= 0:
        return bytes(width * height * 4)
    buf = _np.frombuffer(blocks, _np.uint8, count=n * 16)
    a8 = buf.reshape(n, 16)[:, 0:8]
    lo = (a8 & 0xF).astype(_np.int32)
    hi = ((a8 >> 4) & 0xF).astype(_np.int32)
    alpha = (_np.stack([lo, hi], axis=-1).reshape(n, 16)).astype(_np.uint8) * 17
    w16 = buf.view(_np.uint16).reshape(n, 8)
    rgb, _ = _np_dxt1_color(w16[:, 4:8], transparent=False)
    rgba = _np.concatenate([rgb, alpha.reshape(n, 4, 4, 1)], axis=-1)
    return _np_assemble(rgba, width, height, bw, bh, n)


def _np_decode_dxt5(blocks, width, height):
    bw = (width + 3) >> 2
    bh = (height + 3) >> 2
    n = min(bw * bh, len(blocks) // 16)
    if n <= 0:
        return bytes(width * height * 4)
    buf = _np.frombuffer(blocks, _np.uint8, count=n * 16)
    b = buf.reshape(n, 16)
    alpha = _np_alpha_channel(b[:, 0:8]).reshape(n, 4, 4, 1)
    w16 = buf.view(_np.uint16).reshape(n, 8)
    rgb, _ = _np_dxt1_color(w16[:, 4:8], transparent=False)
    rgba = _np.concatenate([rgb, alpha], axis=-1)
    return _np_assemble(rgba, width, height, bw, bh, n)


def _np_decode_ati2(blocks, width, height):
    bw = (width + 3) >> 2
    bh = (height + 3) >> 2
    n = min(bw * bh, len(blocks) // 16)
    if n <= 0:
        return bytes(width * height * 4)
    buf = _np.frombuffer(blocks, _np.uint8, count=n * 16)
    b = buf.reshape(n, 16)
    r = _np_alpha_channel(b[:, 0:8])
    g = _np_alpha_channel(b[:, 8:16])
    rgba = _np.stack(
        [r, g, _np.zeros_like(r), _np.full_like(r, 255)], axis=-1).reshape(n, 4, 4, 4)
    return _np_assemble(rgba, width, height, bw, bh, n)


def _np_normal_z(rgba):
    arr = _np.frombuffer(rgba, _np.uint8).reshape(-1, 4).copy()
    r = arr[:, 0].astype(_np.float64) / 255.0
    g = arr[:, 1].astype(_np.float64) / 255.0
    z2 = 1.0 - r * r - g * g
    z = _np.round(_np.sqrt(_np.where(z2 > 0.0, z2, 0.0)) * 255.0)
    arr[:, 2] = z.astype(_np.uint8)
    arr[:, 3] = 255
    return arr.tobytes()


def _py_normal_z(rgba):
    out = bytearray(rgba)
    for i in range(0, len(out), 4):
        r = out[i] / 255.0
        g = out[i + 1] / 255.0
        z2 = 1.0 - r * r - g * g
        out[i + 2] = int(round((z2 ** 0.5 if z2 > 0 else 0.0) * 255))
        out[i + 3] = 255
    return bytes(out)


def _np_decode_raw_a8r8g8b8(data, width, height):
    n = min(len(data) // 4, width * height)
    out = _np.zeros((width * height, 4), _np.uint8)
    if n > 0:
        arr = _np.frombuffer(data, _np.uint8, count=n * 4).reshape(n, 4)
        out[:n] = arr[:, [1, 2, 3, 0]]
    return out.tobytes()


def _np_decode_raw_b5g6r5(data, width, height):
    n = min(len(data) // 2, width * height)
    out = _np.zeros((width * height, 4), _np.uint8)
    if n > 0:
        v = _np.frombuffer(data, ">u2", count=n).astype(_np.int32)
        out[:n, 0] = ((v >> 11) & 0x1F) * 255 // 31
        out[:n, 1] = ((v >> 5) & 0x3F) * 255 // 63
        out[:n, 2] = (v & 0x1F) * 255 // 31
        out[:n, 3] = 255
    return out.tobytes()


def _np_decode_raw_a8(data, width, height):
    n = min(len(data), width * height)
    out = _np.zeros((width * height, 4), _np.uint8)
    if n > 0:
        arr = _np.frombuffer(data, _np.uint8, count=n)
        out[:n, 0] = arr
        out[:n, 1] = arr
        out[:n, 2] = arr
        out[:n, 3] = arr
    return out.tobytes()


# ------------------------- public decoders -------------------------

def decode_dxt1(data, width, height):
    if _np is not None:
        base = _base_level_size_dxt(width, height, 8)
        blocks = _untile360_dxt(_swap16(data[:base]), width, height, 8)
        return _np_decode_dxt1(blocks, width, height)
    base = _base_level_size_dxt(width, height, 8)
    return _decode_blocks(_untile360_dxt(data[:base], width, height, 8),
                          width, height, 8, lambda b: _dxt1_block(b, True))


def decode_dxt1_normal(data, width, height):
    rgba = decode_dxt1(data, width, height)
    if _np is not None:
        return _np_normal_z(rgba)
    return _py_normal_z(rgba)


def decode_dxt3(data, width, height):
    if _np is not None:
        base = _base_level_size_dxt(width, height, 16)
        blocks = _untile360_dxt(_swap16(data[:base]), width, height, 16)
        return _np_decode_dxt3(blocks, width, height)
    base = _base_level_size_dxt(width, height, 16)
    return _decode_blocks(_untile360_dxt(data[:base], width, height, 16),
                          width, height, 16, _dxt3_block)


def decode_dxt5(data, width, height, tiled=True):
    if _np is not None:
        base = _base_level_size_dxt(width, height, 16)
        blocks = _swap16(data[:base])
        if tiled:
            blocks = _untile360_dxt(blocks, width, height, 16)
        return _np_decode_dxt5(blocks, width, height)
    base = _base_level_size_dxt(width, height, 16)
    blocks = _untile360_dxt(data[:base], width, height, 16) if tiled else data[:base]
    return _decode_blocks(blocks, width, height, 16, _dxt5_block)


def decode_ati2(data, width, height):
    if _np is not None:
        base = _base_level_size_dxt(width, height, 16)
        blocks = _untile360_dxt(_swap16(data[:base]), width, height, 16)
        return _np_decode_ati2(blocks, width, height)
    base = _base_level_size_dxt(width, height, 16)
    return _decode_blocks(_untile360_dxt(data[:base], width, height, 16),
                          width, height, 16, _ati2_block)


def _decode_raw_a8r8g8b8(data, width, height):
    if _np is not None:
        return _np_decode_raw_a8r8g8b8(data, width, height)
    out = bytearray(width * height * 4)
    n = min(len(data), width * height * 4) // 4 * 4
    for i in range(0, n, 4):
        o = i
        out[o] = data[i + 1]
        out[o + 1] = data[i + 2]
        out[o + 2] = data[i + 3]
        out[o + 3] = data[i]
    return bytes(out)


def _decode_raw_b5g6r5(data, width, height):
    if _np is not None:
        return _np_decode_raw_b5g6r5(data, width, height)
    out = bytearray(width * height * 4)
    n = min(len(data), width * height * 2) // 2 * 2
    for i in range(0, n, 2):
        v = (data[i] << 8) | data[i + 1]
        o = (i // 2) * 4
        out[o] = ((v >> 11) & 0x1F) * 255 // 31
        out[o + 1] = ((v >> 5) & 0x3F) * 255 // 63
        out[o + 2] = (v & 0x1F) * 255 // 31
        out[o + 3] = 255
    return bytes(out)


def _decode_raw_a8(data, width, height):
    if _np is not None:
        return _np_decode_raw_a8(data, width, height)
    out = bytearray(width * height * 4)
    n = min(len(data), width * height)
    for i in range(n):
        o = i * 4
        v = data[i]
        out[o] = v
        out[o + 1] = v
        out[o + 2] = v
        out[o + 3] = v
    return bytes(out)


def _decode_texture_data(raw, width, height, fmt, dxt5_variant):
    if fmt == FMT_DXT1:
        return decode_dxt1(raw, width, height)
    if fmt == FMT_DXT3:
        return decode_dxt3(raw, width, height)
    if fmt == FMT_DXT5:
        return decode_dxt5(raw, width, height, tiled=dxt5_variant not in (1, 2, 84))
    if fmt == FMT_ATI2:
        return decode_ati2(raw, width, height)
    if fmt == FMT_DXT1_NORMAL:
        return decode_dxt1_normal(raw, width, height)
    if fmt == FMT_A8R8G8B8:
        return _decode_raw_a8r8g8b8(_untile360_raw(raw, width, height, 4), width, height)
    if fmt == FMT_B5G6R5:
        return _decode_raw_b5g6r5(_untile360_raw(_swap16(raw), width, height, 2),
                                  width, height)
    if fmt == FMT_A8:
        return _decode_raw_a8(_untile360_raw(raw, width, height, 1), width, height)
    raise RX2ParseError("unhandled texture format 0x%02X" % fmt)


def _main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Parse an EA Skate RX2 file.")
    parser.add_argument("input", help="path to the .rx2 / .dat file")
    parser.add_argument("--toc", action="store_true", help="dump the file table")
    parser.add_argument("--out-dir", help="save every texture as PNG into this directory")
    args = parser.parse_args(argv)

    rx2 = parse_rx2(args.input)
    print(rx2)
    for w in rx2.warnings:
        print("WARNING:", w)
    if args.toc:
        for entry in rx2.entries:
            print("  ", entry)
    for tex in rx2.textures:
        print("  ", tex)
    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for tex in rx2.textures:
            p = out / (tex.name + ".png")
            tex.save_png(p)
            print("saved", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
