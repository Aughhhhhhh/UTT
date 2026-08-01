import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image


class PSGConverter:
    def __init__(self, assets_dir: str | None = None):
        if assets_dir is None:
            assets_dir = str(Path(__file__).resolve().parent / "assets")

        self.assets_dir = Path(assets_dir).resolve()
        self.nvcompress_path = self.assets_dir / "nvcompress.exe"
        self.psg_cli_dir = self.assets_dir / "PsgCliTool"
        self.psg_cli_exe = self.psg_cli_dir / "PsgCliTool.exe"

    def _scale_opacity(self, img: Image.Image, opacity: float) -> Image.Image:
        opacity = max(0.0, min(1.0, opacity))
        if opacity < 1.0:
            alpha = img.getchannel("A").point(lambda value: int(value * opacity))
            img.putalpha(alpha)
        return img

    def convert_image(
        self,
        input_image_path: str,
        output_dir: str,
        alias: str,
        resolution: int = 512,
        opacity: float = 1.0,
    ) -> str:
        input_path = Path(input_image_path).resolve()
        output_path = Path(output_dir).resolve()
        alias = alias.lower()

        if not input_path.is_file():
            raise FileNotFoundError(f"Input image not found: {input_path}")
        if len(alias) != 18 or not alias.startswith("0x"):
            raise ValueError("Alias must be an 18-character hex string starting with '0x'")
        try:
            int(alias[2:], 16)
        except ValueError as exc:
            raise ValueError("Alias contains non-hexadecimal characters") from exc
        if not 16 <= resolution <= 4096:
            raise ValueError("Resolution must be between 16 and 4096")

        for tool in (self.nvcompress_path, self.psg_cli_exe):
            if not tool.is_file():
                raise FileNotFoundError(
                    f"Required tool not found: {tool}. Keep the assets folder next to UTT.exe."
                )

        output_path.mkdir(parents=True, exist_ok=True)
        final_psg = output_path / f"{alias}.psg"
        generated_psg = self.psg_cli_dir / f"{alias}.psg"

        with tempfile.TemporaryDirectory(
            prefix="utt_convert_", ignore_cleanup_errors=True
        ) as temp_dir:
            temp_path = Path(temp_dir)
            temp_png = temp_path / "input.png"
            temp_dds = temp_path / f"{alias}.dds"

            with Image.open(input_path) as image:
                image = image.convert("RGBA")
                image = image.resize(
                    (resolution, resolution), Image.Resampling.LANCZOS
                )
                self._scale_opacity(image, opacity).save(temp_png, format="PNG")

            self._run_tool(
                [str(self.nvcompress_path), "-bc3", str(temp_png), str(temp_dds)],
                self.assets_dir,
            )
            if generated_psg.exists():
                generated_psg.unlink()
            self._run_tool(
                [str(self.psg_cli_exe), str(temp_dds), generated_psg.name],
                self.psg_cli_dir,
            )
            if not generated_psg.is_file():
                raise RuntimeError("PsgCliTool finished without creating a PSG file")
            os.replace(generated_psg, final_psg)

        return str(final_psg)

    @staticmethod
    def _run_tool(command: list[str], cwd: Path) -> None:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            if detail:
                raise RuntimeError(detail)
            raise RuntimeError(f"{Path(command[0]).name} exited with code {result.returncode}")
