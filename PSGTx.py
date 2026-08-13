"""
PSG Texture (tx) Parser Library for Skate 2/3 :3
--------------------------------------------
A simple, yet advanced library to parse, modify, and export .psg texture files.
Handles dynamic DXT format detection and alpha channel manipulation.
"""

import struct
import io
from PIL import Image

from alpha_mask import apply_alpha_mask

class PSGTx:
    def __init__(self, file_source):
        """
        Initializes the PSG Texture object.
        
        :param file_source: Can be a string (filepath) or raw bytes.
        """
        self.tx_width = 0
        self.tx_height = 0
        self.tx_format = b'DXT5'
        self.tx_payload = b''
        
        # Internal PIL Image object
        self._raw_image = None
        
        # Load the data immediately upon instantiation
        self._load_tx_data(file_source)

    def _load_tx_data(self, source):
        """Internal method to load bytes from either a file path or a byte stream."""
        if isinstance(source, str):
            with open(source, 'rb') as f:
                tx_data = f.read()
        elif isinstance(source, (bytes, bytearray)):
            tx_data = source
        else:
            raise TypeError("Source must be a file path (str) or raw bytes.")
            
        self.parse_tx(tx_data)

    def _build_dds_header(self) -> bytes:
        """
        Constructs a valid 128-byte DDS header for Pillow to read.
        Automatically uses the dynamically detected tx_format (DXT1/DXT5).
        """
        header = bytearray(128)
        header[0:4] = b'DDS '
        header[4:8] = struct.pack('<I', 124)                     # Header Size
        header[8:12] = struct.pack('<I', 0x081007)               # Required Flags
        header[12:16] = struct.pack('<I', self.tx_height)
        header[16:20] = struct.pack('<I', self.tx_width)
        header[28:32] = struct.pack('<I', 1)                     # MipMap Count
        header[76:80] = struct.pack('<I', 32)                    # Pixel Format Size
        header[80:84] = struct.pack('<I', 0x04)                  # DDPF_FOURCC Flag
        header[84:88] = self.tx_format                           # DXT1 or DXT5
        header[108:112] = struct.pack('<I', 0x1000)              # DDSCAPS_TEXTURE
        
        return bytes(header)

    def parse_tx(self, tx_bytes: bytes):
        """
        Core parsing logic. Extracts dimensions, payload, and format from raw bytes.
        """
        if len(tx_bytes) < 0x248:
            raise ValueError("Invalid PSG file: File size is too small to contain texture data.")

        # 1. Parse Width (0x164) and Height (0x166) - Big Endian
        self.tx_width = struct.unpack(">H", tx_bytes[0x164:0x166])[0]
        self.tx_height = struct.unpack(">H", tx_bytes[0x166:0x168])[0]

        if self.tx_width == 0 or self.tx_height == 0:
            raise ValueError(f"Invalid dimensions: {self.tx_width}x{self.tx_height}")

        # 2. Extract raw image payload (starts at offset 0x248)
        self.tx_payload = tx_bytes[0x248:]
        payload_size = len(self.tx_payload)

        # 3. Dynamically determine compression format
        # DXT5 requires 1 byte per pixel. DXT1 requires 0.5 bytes per pixel.
        if payload_size < (self.tx_width * self.tx_height):
            self.tx_format = b'DXT1'
        else:
            self.tx_format = b'DXT5'

        # 4. Generate DDS wrapper and load into Pillow
        dds_header = self._build_dds_header()
        full_dds_file = dds_header + self.tx_payload
        
        # Load and ensure it has an Alpha channel (RGBA) for potential modifications
        self._raw_image = Image.open(io.BytesIO(full_dds_file)).convert("RGBA")

    def get_tx_image(self, force_opaque: bool = False, alpha_mask: bool = False,
                     alpha_mask_invert: bool = False, alpha_mask_mix: bool = False) -> Image.Image:
        """
        Returns a Pillow Image object of the texture.
        
        :param force_opaque: If True, boosts all visible pixels' alpha to 255 
                             to fix transparency bleed without ruining the background.
        :param alpha_mask: If True, replaces the alpha channel with the
                           texture's own luminance (the Alpha Mask plugin port)
                           to remove DXT5 alpha grain and transparent dots.
        :param alpha_mask_invert: Invert the mask before applying.
        :param alpha_mask_mix: Blend the mask with the original alpha instead
                               of replacing it.
        :return: PIL.Image object

        The alpha mask is applied first, then force_opaque runs last, so
        checking both removes the DXT5 alpha grain AND leaves the image
        fully opaque (only pure-black mask pixels stay transparent).
        """
        if not self._raw_image:
            raise RuntimeError("No texture data loaded.")

        img = self._raw_image.copy()

        if alpha_mask:
            img = apply_alpha_mask(
                img, img, invert=alpha_mask_invert, alpha_mix=alpha_mask_mix
            )

        if force_opaque:
            # Split channels and manipulate only the alpha channel
            r, g, b, a = img.split()

            # If alpha is >= 5, make it fully solid (255). 
            # Keeps < 5 as 0 to hide DXT compression background artifacts.
            new_a = a.point(lambda p: 0 if p < 5 else 255)

            img = Image.merge("RGBA", (r, g, b, new_a))

        return img

    def export_tx(self, output_path: str, force_opaque: bool = False,
                  alpha_mask: bool = False, alpha_mask_invert: bool = False,
                  alpha_mask_mix: bool = False):
        """
        Exports the texture to a standard image file format (PNG, JPG, TGA, BMP).
        
        :param output_path: The file path to save the image (e.g., 'output.png')
        :param force_opaque: If True, applies the solid alpha fix before exporting.
        :param alpha_mask: If True, applies the Alpha Mask plugin fix.
        :param alpha_mask_invert: Invert the mask before applying.
        :param alpha_mask_mix: Blend the mask with the original alpha.
        """
        img = self.get_tx_image(force_opaque, alpha_mask, alpha_mask_invert, alpha_mask_mix)

        # JPGs crash if you try to save an RGBA image. Convert to RGB automatically.
        if output_path.lower().endswith(('.jpg', '.jpeg')):
            img = img.convert("RGB")

        img.save(output_path)


# ==========================================
# USAGE EXAMPLES (How to call the library)
# ==========================================
if __name__ == "__main__":
    import sys
    
    # Example 1: The simplest way to convert a file
    # This reads the psg, auto-detects everything, and saves a PNG.
    try:
        tx = PSGTx("test_texture.psg")
        tx.export_tx("output.png")
        print(f"Successfully converted! Resolution: {tx.tx_width}x{tx.tx_height}")
    except Exception as e:
        print(f"Basic example skipped/failed: {e}")

    # Example 2: Advanced usage with alpha modification
    try:
        tx = PSGTx("0x2c7f3818002d0000.psg")
        
        # Grab the raw Pillow image object in code for further python processing
        pil_img = tx.get_tx_image(force_opaque=True) 
        print(f"Image object loaded. Detected Format: {tx.tx_format.decode()}")
        
        # Export with the alpha fix applied
        tx.export_tx("fixed_texture.png", force_opaque=True)
    except Exception as e:
        print(f"Advanced example skipped/failed: {e}")
