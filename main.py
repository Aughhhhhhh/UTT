"""Application entry point for UTT."""
from pathlib import Path
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon

from mainui import MainWindow
from mdl_parser import load_psg


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("UTT")
    app.setOrganizationName("UTT")
    app.setStyle("Fusion")
    # Use bundled icon if present
    ico = Path(__file__).with_name("UTT.ico")
    if ico.exists():
        app.setWindowIcon(QIcon(str(ico)))
    window = MainWindow(model_loader=load_psg)
    window.showMaximized()
    window.activateWindow()
    window.raise_()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
