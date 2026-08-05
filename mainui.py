"""PyQt6 user interface for browsing and converting Skate 3 PSG textures."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

from PIL import Image
from PIL.ImageQt import ImageQt
from PyQt6.QtCore import QEvent, QObject, QRect, QRegularExpression, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QPixmap, QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame,
    QGraphicsBlurEffect, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressDialog,
    QPushButton, QSizePolicy, QSlider, QSpinBox, QSplitter, QStackedWidget,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import recipe

from archive_manager import ArchiveManager
from gltf_exporter import export_gltf
from model_viewer import ModelPreview
from psg_converter import PSGConverter
from PSGTx import PSGTx


APP_TITLE = "UTT — Ultimate Texture Toolkit"

CREDITS_TEXT = (
    "Credits\n\n"
    "duckyinnit — had the idea\n"
    "ai — everything\n"
    "itsclaudeya — model viewer\n"
    "Salix — Get Current Models And Textures\n"
    "S4M — PSG Converter\n"
    "Wisp — RX2 Converter\n"
    "GHFear — RX2 Parse\n"
    "Tuukkas — RX2"
)
HEX_LENGTH = 18

PLATFORMS = {
    "xbx": {"name": "Xbox 360", "extension": "rx2", "label": "RX2"},
    "ps3": {"name": "PS3", "extension": "psg", "label": "PSG"},
}


def resource_dir() -> Path:
    """Resolve the app root for bundled assets.

    When the packaged app is launched from a folder where the assets directory
    sits next to the EXE, prefer that folder over PyInstaller's temporary
    extraction path so the app reads the local assets correctly.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "assets").is_dir() or (exe_dir / "psg_list.json").is_file():
            return exe_dir
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()

    return Path(__file__).parent.resolve()


def resource_file(name: str) -> Path:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / name)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass).resolve() / name)
    candidates.append(Path(__file__).resolve().parent / name)
    return next((path for path in candidates if path.is_file()), candidates[-1])


