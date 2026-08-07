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

# Xbox 360 build recipe location — NOT KNOWN YET. The PS3 address above does
# not apply to the 360 build. Find the current-skater recipe in the 360 game
# (Cheat Engine AOB scan for a known equipped asset id, or Ghidra on
# default.xex) and set the guest address here. The same guest address works
# for both Xenia and skate3recomp (same game build).
XBOX_RECIPE_ADDRESS = 0x00000000


class GameNotFoundError(Exception):
    pass


class RecipeReadError(Exception):
    pass


def get_base_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def find_rpcs3() -> pymem.Pymem:
    entry = pymem.process.process_from_name("rpcs3.exe")
    if entry is None:
        raise GameNotFoundError(
            "RPCS3 was not found, make sure the emulator is open before scanning."
        )
    try:
        return pymem.Pymem(entry.th32ProcessID)
    except pymem.exception.ProcessError as e:
        raise GameNotFoundError(f"RPCS3 was found but could not be opened: {e}")


def find_target() -> tuple:
    """Attach to the Xbox 360 game process: skate3recomp (skate3.exe) first,
    then Xenia (canary or master)."""
    for name, label in (
        ("skate3.exe", "skate3.exe (Recomp)"),
        ("xenia_canary.exe", "Xenia Canary"),
        ("xenia.exe", "Xenia"),
    ):
        entry = pymem.process.process_from_name(name)
        if entry is None:
            continue
        try:
            return pymem.Pymem(entry.th32ProcessID), label
        except pymem.exception.ProcessError as e:
            raise GameNotFoundError(f"{label} was found but could not be opened: {e}")
    raise GameNotFoundError(
        "No Xbox 360 game process found — start skate3recomp (skate3.exe) "
        "or Xenia before scanning."
    )


def read_recipe_bytes(proc: pymem.Pymem, address: int) -> bytes:
    try:
        return proc.read_bytes(address, RECIPE_SIZE)
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


def scan_and_save(output_folder: Path = None, platform: str = "ps3") -> dict:
    if platform == "xbx":
        if XBOX_RECIPE_ADDRESS == 0:
            raise RecipeReadError(
                "The Xbox 360 recipe address is not known yet. Find the "
                "current-skater recipe in the 360 game (Cheat Engine AOB scan "
                "for a known equipped asset id, or Ghidra on default.xex) and "
                "set XBOX_RECIPE_ADDRESS in recipe.py."
            )
        proc, target = find_target()
        address = XBOX_RECIPE_ADDRESS
    else:
        proc = find_rpcs3()
        address = RECIPE_ADDRESS
        target = "RPCS3"
    recipe_bytes = read_recipe_bytes(proc, address)
    items = parse_recipe(recipe_bytes)
    if output_folder is None:
        output_folder = get_base_path() / ("output_xbx" if platform == "xbx" else "output")
    if output_folder.exists():
        shutil.rmtree(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)
    txt_path = write_items_txt(items, output_folder)
    return {
        "items": items,
        "txt_path": txt_path,
        "output_folder": output_folder,
        "target": target,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["ps3", "xbx"], default="ps3")
    args = parser.parse_args()
    result = scan_and_save(platform=args.platform)
    print(f"Attached to {result['target']}")
    print(f"Found {len(result['items'])} items")
    print(f"Saved to {result['txt_path']}")
