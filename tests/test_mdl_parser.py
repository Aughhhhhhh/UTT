from __future__ import annotations

import struct
import unittest

import numpy as np

from mdl_parser import is_psg_model, is_rx2_model, parse_psg, parse_rx2


class SyntheticPSGTest(unittest.TestCase):
    def test_triangle_model(self) -> None:
        data = bytearray(0x500)
        file_table = 0x100
        mesh_info = 0x200
        vertex_buffer = 0x300
        vertex_info = 0x350
        face_meta = 0x380
        face_buffer = 0x3A0

        data[:7] = b"\x89RW4ps3"
        data[0x70:0x74] = b"\x00\x00\x00\x10"
        struct.pack_into(">i", data, 0x20, 6)
        struct.pack_into(">i", data, 0x30, file_table)
        struct.pack_into(">i", data, 0x44, 0)
        struct.pack_into(">i", data, 0x230, 0)

        def record(
            index: int, values: tuple[int, int, int, int, int], kind: bytes
        ) -> None:
            offset = file_table + index * 24
            struct.pack_into(">5i", data, offset, *values)
            data[offset + 20 : offset + 24] = kind

        record(0, (vertex_buffer, 0, 36, 0, 0), b"\x00\x00\x00\x00")
        record(1, (vertex_info, 0, 0, 0, 0), b"\x00\x02\x00\xEA")
        record(2, (face_buffer, 0, 6, 0, 0), b"\x00\x00\x00\x00")
        record(3, (face_meta, 0, 0, 0, 0), b"\x00\x02\x00\xEB")
        record(4, (0, 0, 0, 0, 0), b"\x00\x00\x00\x00")
        record(5, (mesh_info, 0, 0, 0, 0), b"\x00\x02\x00\xE9")

        struct.pack_into(">iiHHi", data, mesh_info, 0, 0, 0, 1, 0)
        data[mesh_info + 16 : mesh_info + 24] = (
            b"\x02\x03\x00\x00\x00\x0C\x00\x01"
        )

        vertices = (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
        )
        for index, vertex in enumerate(vertices):
            struct.pack_into(">3f", data, vertex_buffer + index * 12, *vertex)

        struct.pack_into(">i", data, face_meta + 8, 3)
        struct.pack_into(">3H", data, face_buffer, 0, 1, 2)

        self.assertTrue(is_psg_model(data))
        model = parse_psg(data)
        self.assertEqual(len(model.meshes), 1)
        self.assertEqual(model.vertex_count, 3)
        self.assertEqual(model.triangle_count, 1)
        np.testing.assert_allclose(model.meshes[0].vertices, np.asarray(vertices))
        np.testing.assert_array_equal(model.meshes[0].faces, [[0, 1, 2]])


class SyntheticRX2Test(unittest.TestCase):
    def test_triangle_model_x360(self) -> None:
        """X360 models: 16-byte descriptors, a stride byte, int16 attributes.

        Positions and UVs are stored as SHORT and must be normalized by
        32768, matching the Noesis plugin's integer handling.
        """
        data = bytearray(0x500)
        file_table = 0x100
        mesh_info = 0x200
        vertex_buffer = 0x300
        vertex_info = 0x350
        face_meta = 0x378
        face_buffer = 0x3A0

        data[:7] = b"\x89RW4xb2"
        data[0x58:0x5C] = b"\x00\x00\x00\x04"
        struct.pack_into(">i", data, 0x20, 6)
        struct.pack_into(">i", data, 0x30, file_table)
        struct.pack_into(">i", data, 0x44, 0)
        struct.pack_into(">i", data, 0x230, 0)

        def record(
            index: int, values: tuple[int, int, int, int, int], kind: bytes
        ) -> None:
            offset = file_table + index * 24
            struct.pack_into(">5i", data, offset, *values)
            data[offset + 20 : offset + 24] = kind

        record(0, (vertex_buffer, 0, 36, 0, 0), b"\x00\x00\x00\x00")
        record(1, (vertex_info, 0, 0, 0, 0), b"\x00\x02\x00\xEA")
        record(2, (face_buffer, 0, 6, 0, 0), b"\x00\x00\x00\x00")
        record(3, (face_meta, 0, 0, 0, 0), b"\x00\x02\x00\xEB")
        record(4, (0, 0, 0, 0, 0), b"\x00\x00\x00\x00")
        record(5, (mesh_info, 0, 0, 0, 0), b"\x00\x02\x00\xE9")

        struct.pack_into(">iiHHi", data, mesh_info, 0, 0, 2, 0, 0)
        position_descriptor = (
            b"\x00\x00\x00\x00\x00\x1A\x21\x5A\x00\x00\x00\x01\x00\x00\x00\x00"
        )
        uv_descriptor = (
            b"\x00\x00\x00\x08\x00\x2C\x21\x59\x00\x05\x00\x06\x00\x00\x00\x00"
        )
        data[mesh_info + 16 : mesh_info + 32] = position_descriptor
        data[mesh_info + 32 : mesh_info + 48] = uv_descriptor
        data[mesh_info + 48] = 12  # vertex stride byte

        # Three vertices: position (int16 x3) + uv (int16 x2), stride 12.
        # 16384 / 32768 = 0.5
        for index in range(3):
            base = vertex_buffer + index * 12
            struct.pack_into(">3h", data, base, *(16384, 0, 0) if index == 0 else (0, 16384, 0) if index == 1 else (0, 0, 16384))
            struct.pack_into(">2h", data, base + 8, 0, 32767)

        struct.pack_into(">i", data, face_meta + 32, 3)
        struct.pack_into(">3H", data, face_buffer, 0, 1, 2)

        self.assertTrue(is_rx2_model(data))
        model = parse_rx2(data)
        self.assertEqual(len(model.meshes), 1)
        self.assertEqual(model.vertex_count, 3)
        self.assertEqual(model.triangle_count, 1)
        self.assertEqual(model.meshes[0].vertex_stride, 12)
        np.testing.assert_allclose(
            model.meshes[0].vertices,
            [[0.5, 0.0, 0.0], [0.0, 0.5, 0.0], [0.0, 0.0, 0.5]],
            atol=1e-6,
        )
        self.assertIsNotNone(model.meshes[0].uvs)
        np.testing.assert_allclose(
            model.meshes[0].uvs, [[0.0, 32767 / 32768]] * 3, atol=1e-6
        )
        np.testing.assert_array_equal(model.meshes[0].faces, [[0, 1, 2]])
        self.assertEqual(model.metadata["platform"], "xbx")

    def test_wrong_x360_type_rejected(self) -> None:
        data = bytearray(0x5C)
        data[:7] = b"\x89RW4xb2"
        data[0x58:0x5C] = b"\x00\x00\x10\x00"  # texture, not a model
        self.assertFalse(is_rx2_model(data))
        with self.assertRaises(Exception):
            parse_rx2(data)


if __name__ == "__main__":
    unittest.main()