def working_dir() -> Path:
    """Place user-created data beside the executable, not in PyInstaller temp files.

    When running from source during development, prefer the build folder if it
    contains an existing cache from a previously run packaged executable.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    source_dir = Path(__file__).parent.resolve()
    build_dir = source_dir / "build"
    if any((build_dir / "cache" / platform).is_dir() for platform in PLATFORMS):
        return build_dir
    return source_dir


class Worker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, task):
        super().__init__()
        self.task = task

    def run(self):
        try:
            self.finished.emit(self.task())
        except Exception:
            self.failed.emit(traceback.format_exc())


class LogEmitter(QObject):
    """Thread-safe bridge for worker logs: emitting from any thread delivers
    the line to the UI thread through the queued signal."""
    log_line = pyqtSignal(str)


class ImageDropZone(QLabel):
    imageDropped = pyqtSignal(str)
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source_pixmap = QPixmap()
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setText("Drop an image here\nor click to choose one")

    def set_image(self, path: str) -> bool:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return False
        self._source_pixmap = pixmap
        self._scale_pixmap()
        return True

    def dragEnterEvent(self, event):
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.imageDropped.emit(url.toLocalFile())
                event.acceptProposedAction()
                return

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scale_pixmap()

    def _scale_pixmap(self):
        if not self._source_pixmap.isNull():
            self.setPixmap(self._source_pixmap.scaled(
                max(1, self.width() - 32),
                max(1, self.height() - 32),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))


class FullWidthTabWidget(QTabWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.tabBar().setFixedWidth(self.width())


class TitleBar(QFrame):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.window = window
        self.setObjectName("titleBar")
        self.setFixedHeight(42)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 0, 0)
        layout.setSpacing(8)

        icon_label = QLabel()
        icon = QPixmap(str(resource_file("UTT.ico")))
        if not icon.isNull():
            icon_label.setPixmap(icon.scaled(
                20,
                20,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        layout.addWidget(icon_label)

        title = QLabel(APP_TITLE)
        title.setObjectName("windowTitle")
        layout.addWidget(title)
        layout.addStretch(1)

        credits_button = QPushButton("Credits")
        credits_button.setObjectName("creditsButton")
        credits_button.clicked.connect(window._show_credits)
        layout.addWidget(credits_button)

        settings_button = QPushButton("Settings")
        settings_button.setObjectName("settingsButton")
        settings_button.clicked.connect(window._show_settings)
        layout.addWidget(settings_button)

        self.minimize_button = self._window_button("—", "Minimize")
        self.minimize_button.clicked.connect(window.showMinimized)
        layout.addWidget(self.minimize_button)

        self.maximize_button = self._window_button("❐", "Restore")
        self.maximize_button.clicked.connect(window._toggle_maximized)
        layout.addWidget(self.maximize_button)

        self.close_button = self._window_button("×", "Close")
        self.close_button.setObjectName("titleCloseButton")
        self.close_button.clicked.connect(window.close)
        layout.addWidget(self.close_button)

    def _window_button(self, text: str, tooltip: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("titleWindowButton")
        button.setFixedSize(48, 42)
        button.setToolTip(tooltip)
        return button

    def sync_maximized(self):
        screen = self.window.screen() or QGuiApplication.primaryScreen()
        available = screen.availableGeometry()
        margin = 2
        fill_rect = QRect(
            available.left() + margin,
            available.top() + margin,
            available.width() - margin * 2,
            available.height() - margin * 2,
        )
        filled = self.window.geometry() == fill_rect
        self.maximize_button.setText("❐" if filled else "□")
        self.maximize_button.setToolTip("Restore" if filled else "Maximize")

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window._toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)


class PlatformCard(QWidget):
    """Clickable platform card: image blurs and shows a text overlay on hover."""
    clicked = pyqtSignal()

    def __init__(self, image_path: str, text: str, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("platformCard")
        self.setMinimumSize(380, 380)

        layout = QGridLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            pixmap = QPixmap(400, 400)
            pixmap.fill(Qt.GlobalColor.darkGray)
        self.image_label.setPixmap(pixmap.scaled(
            400,
            400,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        layout.addWidget(self.image_label, 0, 0)

        self.text_label = QLabel(text)
        self.text_label.setObjectName("platformText")
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text_label.setWordWrap(True)
        self.text_label.setVisible(False)
        layout.addWidget(self.text_label, 0, 0, Qt.AlignmentFlag.AlignCenter)

    def enterEvent(self, event):
        effect = QGraphicsBlurEffect(self.image_label)
        effect.setBlurRadius(14)
        self.image_label.setGraphicsEffect(effect)
        self.text_label.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.image_label.setGraphicsEffect(None)
        self.text_label.setVisible(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class PlatformPicker(QDialog):
    """First-run gate: choose the console (Xbox 360 / PS3) before anything else."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected = ""
        self.setWindowTitle(f"{APP_TITLE} — Choose Platform")
        self.setModal(True)
        self.setMinimumSize(920, 560)
        self.setStyleSheet("""
            QWidget { background: #202124; color: #f1f3f4; font-size: 14px; }
            QLabel#platformTitle { font-size: 26px; font-weight: 700; }
            QLabel#platformSubtitle { color: #aeb4bd; }
            QLabel#platformText {
                background: rgba(20, 20, 24, 210); color: #ffffff;
                font-size: 19px; font-weight: 700; padding: 14px 22px;
                border-radius: 12px;
            }
            QWidget#platformCard {
                background: #2b2c2f; border: 1px solid #3c4043;
                border-radius: 18px;
            }
            QWidget#platformCard:hover { border: 1px solid #8ab4f8; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 36, 48, 36)
        layout.setSpacing(18)

        title = QLabel("Choose your platform")
        title.setObjectName("platformTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        subtitle = QLabel(
            "Each console stores its textures differently. "
            "Pick the one you are modding for."
        )
        subtitle.setObjectName("platformSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        row = QHBoxLayout()
        row.setSpacing(28)
        self.xbx_card = PlatformCard(
            str(resource_file("xbx.png")), "for rx2, xbox, Recomp", self
        )
        self.ps3_card = PlatformCard(
            str(resource_file("ps3.png")), "for psg, ps3, rpcs3", self
        )
        self.xbx_card.clicked.connect(lambda: self._choose("xbx"))
        self.ps3_card.clicked.connect(lambda: self._choose("ps3"))
        row.addWidget(self.xbx_card, 1)
        row.addWidget(self.ps3_card, 1)
        layout.addLayout(row, 1)

    def _choose(self, platform: str):
        self.selected = platform
        self.accept()


def platform_file() -> Path:
    return working_dir() / "platform.txt"


def get_saved_platform() -> str:
    try:
        value = platform_file().read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""
    return value if value in PLATFORMS else ""


def save_platform(platform: str) -> None:
    if platform not in PLATFORMS:
        return
    try:
        platform_file().write_text(platform, encoding="utf-8")
    except OSError:
        pass


def choose_platform(parent=None) -> str:
    """Return the chosen platform, showing the picker only when none is saved."""
    saved = get_saved_platform()
    if saved:
        return saved
    picker = PlatformPicker(parent)
    picker.exec()
    if picker.selected:
        save_platform(picker.selected)
    return picker.selected


def rx2_preview_image(path: Path, opaque: bool = False):
    """Decode the first decodable texture of an RX2 file into a PIL RGBA image.

    With opaque=True, any partially transparent pixel (alpha > 0) is raised
    to full opacity, mirroring PSGTx's "force visible pixels" cleanup.
    """
    try:
        from rx2_parser import parse_rx2
    except ImportError:
        return None
    try:
        rx2 = parse_rx2(path)
    except Exception:
        return None
    for texture in rx2.textures:
        try:
            image = texture.to_pil()
        except Exception:
            continue
        if opaque:
            image = _force_opaque(image)
        return image
    return None


def _force_opaque(image) -> "PIL Image":
    """Raise every visible pixel (alpha > 0) to full opacity."""
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    alpha = alpha.point(lambda value: 255 if value > 0 else 0)
    rgba.putalpha(alpha)
    return rgba


class ArchivePicker(QDialog):
    """Non-dismissable first-run gate used to select createacharacter.big."""
    def __init__(self, parent=None, platform: str = "ps3"):
        super().__init__(parent)
        self.selected_path: Path | None = None
        self.setWindowTitle("Select createacharacter.big")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setMinimumWidth(520)

        info = PLATFORMS[platform]
        layout = QVBoxLayout(self)
        title = QLabel("Select your createacharacter.big file")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        layout.addWidget(QLabel(
            "UTT needs the game archive before it can list or preview textures. "
            "The archive will be extracted into a local cache folder beside UTT.\n\n"
            f"Platform: {info['name']} — extracted files will be stored as "
            f".{info['extension']} files under cache\\{platform}."
        ))
        row = QHBoxLayout()
        self.path_label = QLabel("No archive selected")
        self.path_label.setWordWrap(True)
        self.pick_button = QPushButton("Browse…")
        self.pick_button.clicked.connect(self.pick_archive)
        row.addWidget(self.path_label, 1)
        row.addWidget(self.pick_button)
        layout.addLayout(row)
        self.continue_button = QPushButton("Extract and continue")
        self.continue_button.setEnabled(False)
        self.continue_button.clicked.connect(self.accept)
        layout.addWidget(self.continue_button)

    def pick_archive(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select createacharacter.big", "", "BIG archives (*.big);;All files (*.*)"
        )
        if not path:
            return
        self.selected_path = Path(path)
        self.path_label.setText(str(self.selected_path))
        self.continue_button.setEnabled(True)


class MainWindow(QMainWindow):
    def __init__(self, model_loader, platform: str = "ps3"):
        super().__init__()
        self.model_loader = model_loader
        self.platform = platform
        self.platform_info = PLATFORMS[platform]
        if self.platform == "xbx":
            self.accent, self.accent_hover = "#107c10", "#1e9e5a"
            self.accent_dark, self.accent_soft = "#2d7a46", "#6fbf73"
        else:
            self.accent, self.accent_hover = "#5b86e5", "#7299ec"
            self.accent_dark, self.accent_soft = "#536d9f", "#8ab4f8"
        self.psg_extension = self.platform_info["extension"]
        self.root_dir = resource_dir()
        self.user_dir = working_dir()
        self.assets_dir = self.root_dir / "assets"
        self.cache_dir = self.user_dir / "cache" / self.platform
        self.output_dir = (
            self.user_dir / "exports" / ("xbx" if self.platform == "xbx" else "psg")
        )
        self.converter = PSGConverter(str(self.assets_dir))
        self.archive_manager = ArchiveManager(self.assets_dir / "bigfile.exe")
        self.psg_index: dict[str, list[Path]] = {}
        self.current_image = None
        self.current_texture: PSGTx | None = None
        self.current_alias = ""
        self.current_model_path: Path | None = None
        self.input_image_path = ""
        self._thread: QThread | None = None
        self._threads: list[QThread] = []
        self._workers: list[Worker] = []
        self.unpack_progress: QProgressDialog | None = None
        self.repack_progress: QProgressDialog | None = None
        self._rpcs3 = None
        self._character_items: list = []
        self._character_from_save = False
        self._character_model_hex = ""
        self._character_texture: PSGTx | None = None

        self.setWindowTitle(APP_TITLE)
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )
        self.resize(1200, 760)
        self.setMinimumSize(900, 600)
        self._normal_geometry = QRect(120, 80, 1200, 760)
        self._build_ui()
        self._apply_style()
        self._load_catalog()
        self._load_saved_character_items()
        self._restore_cache_or_request_archive()

    def _build_ui(self):
        shell = QWidget()
        shell.setObjectName("appShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        self.title_bar = TitleBar(self)
        shell_layout.addWidget(self.title_bar)

        self.tabs = FullWidthTabWidget()
        self.tabs.tabBar().setExpanding(True)
        self.tabs.addTab(self._make_browser_tab(), "Browse")
        self.tabs.addTab(self._make_convert_tab(), "Convert")
        if self.platform == "ps3":
            self.tabs.addTab(self._make_character_tab(), "Character")
        shell_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(shell)

    def _make_browser_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QHBoxLayout()
        self.archive_status = QLabel(
            f"{self.platform_info['name']} ({self.platform_info['label']}) — archive not loaded"
        )
        self.archive_status.setObjectName("status")
        self.repack_button = QPushButton("Repack archive")
        self.repack_button.setEnabled(False)
        self.repack_button.clicked.connect(self._request_repack)
        reload_btn = QPushButton("Choose archive")
        reload_btn.clicked.connect(self._request_archive)
        header.addWidget(self.archive_status, 1)
        header.addWidget(self.repack_button)
        header.addWidget(reload_btn)
        layout.addLayout(header)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search category or hex string (for example: shoes or 0x0000…)")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(lambda _text: self._refresh_browser_tree())
        layout.addWidget(self.search_box)
        mode_row = QHBoxLayout()
        mode_row.addStretch(1)
        mode_row.addWidget(QLabel("Browse"))
        self.browser_mode = QComboBox()
        self.browser_mode.addItem("Textures", "textures")
        self.browser_mode.addItem("Models", "models")
        self.browser_mode.currentIndexChanged.connect(self._refresh_browser_tree)
        mode_row.addWidget(self.browser_mode)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self.texture_tree = QTreeWidget()
        self.texture_tree.setHeaderLabels(["Texture category / hex ID"])
        self.texture_tree.itemSelectionChanged.connect(self._on_texture_selected)
        self.texture_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.texture_tree.customContextMenuRequested.connect(
            lambda pos: self._show_cache_menu(self.texture_tree, pos)
        )

        self.model_tree = QTreeWidget()
        self.model_tree.setHeaderLabels(["Model folder / PSG file"])
        self.model_tree.setMinimumWidth(300)
        self.model_tree.itemSelectionChanged.connect(self._on_texture_selected)
        self.model_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.model_tree.customContextMenuRequested.connect(
            lambda pos: self._show_cache_menu(self.model_tree, pos)
        )

        self.preview_container = QWidget()
        self.preview_container.setObjectName("detailsPanel")
        preview_layout = QVBoxLayout(self.preview_container)
        preview_layout.setContentsMargins(20, 20, 20, 20)
        preview_layout.setSpacing(12)
        self.preview_label = QLabel("Select a texture to preview")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(430, 380)
        self.preview_label.setObjectName("preview")
        preview_layout.addWidget(self.preview_label, 1)
        self.selected_label = QLabel("No texture selected")
        preview_layout.addWidget(self.selected_label)
        self.force_opaque = QCheckBox("Force visible pixels to 255 opacity")
        self.force_opaque.setChecked(True)
        self.force_opaque.setToolTip(
            "Off preserves the alpha stored in the texture. On raises every "
            "visible pixel (alpha > 0) to full opacity."
        )
        self.force_opaque.toggled.connect(self._refresh_preview_alpha)
        preview_layout.addWidget(self.force_opaque)
        export_btn = QPushButton("Export preview…")
        self.texture_export_button = export_btn
        export_btn.clicked.connect(self._export_preview)
        preview_layout.addWidget(export_btn)

        self.browser_stack = QStackedWidget()

        self.texture_page = QWidget()
        texture_page_layout = QHBoxLayout(self.texture_page)
        texture_page_layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.texture_tree)
        self.splitter.addWidget(self.preview_container)
        texture_page_layout.addWidget(self.splitter, 1)
        self.browser_stack.addWidget(self.texture_page)

        self.model_page = QWidget()
        self.model_page.setObjectName("modelPanel")
        model_page_layout = QHBoxLayout(self.model_page)
        model_page_layout.setContentsMargins(0, 0, 0, 0)
        self.model_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.model_preview = ModelPreview()
        self.model_preview.export_requested.connect(self._export_model)
        self.model_splitter.addWidget(self.model_tree)
        self.model_splitter.addWidget(self.model_preview)
        self.model_splitter.setStretchFactor(0, 0)
        self.model_splitter.setStretchFactor(1, 1)
        self.model_splitter.setSizes([340, 900])
        model_page_layout.addWidget(self.model_splitter, 1)
        self.browser_stack.addWidget(self.model_page)

        layout.addWidget(self.browser_stack, 1)
        return page

    def _make_convert_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(14)

        mode_row = QHBoxLayout()
        mode_row.addStretch(1)
        mode_row.addWidget(QLabel("Convert"))
        self.convert_mode = QComboBox()
        self.convert_mode.addItem("Textures", "textures")
        self.convert_mode.addItem(
            f"GLB/glTF to {self.platform_info['label']}", "glb"
        )
        self.convert_mode.currentIndexChanged.connect(self._refresh_convert_mode)
        mode_row.addWidget(self.convert_mode)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self.convert_stack = QStackedWidget()
        self.convert_stack.addWidget(self._make_texture_convert_page())
        self.convert_stack.addWidget(self._make_glb_page())
        layout.addWidget(self.convert_stack, 1)

        return page

    def _refresh_convert_mode(self):
        mode = self.convert_mode.currentData()
        self.convert_stack.setCurrentIndex(1 if mode == "glb" else 0)

    def _make_texture_convert_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        heading = QLabel(f"Image to {self.platform_info['label']}")
        heading.setObjectName("convertTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        description = QLabel(
            f"Drop or choose an image, set its square resolution, then enter the "
            f"{self.platform_info['label']} hex name."
        )
        description.setObjectName("convertDescription")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(description)

        self.image_drop_zone = ImageDropZone()
        self.image_drop_zone.setObjectName("imageDropZone")
        self.image_drop_zone.clicked.connect(self._choose_image)
        self.image_drop_zone.imageDropped.connect(self._set_input_image)
        layout.addWidget(self.image_drop_zone, 1)

        controls = QFrame()
        controls.setObjectName("card")
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(20, 16, 20, 16)
        controls_layout.setSpacing(14)

        file_row = QHBoxLayout()
        file_row.setSpacing(14)
        image_btn = QPushButton("Select image")
        image_btn.clicked.connect(self._choose_image)
        file_row.addWidget(image_btn)
        self.image_path_label = QLabel("No image selected")
        self.image_path_label.setObjectName("selectedImagePath")
        self.image_path_label.setWordWrap(True)
        file_row.addWidget(self.image_path_label, 1)

        alias_column = QVBoxLayout()
        alias_column.setSpacing(4)
        alias_column.addWidget(QLabel("18-character hex name"))
        self.alias_input = QLineEdit()
        self.alias_input.setObjectName("aliasInput")
        self.alias_input.setPlaceholderText("0x0000000000000000")
        self.alias_input.setMaxLength(HEX_LENGTH)
        self.alias_input.setValidator(QRegularExpressionValidator(
            QRegularExpression(r"0x[0-9A-Fa-f]{0,16}"), self.alias_input
        ))
        self.alias_input.textChanged.connect(self._update_convert_ready)
        alias_column.addWidget(self.alias_input)
        file_row.addLayout(alias_column)

        resolution_column = QVBoxLayout()
        resolution_column.setSpacing(4)
        resolution_column.addWidget(QLabel("Resolution"))
        self.resolution = QSpinBox()
        self.resolution.setRange(128, 4096)
        self.resolution.setKeyboardTracking(False)
        self.resolution.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resolution.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.resolution.setValue(512)
        if self.platform == "xbx":
            # The built-in encoder handles the X360 tiled layout itself and
            # supports up to 4096x4096 (the RX2 size field stores width-1 /
            # height-1 as 13-bit values).
            self.resolution.setMaximum(4096)
            self.resolution.setValue(512)
        resolution_row = QHBoxLayout()
        self.resolution_down = QPushButton("↓")
        self.resolution_down.setObjectName("resolutionButton")
        self.resolution_down.setToolTip("Decrease resolution")
        self.resolution_down.setFixedWidth(46)
        self.resolution_down.clicked.connect(self._decrease_resolution)
        resolution_row.addWidget(self.resolution_down)
        resolution_row.addWidget(self.resolution)
        self.resolution_up = QPushButton("↑")
        self.resolution_up.setObjectName("resolutionButton")
        self.resolution_up.setToolTip("Increase resolution")
        self.resolution_up.setFixedWidth(46)
        self.resolution_up.clicked.connect(self._increase_resolution)
        resolution_row.addWidget(self.resolution_up)
        resolution_column.addLayout(resolution_row)
        file_row.addLayout(resolution_column)
        controls_layout.addLayout(file_row)

        opacity_row = QHBoxLayout()
        opacity_row.setSpacing(12)
        opacity_row.addWidget(QLabel("Opacity"))
        self.convert_opacity = QSlider(Qt.Orientation.Horizontal)
        self.convert_opacity.setRange(0, 100)
        self.convert_opacity.setValue(100)
        self.convert_opacity.valueChanged.connect(
            lambda value: self.convert_opacity_text.setText(f"{value}%")
        )
        opacity_row.addWidget(self.convert_opacity, 1)
        self.convert_opacity_text = QLabel("100%")
        self.convert_opacity_text.setObjectName("convertDescription")
        opacity_row.addWidget(self.convert_opacity_text)
        controls_layout.addLayout(opacity_row)

        output_row = QHBoxLayout()
        output_row.setSpacing(12)
        output_row.addWidget(QLabel("Output folder"))
        self.convert_output_input = QLineEdit(str(self.output_dir))
        self.convert_output_input.setToolTip(
            f"{self.platform_info['label']} files will be saved into this folder. "
            "Defaults to the exports folder."
        )
        output_row.addWidget(self.convert_output_input, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._choose_output_folder)
        output_row.addWidget(browse_btn)
        controls_layout.addLayout(output_row)

        self.convert_button = QPushButton(
            f"Convert image to {self.platform_info['label']}"
        )
        self.convert_button.setObjectName("convertButton")
        self.convert_button.setEnabled(False)
        self.convert_button.clicked.connect(self._convert_image)
        controls_layout.addWidget(self.convert_button)
        layout.addWidget(controls)

        return page

    def _make_glb_page(self) -> QWidget:
        """Convert a GLB/glTF model into a game-ready {psg,rx2} mesh by
        patching a donor template from the game."""
        label = self.platform_info["label"]
        extension = self.platform_info["extension"]
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        heading = QLabel(f"GLB/glTF to {label}")
        heading.setObjectName("convertTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        description = QLabel(
            f"Choose a GLB or glTF model and a donor {extension} template, "
            f"then convert it to a game-ready {label} file."
        )
        description.setObjectName("convertDescription")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(description)

        controls = QFrame()
        controls.setObjectName("card")
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(20, 16, 20, 16)
        controls_layout.setSpacing(14)

        glb_row = QHBoxLayout()
        glb_row.setSpacing(14)
        glb_btn = QPushButton("Select GLB/glTF…")
        glb_btn.clicked.connect(self._choose_glb_file)
        glb_row.addWidget(glb_btn)
        self.glb_file_label = QLabel("No model selected")
        self.glb_file_label.setObjectName("selectedImagePath")
        self.glb_file_label.setWordWrap(True)
        glb_row.addWidget(self.glb_file_label, 1)

        donor_row = QHBoxLayout()
        donor_row.setSpacing(14)
        donor_btn = QPushButton(f"Select donor {label} template…")
        donor_btn.clicked.connect(self._choose_glb_donor)
        donor_row.addWidget(donor_btn)
        self.glb_donor_label = QLabel(f"No donor {label} selected")
        self.glb_donor_label.setObjectName("selectedImagePath")
        self.glb_donor_label.setWordWrap(True)
        donor_row.addWidget(self.glb_donor_label, 1)
        controls_layout.addLayout(glb_row)
        controls_layout.addLayout(donor_row)

        output_row = QHBoxLayout()
        output_row.setSpacing(12)
        output_row.addWidget(QLabel("Output file"))
        self.glb_output_input = QLineEdit(str(self.output_dir))
        self.glb_output_input.setToolTip(
            f"The converted {label} file will be written to this path."
        )
        output_row.addWidget(self.glb_output_input, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._choose_glb_output)
        output_row.addWidget(browse_btn)
        controls_layout.addLayout(output_row)

        scale_row = QHBoxLayout()
        scale_row.setSpacing(12)
        scale_row.addWidget(QLabel("Vertex scale"))
        self.glb_scale = QDoubleSpinBox()
        self.glb_scale.setRange(1.0, 100000.0)
        self.glb_scale.setDecimals(1)
        self.glb_scale.setValue(256.0)
        self.glb_scale.setSingleStep(1.0)
        self.glb_scale.setToolTip(
            "Multiplier applied to vertex XYZ when packing to the game's "
            "fixed-point format (256.0 is the default scale)."
        )
        scale_row.addWidget(self.glb_scale, 1)
        controls_layout.addLayout(scale_row)

        self.glb_convert_button = QPushButton(f"Convert GLB to {label}")
        self.glb_convert_button.setObjectName("convertButton")
        self.glb_convert_button.setEnabled(False)
        self.glb_convert_button.clicked.connect(self._convert_glb)
        controls_layout.addWidget(self.glb_convert_button)
        layout.addWidget(controls)

        self.glb_log = QPlainTextEdit()
        self.glb_log.setObjectName("glbLog")
        self.glb_log.setReadOnly(True)
        self.glb_log.setPlaceholderText("Conversion log will appear here…")
        layout.addWidget(self.glb_log, 1)

        return page

    def _make_character_tab(self) -> QWidget:
        """Live Skate 3 character items: read the recipe from RPCS3 memory and
        preview the worn models/textures from the local cache."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.rpcs3_status = QLabel("RPCS3 not attached")
        self.rpcs3_status.setObjectName("status")
        header.addWidget(self.rpcs3_status, 1)
        attach_btn = QPushButton("Attach to RPCS3")
        attach_btn.clicked.connect(self._attach_rpcs3)
        header.addWidget(attach_btn)
        self.character_scan_button = QPushButton("Scan for Models and Textures")
        self.character_scan_button.clicked.connect(self._scan_character)
        header.addWidget(self.character_scan_button)
        self.character_open_button = QPushButton("Open Output Folder")
        self.character_open_button.setEnabled(False)
        self.character_open_button.clicked.connect(self._open_character_output)
        header.addWidget(self.character_open_button)
        layout.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.character_tree = QTreeWidget()
        self.character_tree.setHeaderLabels(["Character part / texture"])
        self.character_tree.setMinimumWidth(340)
        self.character_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.character_tree.customContextMenuRequested.connect(
            lambda pos: self._show_cache_menu(self.character_tree, pos)
        )
        self.character_tree.itemSelectionChanged.connect(self._on_character_item_selected)
        splitter.addWidget(self.character_tree)

        self.character_stack = QStackedWidget()

        self.character_model_preview = ModelPreview()
        self.character_model_preview.export_requested.connect(self._export_model)
        self.character_stack.addWidget(self.character_model_preview)

        self.character_texture_page = QWidget()
        self.character_texture_page.setObjectName("detailsPanel")
        texture_layout = QVBoxLayout(self.character_texture_page)
        texture_layout.setContentsMargins(20, 20, 20, 20)
        texture_layout.setSpacing(12)
        texture_top = QHBoxLayout()
        back_btn = QPushButton("View 3D model")
        back_btn.clicked.connect(self._show_character_model)
        texture_top.addWidget(back_btn)
        texture_top.addStretch(1)
        texture_layout.addLayout(texture_top)
        self.character_texture_label = QLabel("Select a texture to preview")
        self.character_texture_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.character_texture_label.setMinimumSize(430, 380)
        self.character_texture_label.setObjectName("preview")
        texture_layout.addWidget(self.character_texture_label, 1)
        self.character_force_opaque = QCheckBox("Force visible pixels to 255 opacity")
        self.character_force_opaque.setChecked(True)
        self.character_force_opaque.setToolTip("Off preserves the alpha stored in the PSG. On applies PSGTx's alpha cleanup.")
        self.character_force_opaque.toggled.connect(self._refresh_character_texture_preview)
        texture_layout.addWidget(self.character_force_opaque)
        self.character_selected_label = QLabel("No texture selected")
        texture_layout.addWidget(self.character_selected_label)
        self.character_stack.addWidget(self.character_texture_page)

        splitter.addWidget(self.character_stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 880])
        layout.addWidget(splitter, 1)

        placeholder = QTreeWidgetItem(["Scan the character to list worn items"])
        placeholder.setData(0, Qt.ItemDataRole.UserRole, None)
        self.character_tree.addTopLevelItem(placeholder)
        return page

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget { background: #202124; color: #f1f3f4; font-size: 14px; }
            QFrame#titleBar { background: #2b2c2f; border-bottom: 1px solid #3c4043; }
            QFrame#titleBar QLabel { background: transparent; }
            QLabel#windowTitle { color: #d9dce1; font-size: 13px; font-weight: 600; }
            QPushButton#creditsButton, QPushButton#settingsButton { background: transparent; color: #aeb4bd; border-radius: 8px; padding: 6px 11px; font-weight: 500; }
            QPushButton#creditsButton:hover, QPushButton#settingsButton:hover { background: #3c4043; color: #ffffff; }
            QPushButton#titleWindowButton, QPushButton#titleCloseButton { background: transparent; border-radius: 0; padding: 0; font-size: 17px; font-weight: 400; }
            QPushButton#titleWindowButton:hover { background: #3c4043; }
            QPushButton#titleCloseButton:hover { background: #c42b1c; }
            QTabWidget::pane { border: 0; border-top: 1px solid #3c4043; }
            QTabBar::tab { background: #303134; color: #c7ccd4; padding: 9px 26px; margin: 7px 5px; border: 1px solid #4a4d52; border-radius: 18px; }
            QTabBar::tab:hover { background: #3c4043; color: #ffffff; }
            QTabBar::tab:selected { background: @accent; color: #ffffff; border: 1px solid @accent; }
            QPushButton { background: @accent; border: 0; border-radius: 16px; padding: 9px 16px; font-weight: 600; }
            QPushButton:hover { background: @accent_hover; }
            QPushButton:disabled { background: #45474b; color: #9aa0a6; }
            QLineEdit, QTreeWidget, QSpinBox, QDoubleSpinBox { background: #2b2c2f; border: 1px solid #4a4d52; border-radius: 9px; padding: 7px; }
            QTreeWidget::item { padding: 5px; border-radius: 6px; }
            QTreeWidget::item:selected { background: @accent_dark; }
            QLabel#preview { background: #18191b; border: 1px dashed #5f6368; border-radius: 16px; }
            QLabel#dialogTitle { font-size: 20px; font-weight: 700; }
            QLabel#status { color: #a9c7fa; }
            QWidget#detailsPanel { background: #2b2c2f; border-radius: 16px; }
            QFrame#card { background: #292a2d; border: 1px solid #3c4043; border-radius: 18px; }
            QLabel#convertTitle { font-size: 26px; font-weight: 700; }
            QLabel#convertDescription, QLabel#selectedImagePath, QLabel#modelDetails { color: #aeb4bd; }
            QLabel#imageDropZone { background: #18191b; border: 2px dashed #5f6368; border-radius: 16px; color: #aeb4bd; font-size: 17px; }
            QLabel#imageDropZone:hover { border-color: @accent_soft; color: #f1f3f4; }
            QLabel#modelTitle { font-size: 18px; font-weight: 700; }
            QLineEdit#aliasInput { font-family: Consolas; font-size: 16px; padding: 10px; }
            QPushButton#resolutionButton { font-size: 21px; padding: 5px 10px; }
            QPushButton#convertButton { font-size: 16px; padding: 12px 18px; }
            QPlainTextEdit#glbLog { background: #18191b; border: 1px solid #3c4043; border-radius: 12px; font-family: Consolas; font-size: 12px; padding: 8px; }
            QSlider::groove:horizontal { height: 7px; background: #4a4d52; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: @accent_soft; border-radius: 3px; }
            QSlider::handle:horizontal { width: 18px; margin: -6px 0; background: #d2e3fc; border-radius: 9px; }
        """.replace("@accent", self.accent)
            .replace("@accent_hover", self.accent_hover)
            .replace("@accent_dark", self.accent_dark)
            .replace("@accent_soft", self.accent_soft))

    def _load_catalog(self):
        catalog_candidates = [
            self.root_dir / "psg_list.json",
            Path(__file__).resolve().parent / "psg_list.json",
            Path(__file__).resolve().parent / "build" / "psg_list.json",
        ]
        catalog_path = None
        for candidate in catalog_candidates:
            if candidate.is_file():
                catalog_path = candidate
                break
        if catalog_path is None:
            raise FileNotFoundError(f"Could not find psg_list.json in {self.root_dir}")

        with catalog_path.open("r", encoding="utf-8") as file:
            catalog = json.load(file)
        search = self.search_box.text().strip().lower()
        self.texture_tree.clear()
        # Before an archive is chosen we show the full reference catalog. Once
        # a cache exists, IDs without a matching PSG are omitted entirely.
        self._add_catalog_nodes(
            self.texture_tree, catalog, bool(self.psg_index), search
        )
        if search:
            self._add_cache_search_results(search)

    def _add_cache_search_results(self, search: str) -> None:
        """Show cached files matching the search that are not in the JSON
        catalog, so anything inside the cache can be found by its hex ID."""
        shown = set()
        root = self.texture_tree.invisibleRootItem()

        def collect(item):
            for i in range(item.childCount()):
                child = item.child(i)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(data, str):
                    shown.add(data)
                collect(child)

        collect(root)
        matches = sorted(
            alias
            for alias, paths in self.psg_index.items()
            if search in alias and alias not in shown
        )
        if not matches:
            return
        node = QTreeWidgetItem(self.texture_tree, [
            f"Other cache files ({len(matches)})"
        ])
        for alias in matches:
            leaf = QTreeWidgetItem(node, [alias])
            leaf.setData(0, Qt.ItemDataRole.UserRole, alias)

    def _refresh_browser_tree(self):
        mode = self.browser_mode.currentData()
        if mode is None:
            mode = self.browser_mode.currentText().strip().lower()
        if mode == "models":
            self.browser_stack.setCurrentWidget(self.model_page)
            self.model_tree.setHeaderLabels([
                f"Model folder / {self.platform_info['label']} file"
            ])
            self._load_models_tree()
            self.preview_label.setVisible(False)
            self.texture_export_button.setVisible(False)
            self.force_opaque.setVisible(False)
            self.selected_label.setVisible(False)
        else:
            self.browser_stack.setCurrentWidget(self.texture_page)
            self.texture_tree.setHeaderLabels(["Texture category / hex ID"])
            self._load_catalog()
            self.preview_label.setVisible(True)
            self.texture_export_button.setVisible(True)
            self.force_opaque.setVisible(True)
            self.selected_label.setVisible(True)
            self.selected_label.setText("No texture selected")

    def _load_models_tree(self):
        """Build the model categories directly from the cached cas_db folders."""
        self.model_tree.clear()
        models_root = self.cache_dir / "data" / "content" / "createacharacter" / "model" / "cas_db"
        if not models_root.is_dir():
            self.model_tree.addTopLevelItem(QTreeWidgetItem([
                "No cached models found — unpack createacharacter.big first."
            ]))
            return
        found = self._add_model_nodes(
            self.model_tree.invisibleRootItem(), models_root, self.search_box.text().strip().lower()
        )
        if not found:
            self.model_tree.addTopLevelItem(QTreeWidgetItem(["No matching model files found."]))

    def _add_model_nodes(self, parent: QTreeWidgetItem, folder: Path, search: str,
                         parent_matches: bool = False) -> bool:
        """Add nested cas_db folders and their PSG model files."""
        added = False
        try:
            children = sorted(folder.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower()))
        except OSError:
            return False
        for child in children:
            if child.is_dir():
                node = QTreeWidgetItem(parent, [child.name])
                folder_matches = parent_matches or (bool(search) and search in child.name.lower())
                if self._add_model_nodes(node, child, search, folder_matches):
                    added = True
                else:
                    parent.removeChild(node)
            elif child.suffix.lower() == "." + self.psg_extension and (
                parent_matches or not search or search in child.name.lower()
            ):
                leaf = QTreeWidgetItem(parent, [child.name])
                leaf.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    ("psg_model", str(child)),
                )
                added = True
        return added

    def _add_catalog_nodes(self, parent, value, available_only: bool, search: str,
                           parent_matches: bool = False) -> bool:
        container = parent.invisibleRootItem() if isinstance(parent, QTreeWidget) else parent
        has_children = False
        for name, child in value.items():
            item = QTreeWidgetItem(container, [name])
            show_all_children = parent_matches or (bool(search) and search in name.lower())
            if isinstance(child, dict):
                # When a folder holds a list with the same name as itself
                # (e.g. Decks -> {"Decks": [...], "Plain Color Boards": [...]}),
                # inline that list into the folder instead of nesting a folder
                # with the identical name.
                same_name_list = child.get(name)
                if isinstance(same_name_list, list):
                    if self._add_alias_leaves(
                        item, same_name_list, available_only, search, show_all_children
                    ):
                        has_children = True
                    rest = {key: value2 for key, value2 in child.items() if key != name}
                    if rest and self._add_catalog_nodes(
                        item, rest, available_only, search, show_all_children
                    ):
                        has_children = True
                    if item.childCount() == 0:
                        container.removeChild(item)
                else:
                    if self._add_catalog_nodes(item, child, available_only, search, show_all_children):
                        has_children = True
                    else:
                        container.removeChild(item)
            else:
                if self._add_alias_leaves(item, child, available_only, search, show_all_children):
                    has_children = True
                if item.childCount() == 0:
                    container.removeChild(item)
        return has_children

    def _add_alias_leaves(self, item, aliases, available_only: bool, search: str,
                          show_all_children: bool) -> bool:
        added = False
        for alias in aliases:
            if available_only and alias.lower() not in self.psg_index:
                continue
            if search and not show_all_children and search not in alias.lower():
                continue
            leaf = QTreeWidgetItem(item, [alias])
            leaf.setData(0, Qt.ItemDataRole.UserRole, alias.lower())
            added = True
        return added

    def _request_archive(self):
        picker = ArchivePicker(self, self.platform)
        if picker.exec() == QDialog.DialogCode.Accepted and picker.selected_path:
            self._extract_archive(picker.selected_path)

    def _request_repack(self):
        data_path = self.cache_dir / "data"
        if not data_path.is_dir():
            self._show_error("Extract createacharacter.big before repacking it.")
            return

        target_path = data_path / "createacharacter.big"
        message = (
            "Repack every file under cache\\data into:\n\n"
            f"{target_path}\n\n"
            "Files are packed without compression for speed."
        )
        if target_path.exists():
            message += "\n\nThe existing repacked archive will be replaced."
        answer = QMessageBox.question(
            self,
            "Repack createacharacter.big?",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.repack_button.setEnabled(False)
        self.archive_status.setText("Repacking createacharacter.big…")
        self.repack_progress = QProgressDialog(
            "Packing cache\\data, please wait…", None, 0, 0, self
        )
        self.repack_progress.setWindowTitle(APP_TITLE)
        self.repack_progress.setWindowModality(
            Qt.WindowModality.ApplicationModal
        )
        self.repack_progress.setCancelButton(None)
        self.repack_progress.setMinimumDuration(0)
        self.repack_progress.setAutoClose(False)
        self.repack_progress.show()
        self._start_worker(
            lambda: self.archive_manager.repack(self.cache_dir),
            self._repack_finished,
            self._repack_failed,
        )

    def _repack_finished(self, result):
        if self.repack_progress:
            self.repack_progress.close()
            self.repack_progress = None
        self.repack_button.setEnabled(True)
        size_mb = result.size / (1024 * 1024)
        self.archive_status.setText(
            f"Repacked {result.file_count:,} files — {size_mb:,.1f} MB"
        )
        QMessageBox.information(
            self,
            APP_TITLE,
            f"createacharacter.big created:\n{result.path}\n\n"
            f"{result.file_count:,} files • {size_mb:,.1f} MB",
        )

    def _repack_failed(self, details: str):
        if self.repack_progress:
            self.repack_progress.close()
            self.repack_progress = None
        self.repack_button.setEnabled(self.cache_dir.is_dir())
        self.archive_status.setText("Repack failed")
        self._show_error(details)

    def _restore_cache_or_request_archive(self):
        """Use the persistent cache beside the EXE whenever it is available."""
        if self.cache_dir.is_dir() and any(
            self.cache_dir.rglob(f"*.{self.psg_extension}")
        ):
            self.archive_status.setText("Loading existing texture cache…")
            self._start_worker(self._build_psg_index, self._archive_loaded)
        else:
            self._request_archive()

    def _extract_archive(self, archive: Path):
        if self.cache_dir.is_dir() and any(
            path.is_file() for path in self.cache_dir.rglob("*")
        ):
            answer = QMessageBox.question(
                self, "Replace cache?", "A cache already exists. Replace it with this archive's contents?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._index_cache()
                return
        self.archive_status.setText("Extracting archive…")
        self.repack_button.setEnabled(False)
        self.unpack_progress = QProgressDialog("Unpacking, please wait…", None, 0, 0, self)
        self.unpack_progress.setWindowTitle(APP_TITLE)
        self.unpack_progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.unpack_progress.setCancelButton(None)
        self.unpack_progress.setMinimumDuration(0)
        self.unpack_progress.setAutoClose(False)
        self.unpack_progress.show()

        def extract():
            if self.cache_dir.exists():
                shutil.rmtree(self.cache_dir)
            self.cache_dir.mkdir(parents=True)
            tool = self.assets_dir / "bigfile.exe"
            subprocess.run(
                [str(tool), str(archive), "-x"], cwd=self.cache_dir, check=True,
                capture_output=True, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return self._build_psg_index()
        self._start_worker(extract, self._archive_loaded)

    def _build_psg_index(self) -> dict[str, list[Path]]:
        index: dict[str, list[Path]] = {}
        for file in self.cache_dir.rglob(f"*.{self.psg_extension}"):
            index.setdefault(file.stem.lower(), []).append(file)
        return index

    def _index_cache(self):
        self.psg_index = self._build_psg_index() if self.cache_dir.exists() else {}
        self.archive_status.setText(
            f"Cache ready — {sum(map(len, self.psg_index.values()))} "
            f"{self.platform_info['label']} files found"
        )
        self.repack_button.setEnabled(bool(self.psg_index))

    def _archive_loaded(self, index):
        self.psg_index = index
        self._refresh_browser_tree()
        self.archive_status.setText(
            f"Cache ready — {sum(map(len, index.values()))} "
            f"{self.platform_info['label']} files found"
        )
        self.repack_button.setEnabled(bool(index))
        if self.unpack_progress:
            self.unpack_progress.close()
            self.unpack_progress = None
        if self._character_from_save and self._character_items:
            self._populate_character_tree(
                self._character_items, self._character_output_folder
            )

    def _on_texture_selected(self):
        mode = self.browser_mode.currentData()
        if mode is None:
            mode = self.browser_mode.currentText().strip().lower()
        tree = self.model_tree if mode == "models" else self.texture_tree
        items = tree.selectedItems()
        if not items:
            return
        alias = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not alias:
            return
        if isinstance(alias, tuple) and alias[0] == "psg_model":
            path = Path(alias[1])
            self.current_model_path = path
            self.model_preview.show_loading(path)
            self._start_worker(
                lambda: (path, self.model_loader(path)),
                self._model_loaded,
            )
            return
        if isinstance(alias, str):
            matches = self.psg_index.get(alias, [])
        else:
            return
        self.current_alias = alias
        self.selected_label.setText(alias)
        if not matches:
            self.current_image = None
            return
        self.preview_label.setText("Loading preview…")
        path = matches[0]
        if path.suffix.lower() == ".rx2":
            self._rx2_path = path
            self._start_worker(
                lambda: rx2_preview_image(path, self.force_opaque.isChecked()),
                self._rx2_preview_loaded,
            )
            return
        self._rx2_path = None
        self._start_worker(lambda: PSGTx(str(path)), self._preview_loaded)

    def _model_loaded(self, result):
        path, model = result
        if path == self.current_model_path:
            self.model_preview.set_model(path, model)

    def _preview_loaded(self, texture: PSGTx):
        self.current_texture = texture
        self.current_image = texture.get_tx_image(self.force_opaque.isChecked())
        pixmap = QPixmap.fromImage(ImageQt(self.current_image.convert("RGBA")))
        self.preview_label.setPixmap(pixmap.scaled(
            self.preview_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _rx2_preview_loaded(self, image):
        if image is None:
            self.current_image = None
            self.current_texture = None
            self.preview_label.setText("This RX2 contains no decodable textures.")
            return
        self.current_image = image
        self.current_texture = None
        pixmap = QPixmap.fromImage(ImageQt(image.convert("RGBA")))
        self.preview_label.setPixmap(pixmap.scaled(
            self.preview_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _refresh_preview_alpha(self):
        if self.current_texture is not None:
            self._preview_loaded(self.current_texture)
            return
        rx2_path = getattr(self, "_rx2_path", None)
        if rx2_path is not None:
            self.preview_label.setText("Loading preview…")
            self._start_worker(
                lambda: rx2_preview_image(rx2_path, self.force_opaque.isChecked()),
                self._rx2_preview_loaded,
            )

    def _export_preview(self):
        if self.current_image is None:
            QMessageBox.information(self, APP_TITLE, "Load a preview before exporting.")
            return
        default = self.output_dir / f"{self.current_alias}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export texture", str(default),
            "PNG (*.png);;JPEG (*.jpg *.jpeg);;BMP (*.bmp);;TIFF (*.tiff);;All files (*.*)",
        )
        if not path:
            return
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            if self.current_texture is not None:
                self.current_texture.export_tx(path, self.force_opaque.isChecked())
            else:
                image = self.current_image
                if path.lower().endswith((".jpg", ".jpeg")):
                    image = image.convert("RGB")
                image.save(path)
            QMessageBox.information(self, APP_TITLE, f"Exported:\n{path}")
        except Exception as exc:
            self._show_error(str(exc))

    def _export_model(self, model, source_path):
        if model is None:
            return
        default_name = source_path.stem if source_path else "model"
        default = self.output_dir / f"{default_name}.glb"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export model as glTF Binary", str(default),
            "glTF Binary (*.glb);;All files (*.*)",
        )
        if not path:
            return
        self._start_worker(
            lambda: export_gltf(model, path),
            lambda result: QMessageBox.information(self, APP_TITLE, f"Model exported:\n{result}"),
        )

    def _find_psg(self, hex_name) -> Path | None:
        key = str(hex_name).lower()
        candidates = (key, key[2:]) if key.startswith("0x") else ("0x" + key, key)
        for candidate in candidates:
            matches = self.psg_index.get(candidate)
            if matches:
                return matches[0]
        return None

    def _character_item_cached(self, item: dict) -> bool:
        model = item.get("model")
        if model and self._find_psg(model):
            return True
        return any(self._find_psg(hex_name) for hex_name in item.get("textures", {}).values())

    def _attach_rpcs3(self):
        try:
            self._rpcs3 = recipe.find_rpcs3()
        except recipe.RPCS3NotFoundError as exc:
            self.rpcs3_status.setText("RPCS3 not attached")
            self._show_error(str(exc))
            return
        self.rpcs3_status.setText(
            f"Attached to RPCS3 — process {self._rpcs3.process_id}"
        )

    def _scan_character(self):
        self.character_scan_button.setEnabled(False)
        self.rpcs3_status.setText("Scanning character…")
        self._start_worker(
            lambda: recipe.scan_and_save(recipe.get_base_path() / "output"),
            self._character_scan_finished,
            self._character_scan_failed,
        )

    def _character_scan_finished(self, result):
        self._populate_character_tree(result["items"], result["output_folder"])
        self.character_scan_button.setEnabled(True)
        self.rpcs3_status.setText(
            f"Found {len(self._character_items)} items — saved to {result['txt_path']}"
        )

    def _populate_character_tree(self, items, output_folder):
        """Rebuild the character tree from scanned items (kept collapsed)."""
        self._character_items = items
        self._character_output_folder = output_folder
        self.character_tree.clear()
        for item in items:
            part = QTreeWidgetItem([item["name"]])
            part.setData(0, Qt.ItemDataRole.UserRole, ("part", item))
            if not self._character_item_cached(item):
                part.setText(0, f"{item['name']}  (not in cache)")
            model_hex = item.get("model")
            if model_hex:
                path = self._find_psg(model_hex)
                if path is not None:
                    model = QTreeWidgetItem([f"model {model_hex}"])
                    model.setData(0, Qt.ItemDataRole.UserRole, ("model", model_hex, str(path)))
                else:
                    model = QTreeWidgetItem([f"model {model_hex} — not found"])
                    model.setData(0, Qt.ItemDataRole.UserRole, ("model", model_hex, None))
            else:
                model = QTreeWidgetItem(["model not found"])
                model.setData(0, Qt.ItemDataRole.UserRole, ("model", None, None))
            part.addChild(model)
            texture_hex = item.get("textures", {}).get("diffuse")
            if texture_hex:
                path = self._find_psg(texture_hex)
                if path is not None:
                    texture = QTreeWidgetItem([f"texture {texture_hex}"])
                    texture.setData(0, Qt.ItemDataRole.UserRole, ("texture", texture_hex, str(path)))
                else:
                    texture = QTreeWidgetItem([f"texture {texture_hex} — not found"])
                    texture.setData(0, Qt.ItemDataRole.UserRole, ("texture", texture_hex, None))
            else:
                texture = QTreeWidgetItem(["texture not found"])
                texture.setData(0, Qt.ItemDataRole.UserRole, ("texture", None, None))
            part.addChild(texture)
            self.character_tree.addTopLevelItem(part)
        self.character_open_button.setEnabled(True)

    def _load_saved_character_items(self):
        """Reuse the previous scan when output\\current_items.txt exists."""
        if self.platform != "ps3":
            return
        txt_path = recipe.get_base_path() / "output" / "current_items.txt"
        if not txt_path.is_file():
            return
        try:
            items = recipe.read_items_txt(txt_path)
        except OSError:
            return
        if not items:
            return
        self._character_from_save = True
        self._populate_character_tree(items, txt_path.parent)
        self.rpcs3_status.setText(
            f"Loaded {len(items)} items from current_items.txt — rescan to refresh"
        )

    def _character_scan_failed(self, details: str):
        self.character_scan_button.setEnabled(True)
        self.rpcs3_status.setText("Scan failed")
        self._show_error(details)

    def _on_character_item_selected(self):
        selected = self.character_tree.selectedItems()
        if not selected:
            return
        node = selected[0]
        data = node.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        kind = data[0]
        if kind == "part":
            return
        if kind == "texture":
            hex_name, path = data[1], data[2]
            if path is None:
                self._character_texture = None
                self.character_texture_label.setPixmap(QPixmap())
                self.character_texture_label.setText(
                    "Texture not found in cache — unpack createacharacter.big to preview."
                )
                self.character_selected_label.setText(hex_name or "")
                return
            self.character_texture_label.setText("Loading preview…")
            self._start_worker(
                lambda: (hex_name, PSGTx(str(path))),
                self._character_texture_loaded,
            )
            return
        model_hex, path = data[1], data[2]
        self._character_texture = None
        self.character_texture_label.setPixmap(QPixmap())
        self.character_texture_label.setText("Select a texture to preview")
        self.character_selected_label.setText("No texture selected")
        if path is None:
            self._character_model_hex = ""
            self.character_model_preview.clear(
                "Model not found in cache — unpack createacharacter.big to preview."
            )
            return
        self._character_model_hex = model_hex
        model_path = Path(path)
        self._show_character_model()
        self.character_model_preview.show_loading(model_path)
        self._start_worker(
            lambda: (model_hex, model_path, self.model_loader(model_path)),
            self._character_model_loaded,
        )

    def _character_model_loaded(self, result):
        hex_name, path, model = result
        if hex_name == self._character_model_hex:
            self.character_model_preview.set_model(path, model)

    def _show_character_model(self):
        self.character_stack.setCurrentWidget(self.character_model_preview)

    def _character_texture_loaded(self, result):
        hex_name, texture = result
        selected = self.character_tree.selectedItems()
        if not selected:
            return
        node = selected[0]
        data = node.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "texture" or data[1] != hex_name:
            return
        self._character_texture = texture
        self.character_selected_label.setText(hex_name)
        self._refresh_character_texture_preview()
        self.character_stack.setCurrentWidget(self.character_texture_page)

    def _refresh_character_texture_preview(self):
        if self._character_texture is None:
            return
        image = self._character_texture.get_tx_image(
            self.character_force_opaque.isChecked()
        )
        pixmap = QPixmap.fromImage(ImageQt(image.convert("RGBA")))
        self.character_texture_label.setPixmap(pixmap.scaled(
            self.character_texture_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _open_character_output(self):
        folder = recipe.get_base_path() / "output"
        if not folder.is_dir():
            return
        if sys.platform == "win32":
            os.startfile(str(folder))
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    def _show_cache_menu(self, tree, pos):
        """Right-click menu: copy the hex name and/or reveal the PSG in Explorer."""
        node = tree.itemAt(pos)
        if node is None:
            return
        data = node.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        hex_name = None
        file_path = None
        if isinstance(data, str):
            hex_name = data
            matches = self.psg_index.get(data, [])
            if matches:
                file_path = matches[0]
        else:
            kind = data[0]
            if kind == "part":
                return
            if kind == "psg_model":
                path = Path(data[1])
                file_path = path
                hex_name = path.stem if path.stem.lower().startswith("0x") else "0x" + path.stem
            elif kind == "model":
                hex_name = data[1]
                if data[2]:
                    file_path = Path(data[2])
            elif kind == "texture":
                hex_name = data[1]
                if data[2]:
                    file_path = Path(data[2])
        menu = QMenu(self)
        copy_action = menu.addAction("Copy hex name")
        copy_action.setEnabled(bool(hex_name))
        export_action = menu.addAction("Export file…")
        export_action.setEnabled(file_path is not None and Path(file_path).is_file())
        explore_action = menu.addAction("Search in Explorer")
        explore_action.setEnabled(
            file_path is not None and Path(file_path).is_file()
        )
        chosen = menu.exec(tree.viewport().mapToGlobal(pos))
        if chosen is copy_action and hex_name:
            QGuiApplication.clipboard().setText(hex_name)
        elif chosen is export_action and file_path is not None:
            self._export_cache_file(Path(file_path))
        elif chosen is explore_action and file_path is not None:
            self._open_in_explorer(file_path)

    def _export_cache_file(self, path: Path):
        default = self.output_dir / path.name
        target, _ = QFileDialog.getSaveFileName(
            self, "Export file", str(default), "All files (*.*)"
        )
        if not target:
            return
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            QMessageBox.information(self, APP_TITLE, f"Exported:\n{target}")
        except OSError as exc:
            self._show_error(str(exc))

    def _open_in_explorer(self, path: Path):
        path = Path(path).resolve()
        if sys.platform == "win32":
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])

    def _choose_output_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose output folder", self.convert_output_input.text().strip()
        )
        if folder:
            self.convert_output_input.setText(os.path.normpath(folder))

    def _choose_glb_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a GLB/glTF model", "",
            "GLB/glTF models (*.glb *.gltf);;All files (*.*)",
        )
        if path:
            self.glb_file_label.setText(Path(path).name)
            self.glb_file_label.setToolTip(str(path))
            output = self.glb_output_input.text().strip()
            default_output = str(self.output_dir)
            if not output or Path(output) == Path(default_output):
                self.glb_output_input.setText(
                    str(self.output_dir / f"{Path(path).stem}.{self.psg_extension}")
                )
            self._update_glb_ready()

    def _choose_glb_donor(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a donor template", "",
            f"{self.platform_info['label']} files (*.{self.psg_extension});;All files (*.*)",
        )
        if path:
            self.glb_donor_label.setText(Path(path).name)
            self.glb_donor_label.setToolTip(str(path))
            self._update_glb_ready()

    def _choose_glb_output(self):
        default = self.glb_output_input.text().strip() or str(
            self.output_dir / f"converted.{self.psg_extension}"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Choose output file", default,
            f"{self.platform_info['label']} files (*.{self.psg_extension});;All files (*.*)",
        )
        if path:
            self.glb_output_input.setText(os.path.normpath(path))

    def _update_glb_ready(self):
        self.glb_convert_button.setEnabled(bool(
            self.glb_file_label.toolTip()
            and self.glb_donor_label.toolTip()
            and self.glb_output_input.text().strip()
        ))

    def _convert_glb(self):
        glb_path = self.glb_file_label.toolTip()
        donor_path = self.glb_donor_label.toolTip()
        output_path = self.glb_output_input.text().strip()
        if not (glb_path and donor_path and output_path):
            self._show_error("Choose a model, a donor template and an output file.")
            return
        scale = self.glb_scale.value()
        self.glb_convert_button.setEnabled(False)
        self.glb_log.clear()

        emitter = LogEmitter()
        emitter.log_line.connect(self.glb_log.appendPlainText)

        if self.platform == "xbx":
            from rx2_glb_converter import convert_glb_to_rx2
            def task():
                return convert_glb_to_rx2(
                    glb_path, donor_path, output_path,
                    scale_xyz=scale, log=emitter.log_line.emit,
                )
        else:
            from psg_glb_converter import convert_glb_to_psg
            def task():
                return convert_glb_to_psg(
                    glb_path, donor_path, output_path,
                    scale_xyz=scale, log=emitter.log_line.emit,
                )

        def done(result):
            self._update_glb_ready()
            QMessageBox.information(
                self, APP_TITLE,
                f"{self.platform_info['label']} created:\n{result.output_path}\n\n"
                f"{result.vertex_count} vertices | {result.face_count} faces\n"
                f"Skinned: {'yes' if result.skinned else 'no'} | "
                f"Bones: {result.bone_count} | {result.file_size} bytes",
            )

        self._start_worker(task, done)

    def _choose_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tiff *.tif *.webp *.gif);;All files (*.*)",
        )
        if path:
            self._set_input_image(path)

    def _set_input_image(self, path: str):
        if not self.image_drop_zone.set_image(path):
            self._show_error("The selected file is not a supported image.")
            return
        self.input_image_path = str(Path(path).resolve())
        self.image_path_label.setText(Path(path).name)
        self._update_convert_ready()

    def _update_convert_ready(self):
        alias = self.alias_input.text().strip()
        valid_alias = (
            len(alias) == HEX_LENGTH
            and alias.startswith("0x")
            and all(character in "0123456789abcdefABCDEF" for character in alias[2:])
        )
        self.convert_button.setEnabled(bool(self.input_image_path) and valid_alias)

    def _increase_resolution(self):
        value = self.resolution.value()
        new_value = min(self.resolution.maximum(), value * 2)
        self.resolution.setValue(new_value)

    def _decrease_resolution(self):
        value = self.resolution.value()
        if value <= 128:
            self.resolution.setValue(128)
            return
        new_value = max(128, value // 2)
        self.resolution.setValue(new_value)

    def _convert_image(self):
        source = self.input_image_path
        alias = self.alias_input.text().strip().lower()
        folder = self.convert_output_input.text().strip() or str(self.output_dir)
        if not source:
            self._show_error("Choose an input image.")
            return
        if len(alias) != HEX_LENGTH or not alias.startswith("0x"):
            self._show_error("The hex string must be 18 characters and begin with 0x.")
            return
        try:
            int(alias[2:], 16)
        except ValueError:
            self._show_error("The hex string contains non-hexadecimal characters.")
            return
        self.convert_button.setEnabled(False)
        if self.platform == "xbx":
            self._start_worker(
                lambda: self._convert_image_rx2(
                    source, folder, alias, self.resolution.value(),
                    self.convert_opacity.value() / 100,
                ),
                self._conversion_finished,
            )
            return
        self._start_worker(
            lambda: self.converter.convert_image(
                source,
                folder,
                alias,
                self.resolution.value(),
                self.convert_opacity.value() / 100,
            ),
            self._conversion_finished,
        )

    def _convert_image_rx2(self, source: str, folder: str, alias: str,
                           resolution: int = 512, opacity: float = 1.0) -> str:
        """Convert an image to an RX2 texture with the built-in encoder.

        The image is resized to a square power of two and encoded to a
        tiled DXT5 mip chain in pure Python, using the bundled container
        template (assets/rx2/container.rx2) for the header and file table.
        The opacity slider scales the alpha channel (10-15% gives a subtle
        in-game look on character textures).
        """
        container = self.assets_dir / "rx2" / "container.rx2"
        if not container.is_file():
            raise FileNotFoundError(
                f"Container template not found: {container}. "
                "Keep the assets folder next to UTT.exe."
            )
        if not 16 <= resolution <= 4096:
            raise ValueError("Resolution must be between 16 and 4096")
        resolution = 1 << (resolution.bit_length() - 1)
        resolution = max(128, min(resolution, 4096))
        opacity = max(0.0, min(1.0, opacity))
        output_path = Path(folder).resolve() / f"{alias}.rx2"
        from rx2_parser import encode_rx2_texture
        with Image.open(source) as image:
            image = image.convert("RGBA")
            image = image.resize(
                (resolution, resolution), Image.Resampling.LANCZOS
            )
            if opacity < 1.0:
                alpha = image.getchannel("A").point(
                    lambda value: int(value * opacity)
                )
                image.putalpha(alpha)
            rgba = image.tobytes()
        data = encode_rx2_texture(container.read_bytes(),
                                  resolution, resolution, rgba)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        return str(output_path)

    def _conversion_finished(self, output):
        self._update_convert_ready()
        QMessageBox.information(
            self, APP_TITLE,
            f"{self.platform_info['label']} created:\n{output}",
        )

    def _start_worker(self, task, on_success, on_failure=None):
        thread = QThread(self)
        worker = Worker(task)
        self._workers.append(worker)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(on_success)
        worker.failed.connect(on_failure or self._worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(
            lambda: self._workers.remove(worker) if worker in self._workers else None
        )
        thread.finished.connect(lambda: self._threads.remove(thread) if thread in self._threads else None)
        self._thread = thread
        self._threads.append(thread)
        thread.start()

    def closeEvent(self, event):
        for thread in list(self._threads):
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait(2000)
            except RuntimeError:
                pass
        super().closeEvent(event)

    def _worker_failed(self, details: str):
        self._update_convert_ready()
        if self.unpack_progress:
            self.unpack_progress.close()
            self.unpack_progress = None
        self._show_error(details)

    def _show_error(self, details: str):
        print(details, file=sys.stderr)
        QMessageBox.critical(self, APP_TITLE, details.splitlines()[-1] if details else "An unknown error occurred.")

    def _show_credits(self):
        QMessageBox.information(
            self,
            "UTT Credits",
            CREDITS_TEXT,
        )

    def _show_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Settings")
        dialog.setModal(True)
        dialog.setMinimumWidth(420)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(14)
        title = QLabel("Settings")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        platform_row = QHBoxLayout()
        platform_row.setSpacing(12)
        platform_row.addWidget(QLabel("Platform mode"))
        combo = QComboBox()
        for key, info in PLATFORMS.items():
            combo.addItem(info["name"], key)
        combo.setCurrentIndex(combo.findData(self.platform))
        platform_row.addWidget(combo, 1)
        layout.addLayout(platform_row)

        note = QLabel(
            "Switching platform restarts UTT in that mode. Textures are read from "
            f"the {PLATFORMS[self.platform]['name']} cache; if no cache exists for the "
            "new platform you will be asked to extract its createacharacter.big."
        )
        note.setWordWrap(True)
        note.setObjectName("convertDescription")
        layout.addWidget(note)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(dialog.reject)
        apply_button = QPushButton("Switch platform")
        apply_button.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply_button)
        layout.addLayout(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target = combo.currentData()
        if target == self.platform:
            return
        save_platform(target)
        self._restart_app()

    def _restart_app(self):
        try:
            if getattr(sys, "frozen", False):
                command = [sys.executable]
            else:
                command = [
                    sys.executable,
                    str(Path(__file__).resolve().parent / "main.py"),
                ]
            command.extend(sys.argv[1:])
            subprocess.Popen(
                command,
                cwd=str(Path(__file__).resolve().parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            pass
        self.close()

    def _toggle_maximized(self):
        """Fill the screen while staying a window.

        Real maximize makes the frameless window cover the entire monitor,
        which NVIDIA treats as borderless fullscreen and pops its overlay over
        the app. Leaving a small visible desktop edge keeps the window
        classified as a window so the overlay never appears.
        """
        screen = self.screen() or QGuiApplication.primaryScreen()
        available = screen.availableGeometry()
        margin = 2
        fill_rect = QRect(
            available.left() + margin,
            available.top() + margin,
            available.width() - margin * 2,
            available.height() - margin * 2,
        )
        if self.geometry() == fill_rect:
            self.setGeometry(self._normal_geometry)
        else:
            self._normal_geometry = self.geometry()
            self.setGeometry(fill_rect)

    def changeEvent(self, event):
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.WindowStateChange
            and hasattr(self, "title_bar")
        ):
            self.title_bar.sync_maximized()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.current_texture is not None:
            self._preview_loaded(self.current_texture)
        self._refresh_character_texture_preview()
