from .errors import PSGDataError, PSGError, PSGFormatError
from .model import Bone, MaterialParameter, Mesh, PSGModel, VertexAttribute
from .parser import (
    extract_material_textures,
    is_psg_model,
    is_rx2_model,
    load_model,
    load_psg,
    load_rx2,
    parse_model,
    parse_psg,
    parse_rx2,
)

__version__ = "2.1.1"

__all__ = [
    "Bone",
    "MaterialParameter",
    "Mesh",
    "PSGDataError",
    "PSGError",
    "PSGFormatError",
    "PSGModel",
    "VertexAttribute",
    "extract_material_textures",
    "is_psg_model",
    "is_rx2_model",
    "load_model",
    "load_psg",
    "load_rx2",
    "parse_model",
    "parse_psg",
    "parse_rx2",
]
