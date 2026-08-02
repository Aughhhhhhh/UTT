"""Application entry point for UTT."""
from pathlib import Path
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon

from mainui import MainWindow, choose_platform
from mdl_parser import load_model

FILE_SUFFIXES = (".rx2", ".psg")


def find_opened_file() -> Path | None:
    """Return the .rx2/.psg path UTT was launched with, if any."""
    for argument in sys.argv[1:]:
        path = Path(argument)
        if path.suffix.lower() in FILE_SUFFIXES and path.is_file():
            return path
    return None


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("UTT")
    app.setOrganizationName("UTT")
    app.setStyle("Fusion")
    # Use bundled icon if present
    ico = Path(__file__).with_name("UTT.ico")
    if ico.exists():
        app.setWindowIcon(QIcon(str(ico)))
    opened_file = find_opened_file()
    if opened_file is not None:
        from quick_viewer import QuickFileViewer
        window = QuickFileViewer(opened_file)
        window.showMaximized()
        window.activateWindow()
        window.raise_()
        return app.exec()
    platform = choose_platform()
    if not platform:
        return 0
    window = MainWindow(model_loader=load_model, platform=platform)
    window.showMaximized()
    window.activateWindow()
    window.raise_()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
