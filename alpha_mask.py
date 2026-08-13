"""Alpha Mask library for Skate 3 texture parsing/exports.

Faithful Python port of the Paint.NET "Alpha Mask Import" effect
(C# source: AlphaMaskImportEffect.cs). The effect replaces an image's
alpha channel with the luminance of a mask image, sampled tiled over
the target, with optional Invert and Mix modes.

Original core (C#):
    luma  = (int)(R * 0.3 + G * 0.59 + B * 0.11)   # mask pixel
    col.A = luma;                                  # invert: 255 - luma
    if (AlphaMix) col.A = (byte)((preCol / 255) * luma)  # preCol = old alpha

In UTT the mask is usually the texture's own RGB luminance, which
replaces the noisy DXT5 alpha (specular/gloss data) with a clean
brightness mask and hides DXT1 transparent-black dots.
"""

import numpy as np
from PIL import Image, ImageChops


def luminance(mask) -> Image.Image:
    """Grayscale "L" image of the mask using the plugin's luma weights.

    Uses exact integer math (30R + 59G + 11B) / 100, which matches the
    C# int(R * 0.3 + G * 0.59 + B * 0.11) within one alpha unit.
    """
    rgba = mask.convert("RGBA")
    arr = np.frombuffer(rgba.tobytes(), dtype=np.uint8).reshape(-1, 4)
    luma = (
        arr[:, 0].astype(np.int32) * 30
        + arr[:, 1].astype(np.int32) * 59
        + arr[:, 2].astype(np.int32) * 11
    ) // 100
    return Image.frombytes("L", mask.size, luma.astype(np.uint8).tobytes())


def apply_alpha_mask(image, mask, invert: bool = False, alpha_mix: bool = False) -> Image.Image:
    """Return a copy of ``image`` with its alpha derived from ``mask``.

    The mask is sampled with wrap-around (mask[x % maskW, y % maskH]),
    exactly like the original plugin. The image's RGB is left untouched.
    """
    image = image.convert("RGBA")
    luma = luminance(mask)
    if luma.size != image.size:
        tiled = Image.new("L", image.size)
        for oy in range(0, image.size[1], luma.size[1]):
            for ox in range(0, image.size[0], luma.size[0]):
                tiled.paste(luma, (ox, oy))
        luma = tiled
    if invert:
        luma = luma.point(lambda v: 255 - v)
    if alpha_mix:
        luma = ImageChops.multiply(image.getchannel("A"), luma)
    image.putalpha(luma)
    return image
