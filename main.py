"""Application entry point for UTT."""
from pathlib import Path
import sys
import traceback

from PyQt6.QtCore import qInstallMessageHandler, qCritical, qWarning, qInfo, qDebug
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon

from mainui import MainWindow, choose_platform
from mdl_parser import load_model

FILE_SUFFIXES = (".rx2", ".psg")


def _write_log(message: str) -> None:
    try:
        log_path = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent / "utt_crash.log"
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass


def _install_crash_logger() -> None:
    def hook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _write_log(f"[{__import__('datetime').datetime.now()}] unhandled exception:\n{text}")
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = hook
    sys.unraisablehook = lambda args: _write_log(
        f"[{__import__('datetime').datetime.now()}] unraisable: {args.exc_value}"
    )

    def qt_handler(_kind, _context, message):
        _write_log(f"[{__import__('datetime').datetime.now()}] Qt: {message}")

    qInstallMessageHandler(qt_handler)


def find_opened_file() -> Path | None:
    """Return the .rx2/.psg path UTT was launched with, if any."""
    for argument in sys.argv[1:]:
        path = Path(argument)
        if path.suffix.lower() in FILE_SUFFIXES and path.is_file():
            return path
    return None


def main() -> int:
    _install_crash_logger()
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
    window.show()
    window._toggle_maximized()
    window.activateWindow()
    window.raise_()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
