"""Game-folder route (Xbox 360 / PS3): locate createacharacter.big, back it
up, and unpack it.

The route works from the user's game folder instead of a manually chosen
archive. The untouched original createacharacter.big has a platform-specific
size (Xbox ~450 MB, PS3 ~446 MB) while repacked archives are around 600 MB,
so the size guard below distinguishes the original from a user's repack
(with a small tolerance).

For PS3 the user selects their RPCS3 root folder, and UTT derives the game
from ``games/Skate_3_BLUS/PS3_GAME`` (or ``Skate_3_BLES``):

    <rpcs3>/games/<title>/PS3_GAME/USRDIR/data/content

The PS3 INSTALL folders under ``dev_hdd0/game/<serial>_INSTALL`` are cleaned
silently on launch so the installed copy never shadows UTT's loose files.
"""

from __future__ import annotations

import os
from pathlib import Path

ORIGINAL_BIG_BYTES = {
    "xbx": 461_258 * 1024,  # 472,328,192 bytes (~450 MB)
    "ps3": 457_370 * 1024,  # 468,346,880 bytes (~446 MB)
}
ORIGINAL_BIG_TOLERANCE = 1024  # ±1 KB

# (game folder name under <rpcs3>/games, RPCS3 serial). UTT must handle both
# Skate 3 serials, so the one actually installed is detected at runtime.
PS3_TITLES = (
    ("Skate_3_BLUS", "BLUS30464"),
    ("Skate_3_BLES", "BLES00760"),
)


def ps3_game_dir(rpcs3_folder: str | Path) -> Path | None:
    """The installed Skate 3 ``PS3_GAME`` folder under an RPCS3 root, or None.

    Both serials are probed. When both are installed, the one whose
    ``USRDIR/data/content`` already exists wins; otherwise the first present
    title is used.
    """
    base = Path(rpcs3_folder)
    found: Path | None = None
    for title, _serial in PS3_TITLES:
        candidate = base / "games" / title / "PS3_GAME"
        if candidate.is_dir():
            if (candidate / "USRDIR" / "data" / "content").is_dir():
                return candidate
            if found is None:
                found = candidate
    return found


def game_root(game_folder: str | Path, platform: str = "xbx") -> Path:
    """The folder archive paths are rooted at when packing.

    Xbox packs relative to the game folder itself (``data/content/...``). PS3
    packs relative to the detected PS3_GAME folder
    (``USRDIR/data/content/...``).
    """
    if platform == "ps3":
        return ps3_game_dir(game_folder) or Path(game_folder)
    return Path(game_folder)


def content_dir(game_folder: str | Path, platform: str = "xbx") -> Path:
    """The game folder's loose-content directory.

    Xbox:  <game>/data/content
    PS3:   <rpcs3>/games/Skate_3_BLUS|Skate_3_BLES/PS3_GAME/USRDIR/data/content

    Older configs that point PS3 at PS3_GAME (or USRDIR) directly are still
    accepted as a fallback.
    """
    base = Path(game_folder)
    if platform == "ps3":
        game = ps3_game_dir(base)
        if game is not None:
            return game / "USRDIR" / "data" / "content"
        # Legacy: the user may have selected PS3_GAME or USRDIR directly.
        if (base / "USRDIR").is_dir():
            return base / "USRDIR" / "data" / "content"
        if (base / "data" / "content").is_dir():
            return base / "data" / "content"
        if (base / "games").is_dir() or (base / "dev_hdd0").is_dir():
            # An RPCS3 root with no Skate 3 title detected yet: default to
            # the BLUS layout so locate/repack resolve a stable path.
            return base / "games" / "Skate_3_BLUS" / "PS3_GAME" / "USRDIR" / "data" / "content"
        # A bare legacy PS3_GAME folder without USRDIR yet.
        return base / "data" / "content"
    return base / "data" / "content"


def ps3_install_folders(rpcs3_folder: str | Path) -> list[Path]:
    """The ``dev_hdd0/game/<serial>_INSTALL`` folders for both Skate 3 serials.

    ``dev_hdd0`` is a fixed RPCS3 path (the game wouldn't run otherwise), so
    UTT checks both known serials and cleans whichever folders exist — no
    title detection needed.
    """
    base = Path(rpcs3_folder)
    return [
        base / "dev_hdd0" / "game" / f"{serial}_INSTALL"
        for _title, serial in PS3_TITLES
    ]


def clean_ps3_install_folders(rpcs3_folder: str | Path) -> None:
    """Silently delete the contents of the PS3 INSTALL folders (PS3 only).

    RPCS3 installs game data under dev_hdd0/game/<serial>_INSTALL. When UTT
    manages the loose files in PS3_GAME/USRDIR/data/content, that installed
    copy must not shadow them, so the install folder's contents are removed
    each launch. An empty (or missing) folder is left untouched.
    """
    import shutil

    for folder in ps3_install_folders(rpcs3_folder):
        if not folder.is_dir():
            continue
        for item in folder.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            except OSError:
                continue


