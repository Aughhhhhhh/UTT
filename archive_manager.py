from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class RepackResult:
    path: Path
    file_count: int
    size: int


class ArchiveManager:
    def __init__(self, bigfile_path: str | Path):
        self.bigfile_path = Path(bigfile_path).resolve()

    def repack(
        self,
        cache_dir: str | Path,
        compression: int = 0,
        data_rel_path: str = "data",
        subdirs: tuple[str, ...] | None = None,
    ) -> RepackResult:
        """Pack files under ``cache_dir/<data_rel_path>`` into createacharacter.big.

        Archive paths are stored relative to ``cache_dir``, so the packed
        archive mirrors the game's loose layout (e.g. ``data/content/...``).
        When ``subdirs`` is given, only files whose top-level folder under the
        data path is in that set are packed (used for unpacked Xbox mode,
        which repacks just createacharacter + recipe).
        """
        if not self.bigfile_path.is_file():
            raise FileNotFoundError(
                f"Required tool not found: {self.bigfile_path}. "
                "Keep the assets folder next to UTT.exe."
            )

        if not 0 <= int(compression) <= 4:
            raise ValueError("compression must be between 0 (none) and 4 (LZX)")

        cache_path = Path(cache_dir).resolve()
        data_path = (cache_path / data_rel_path).resolve() if data_rel_path else cache_path
        target_path = data_path / "createacharacter.big"
        if not data_path.is_dir():
            raise FileNotFoundError(f"Data folder not found: {data_path}")

        files = sorted(
            (
                path
                for path in data_path.rglob("*")
                if path.is_file() and path.resolve() != target_path
            ),
            key=lambda path: str(path).lower(),
        )
        if subdirs:
            files = [
                path
                for path in files
                if path.relative_to(data_path).parts[0] in subdirs
            ]
        if not files:
            raise RuntimeError("The data folder does not contain any files to pack")

        response_path = cache_path / ".utt_repack_files.rsp"
        staging_path = data_path / "createacharacter.big.new"
        try:
            response_path.write_text(
                "\n".join(
                    f'"{path.relative_to(cache_path)}"' for path in files
                ),
                encoding="utf-8",
            )

            self._run(
                [
                    str(self.bigfile_path),
                    str(staging_path),
                    f"-compress{int(compression)}",
                    "-fat",
                    f"@{response_path}",
                ],
                cache_path,
            )
            if not staging_path.is_file() or staging_path.stat().st_size == 0:
                raise RuntimeError("bigfile.exe finished without creating an archive")

            os.replace(staging_path, target_path)
        finally:
            staging_path.unlink(missing_ok=True)
            response_path.unlink(missing_ok=True)

        return RepackResult(
            path=target_path,
            file_count=len(files),
            size=target_path.stat().st_size,
        )

    @staticmethod
    def _run(command: list[str], cwd: Path) -> None:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            detail = result.stderr.strip()
            if detail:
                raise RuntimeError(detail.splitlines()[-1])
            raise RuntimeError(
                f"{Path(command[0]).name} exited with code {result.returncode}"
            )
