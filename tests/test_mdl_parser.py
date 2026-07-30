from __future__ import annotations

import struct
import unittest

import numpy as np

from mdl_parser import is_psg_model, parse_psg


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


if __name__ == "__main__":
    unittest.main()