def loose_big(game_folder: str | Path, platform: str = "xbx") -> Path:
    """The loose archive location inside the platform's content directory."""
    return content_dir(game_folder, platform) / "createacharacter.big"


def find_backup_big(game_folder: str | Path, platform: str = "xbx") -> Path | None:
    """Find createacharacter.big inside a folder whose name contains 'backup'.

    Backup folders can be spelled many ways (Backup, backup, BACKUP, ...), so
    the match is a case-insensitive substring search for 'backup'.
    """
    content = content_dir(game_folder, platform)
    if not content.is_dir():
        return None
    for folder in content.iterdir():
        if folder.is_dir() and "backup" in folder.name.lower():
            candidate = folder / "createacharacter.big"
            if candidate.is_file():
                return candidate
    return None


def locate_or_backup_source(
    game_folder: str | Path, platform: str = "xbx"
) -> Path | None:
    """Return the archive to unpack from.

    Priority:
      1. A loose createacharacter.big in <game>/data/content. If it matches
         the untouched original's size it is moved into a Backup folder and
         that copy is returned; otherwise (a user's repack) it is used in
         place so the original backup is never overwritten.
      2. Otherwise any existing backup folder's createacharacter.big.
      3. Otherwise None, so the caller can prompt for a manual selection.
    """
    content = content_dir(game_folder, platform)
    loose = content / "createacharacter.big"
    if loose.is_file():
        if is_original_big(loose, platform):
            backup_dir = content / "Backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            dest = backup_dir / "createacharacter.big"
            if not dest.is_file():
                os.replace(loose, dest)
            return dest
        return loose
    return find_backup_big(game_folder, platform)


def is_original_big(path: str | Path, platform: str = "xbx") -> bool:
    """True when the file matches the untouched original size within tolerance."""
    expected = ORIGINAL_BIG_BYTES.get(platform)
    if expected is None:
        return False
    try:
        size = Path(path).stat().st_size
    except OSError:
        return False
    return abs(size - expected) <= ORIGINAL_BIG_TOLERANCE


def unpack_into_game(
    bigfile: str | Path,
    archive: str | Path,
    game_folder: str | Path,
    platform: str = "xbx",
) -> tuple[bool, bool]:
    """Unpack createacharacter + recipe from an archive into the game folder.

    The archive is extracted to a temporary folder, then only the two subtrees
    the game reads are copied out:
      temp/data/content/createacharacter -> <game>/data/content/createacharacter
      temp/data/content/recipe           -> <game>/data/content/recipe
    Existing loose files are never overwritten, so a partial or repeated unpack
    cannot clobber the user's replaced textures.

    Returns (created_createacharacter, created_recipe).
    """
    import shutil
    import subprocess
    import tempfile

    game_content = content_dir(game_folder, platform)
    game_content.mkdir(parents=True, exist_ok=True)
    created_char = False
    created_recipe = False
    with tempfile.TemporaryDirectory() as temp:
        temp_root = Path(temp)
        subprocess.run(
            [str(bigfile), str(archive), "-x"],
            cwd=temp_root,
            check=True,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if platform == "ps3":
            char_candidates = (
                temp_root / "USRDIR" / "data" / "content" / "createacharacter",
                temp_root / "data" / "content" / "createacharacter",
                temp_root / "content" / "createacharacter",
            )
            recipe_candidates = (
                temp_root / "USRDIR" / "data" / "content" / "recipe",
                temp_root / "data" / "content" / "recipe",
                temp_root / "content" / "recipe",
            )
        else:
            char_candidates = (
                temp_root / "data" / "content" / "createacharacter",
                temp_root / "content" / "createacharacter",
            )
            recipe_candidates = (
                temp_root / "data" / "content" / "recipe",
                temp_root / "content" / "recipe",
            )
        src_char = next((p for p in char_candidates if p.is_dir()), None)
        src_recipe = next((p for p in recipe_candidates if p.is_dir()), None)
        dst_char = game_content / "createacharacter"
        model_ext = "rx2" if platform == "xbx" else "psg"
        if src_char is not None and not (
            dst_char.is_dir() and any(dst_char.rglob(f"*.{model_ext}"))
        ):
            shutil.copytree(src_char, dst_char, dirs_exist_ok=True)
            created_char = True
        dst_recipe = game_content / "recipe"
        if src_recipe is not None and not dst_recipe.exists():
            shutil.copytree(src_recipe, dst_recipe)
            created_recipe = True
    return created_char, created_recipe
