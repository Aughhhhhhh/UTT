from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from archive_manager import ArchiveManager


class ArchiveManagerTest(unittest.TestCase):
    def test_repack_excludes_previous_output(self) -> None:
        tool = Path(__file__).resolve().parents[1] / "assets" / "bigfile.exe"
        if not tool.is_file():
            self.skipTest("bigfile.exe is not available")

        with tempfile.TemporaryDirectory(prefix="utt_archive_test_") as temp:
            cache = Path(temp)
            source = cache / "data" / "content" / "sample.bin"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"UTT archive test" * 100)

            manager = ArchiveManager(tool)
            first = manager.repack(cache)
            second = manager.repack(cache)

            self.assertEqual(first.file_count, 1)
            self.assertEqual(second.file_count, 1)
            self.assertEqual(first.size, second.size)
            self.assertEqual(
                second.path,
                cache.resolve() / "data" / "createacharacter.big",
            )


if __name__ == "__main__":
    unittest.main()
