import shutil
import sys
from datetime import datetime
from pathlib import Path

import pymem
import pymem.exception
import pymem.process
from S3RecipeHandler.Helpers import Helpers
from S3RecipeHandler.Recipe import Recipe

RECIPE_ADDRESS = 0x3018DE800
RECIPE_SIZE = 6500


class RPCS3NotFoundError(Exception):
    pass


class RecipeReadError(Exception):
    pass


def get_base_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def find_rpcs3() -> pymem.Pymem:
    entry = pymem.process.process_from_name("rpcs3")
    if entry is None:
        raise RPCS3NotFoundError(
            "RPCS3 was not found, make sure the emulator is open before scanning."
        )
    try:
        return pymem.Pymem(entry.th32ProcessID)
    except pymem.exception.ProcessError as e:
        raise RPCS3NotFoundError(f"RPCS3 was found but could not be opened: {e}")


def read_recipe_bytes(rpcs3: pymem.Pymem) -> bytes:
    try:
        return rpcs3.read_bytes(RECIPE_ADDRESS, RECIPE_SIZE)
    except pymem.exception.PymemMemoryError as e:
        raise RecipeReadError(
            f"Could not read the recipe from memory, is a skate 3 save loaded? ({e})"
        )


def hex_name(raw: bytes) -> str:
    return "0x" + bytes(raw).hex()


def parse_recipe(recipe_bytes: bytes) -> list:
    game_recipe = Recipe(recipe_bytes)
    game_recipe.remove_low_lod_models()

    items = []
    for asset_list in game_recipe.asset_lists:
        model = None
        textures = {}
        for asset in asset_list.assets:
            for game_model in asset.Models:
                model = hex_name(game_model.ModelName)
                for texture in game_model.Textures:
                    textures[texture.texture_channel] = hex_name(texture.texture_name)
        items.append({"name": asset_list.asset_folder_name, "model": model, "textures": textures})
    return items


def format_items(items: list) -> str:
    lines = []
    for item in items:
        lines.append(f"Item: {item['name']}")
        lines.append(f"  Model: {item['model']}")
        texture_lines = [f"    {channel}: {name}" for channel, name in item["textures"].items()]
        if texture_lines:
            lines.append("  Textures:")
            lines.extend(texture_lines)
        lines.append("")
    return "\n".join(lines)


def write_items_txt(items: list, output_folder: Path) -> Path:
    output_folder.mkdir(parents=True, exist_ok=True)
    txt_path = output_folder / "current_items.txt"
    header = [
        "Skate 3 Character Items",
        "=======================",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    txt_path.write_text("\n".join(header) + "\n" + format_items(items), encoding="utf-8")
    return txt_path


def read_items_txt(txt_path) -> list:
    """Reverse of format_items: load items from a saved current_items.txt."""
    items = []
    current = None
    for raw in Path(txt_path).read_text(encoding="utf-8").splitlines():
        if raw.startswith("Item: "):
            current = {"name": raw[6:].strip(), "model": None, "textures": {}}
            items.append(current)
        elif current is not None and raw.startswith("  Model: "):
            value = raw[9:].strip()
            current["model"] = None if value in ("", "None") else value
        elif current is not None and raw.startswith("    "):
            channel, _, value = raw.strip().partition(":")
            if value.strip():
                current["textures"][channel.strip()] = value.strip()
    return items


def scan_and_save(output_folder: Path = None) -> dict:
    rpcs3 = find_rpcs3()
    recipe_bytes = read_recipe_bytes(rpcs3)
    items = parse_recipe(recipe_bytes)
    if output_folder is None:
        output_folder = get_base_path() / "Output"
    if output_folder.exists():
        shutil.rmtree(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)
    txt_path = write_items_txt(items, output_folder)
    return {"items": items, "txt_path": txt_path, "output_folder": output_folder}


if __name__ == "__main__":
    result = scan_and_save()
    print(f"Found {len(result['items'])} items")
    print(f"Saved to {result['txt_path']}")
