"""PyQt6 user interface for browsing and converting Skate 3 PSG textures."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from PIL.ImageQt import ImageQt
from PyQt6.QtCore import QEvent, QObject, QRegularExpression, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap, QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QProgressDialog, QPushButton, QSizePolicy,
    QSlider, QSpinBox, QSplitter, QStackedWidget, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from archive_manager import ArchiveManager
from gltf_exporter import export_gltf
from model_viewer import ModelPreview
from psg_converter import PSGConverter
from PSGTx import PSGTx


APP_TITLE = "UTT — PSG Tool"
HEX_LENGTH = 18


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
    if (build_dir / "cache").is_dir():
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
        maximized = self.window.isMaximized()
        self.maximize_button.setText("❐" if maximized else "□")
        self.maximize_button.setToolTip("Restore" if maximized else "Maximize")

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


class ArchivePicker(QDialog):
    """Non-dismissable first-run gate used to select createacharacter.big."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_path: Path | None = None
        self.setWindowTitle("Select createacharacter.big")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        title = QLabel("Select your createacharacter.big file")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        layout.addWidget(QLabel(
            "UTT needs the game archive before it can list or preview textures. "
            "The archive will be extracted into a local cache folder beside UTT."
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
    def __init__(self, model_loader):
        super().__init__()
        self.model_loader = model_loader
        self.root_dir = resource_dir()
        self.user_dir = working_dir()
        self.assets_dir = self.root_dir / "assets"
        self.cache_dir = self.user_dir / "cache"
        self.output_dir = self.user_dir / "exports"
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

        self.setWindowTitle(APP_TITLE)
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )
        self.resize(1200, 760)
        self.setMinimumSize(900, 600)
        self._build_ui()
        self._apply_style()
        self._load_catalog()
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
        shell_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(shell)

    def _make_browser_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QHBoxLayout()
        self.archive_status = QLabel("Archive not loaded")
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

        self.model_tree = QTreeWidget()
        self.model_tree.setHeaderLabels(["Model folder / PSG file"])
        self.model_tree.setMinimumWidth(300)
        self.model_tree.itemSelectionChanged.connect(self._on_texture_selected)

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
        self.force_opaque.setToolTip("Off preserves the alpha stored in the PSG. On applies PSGTx's alpha cleanup.")
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
        layout.setContentsMargins(32, 28, 32, 32)
        layout.setSpacing(18)

        heading = QLabel("Image to PSG")
        heading.setObjectName("convertTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        description = QLabel(
            "Drop or choose an image, set its square resolution, then enter the PSG hex name."
        )
        description.setObjectName("convertDescription")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(description)

        card = QFrame()
        card.setObjectName("card")
        card.setMaximumWidth(980)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 22, 22, 22)
        card_layout.setSpacing(16)

        self.image_drop_zone = ImageDropZone()
        self.image_drop_zone.setObjectName("imageDropZone")
        self.image_drop_zone.clicked.connect(self._choose_image)
        self.image_drop_zone.imageDropped.connect(self._set_input_image)
        card_layout.addWidget(self.image_drop_zone, 1)

        self.image_path_label = QLabel("No image selected")
        self.image_path_label.setObjectName("selectedImagePath")
        self.image_path_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_path_label.setWordWrap(True)
        card_layout.addWidget(self.image_path_label)

        image_btn = QPushButton("Select image")
        image_btn.clicked.connect(self._choose_image)
        card_layout.addWidget(image_btn, 0, Qt.AlignmentFlag.AlignCenter)

        fields = QHBoxLayout()
        fields.setSpacing(18)

        alias_column = QVBoxLayout()
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
        fields.addLayout(alias_column, 1)

        resolution_column = QVBoxLayout()
        resolution_column.addWidget(QLabel("Resolution"))
        self.resolution = QSpinBox()
        self.resolution.setRange(128, 4096)
        self.resolution.setKeyboardTracking(False)
        self.resolution.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resolution.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
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
        fields.addLayout(resolution_column)
        card_layout.addLayout(fields)

        opacity_header = QHBoxLayout()
        opacity_header.addWidget(QLabel("Opacity"))
        opacity_header.addStretch(1)
        self.convert_opacity_text = QLabel("100%")
        self.convert_opacity_text.setObjectName("convertDescription")
        opacity_header.addWidget(self.convert_opacity_text)
        card_layout.addLayout(opacity_header)

        self.convert_opacity = QSlider(Qt.Orientation.Horizontal)
        self.convert_opacity.setRange(0, 100)
        self.convert_opacity.setValue(100)
        self.convert_opacity.valueChanged.connect(
            lambda value: self.convert_opacity_text.setText(f"{value}%")
        )
        card_layout.addWidget(self.convert_opacity)

        output_hint = QLabel(f"Output: {self.output_dir}")
        output_hint.setObjectName("convertDescription")
        output_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        output_hint.setWordWrap(True)
        card_layout.addWidget(output_hint)

        self.convert_button = QPushButton("Convert image to PSG")
        self.convert_button.setObjectName("convertButton")
        self.convert_button.setEnabled(False)
        self.convert_button.clicked.connect(self._convert_image)
        card_layout.addWidget(self.convert_button)
        layout.addWidget(card, 1, Qt.AlignmentFlag.AlignHCenter)

        return page

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget { background: #202124; color: #f1f3f4; font-size: 14px; }
            QFrame#titleBar { background: #2b2c2f; border-bottom: 1px solid #3c4043; }
            QFrame#titleBar QLabel { background: transparent; }
            QLabel#windowTitle { color: #d9dce1; font-size: 13px; font-weight: 600; }
            QPushButton#creditsButton { background: transparent; color: #aeb4bd; border-radius: 8px; padding: 6px 11px; font-weight: 500; }
            QPushButton#creditsButton:hover { background: #3c4043; color: #ffffff; }
            QPushButton#titleWindowButton, QPushButton#titleCloseButton { background: transparent; border-radius: 0; padding: 0; font-size: 17px; font-weight: 400; }
            QPushButton#titleWindowButton:hover { background: #3c4043; }
            QPushButton#titleCloseButton:hover { background: #c42b1c; }
            QTabWidget::pane { border: 0; border-top: 1px solid #3c4043; }
            QTabBar::tab { background: #303134; padding: 13px 22px; margin: 0; border: 0; }
            QTabBar::tab:selected { background: #4d5156; border-bottom: 3px solid #8ab4f8; }
            QPushButton { background: #5b86e5; border: 0; border-radius: 16px; padding: 9px 16px; font-weight: 600; }
            QPushButton:hover { background: #7299ec; }
            QPushButton:disabled { background: #45474b; color: #9aa0a6; }
            QLineEdit, QTreeWidget, QSpinBox { background: #2b2c2f; border: 1px solid #4a4d52; border-radius: 9px; padding: 7px; }
            QTreeWidget::item { padding: 5px; border-radius: 6px; }
            QTreeWidget::item:selected { background: #536d9f; }
            QLabel#preview { background: #18191b; border: 1px dashed #5f6368; border-radius: 16px; }
            QLabel#dialogTitle { font-size: 20px; font-weight: 700; }
            QLabel#status { color: #a9c7fa; }
            QWidget#detailsPanel { background: #2b2c2f; border-radius: 16px; }
            QFrame#card { background: #292a2d; border: 1px solid #3c4043; border-radius: 18px; }
            QLabel#convertTitle { font-size: 26px; font-weight: 700; }
            QLabel#convertDescription, QLabel#selectedImagePath, QLabel#modelDetails { color: #aeb4bd; }
            QLabel#imageDropZone { background: #18191b; border: 2px dashed #5f6368; border-radius: 16px; color: #aeb4bd; font-size: 17px; }
            QLabel#imageDropZone:hover { border-color: #8ab4f8; color: #f1f3f4; }
            QLabel#modelTitle { font-size: 18px; font-weight: 700; }
            QLineEdit#aliasInput { font-family: Consolas; font-size: 16px; padding: 10px; }
            QPushButton#resolutionButton { font-size: 21px; padding: 5px 10px; }
            QPushButton#convertButton { font-size: 16px; padding: 12px 18px; }
            QSlider::groove:horizontal { height: 7px; background: #4a4d52; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #8ab4f8; border-radius: 3px; }
            QSlider::handle:horizontal { width: 18px; margin: -6px 0; background: #d2e3fc; border-radius: 9px; }
        """)

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
        self.texture_tree.clear()
        # Before an archive is chosen we show the full reference catalog. Once
        # a cache exists, IDs without a matching PSG are omitted entirely.
        self._add_catalog_nodes(
            self.texture_tree, catalog, bool(self.psg_index), self.search_box.text().strip().lower()
        )

    def _refresh_browser_tree(self):
        mode = self.browser_mode.currentData()
        if mode is None:
            mode = self.browser_mode.currentText().strip().lower()
        if mode == "models":
            self.browser_stack.setCurrentWidget(self.model_page)
            self.model_tree.setHeaderLabels(["Model folder / PSG file"])
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
            elif child.suffix.lower() == ".psg" and (
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
                if self._add_catalog_nodes(item, child, available_only, search, show_all_children):
                    has_children = True
                else:
                    container.removeChild(item)
            else:
                for alias in child:
                    if available_only and alias.lower() not in self.psg_index:
                        continue
                    if search and not show_all_children and search not in alias.lower():
                        continue
                    leaf = QTreeWidgetItem(item, [alias])
                    leaf.setData(0, Qt.ItemDataRole.UserRole, alias.lower())
                    has_children = True
                if item.childCount() == 0:
                    container.removeChild(item)
        return has_children

    def _request_archive(self):
        picker = ArchivePicker(self)
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
            "This can take several minutes."
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
        if self.cache_dir.is_dir() and any(self.cache_dir.rglob("*.psg")):
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
        for file in self.cache_dir.rglob("*.psg"):
            index.setdefault(file.stem.lower(), []).append(file)
        return index

    def _index_cache(self):
        self.psg_index = self._build_psg_index() if self.cache_dir.exists() else {}
        self.archive_status.setText(f"Cache ready — {sum(map(len, self.psg_index.values()))} PSG files found")
        self.repack_button.setEnabled(bool(self.psg_index))

    def _archive_loaded(self, index):
        self.psg_index = index
        self._refresh_browser_tree()
        self.archive_status.setText(f"Cache ready — {sum(map(len, index.values()))} PSG files found")
        self.repack_button.setEnabled(bool(index))
        if self.unpack_progress:
            self.unpack_progress.close()
            self.unpack_progress = None

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
        self._start_worker(lambda: PSGTx(str(matches[0])), self._preview_loaded)

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

    def _refresh_preview_alpha(self):
        if self.current_texture is not None:
            self._preview_loaded(self.current_texture)

    def _export_preview(self):
        if self.current_image is None:
            QMessageBox.information(self, APP_TITLE, "Load a PSG preview before exporting.")
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
            self.current_texture.export_tx(path, self.force_opaque.isChecked())
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
        folder = str(self.output_dir)
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

    def _conversion_finished(self, output):
        self._update_convert_ready()
        QMessageBox.information(self, APP_TITLE, f"PSG created:\n{output}")

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
            "Credits\n\n"
            "duckyinnit — everything\n"
            "itsclaudeya — model viewer",
        )

    def _toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

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
