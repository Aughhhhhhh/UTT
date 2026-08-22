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
from PyQt6.QtCore import (
    QEvent, QObject, QPoint, QPointF, QRect, QRectF, QRegularExpression, QSize,
    QSizeF, Qt, QThread, QTimer, pyqtProperty, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QGuiApplication, QLinearGradient, QPainter, QPainterPath, QPen,
    QPixmap, QPolygon, QRegularExpressionValidator,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFrame, QGraphicsBlurEffect, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QProgressDialog, QPushButton, QRadioButton,
    QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter, QStackedWidget,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import recipe
import updater

from alpha_mask import apply_alpha_mask
from archive_manager import ArchiveManager
from gltf_exporter import export_gltf
from game_route import (
    clean_ps3_install_folders,
    content_dir,
    game_root,
    is_original_big,
    locate_or_backup_source,
    ps3_game_dir,
    unpack_into_game,
)
from mdl_parser import extract_material_textures
from model_viewer import ModelPreview
from psg_converter import PSGConverter
from PSGTx import PSGTx


APP_TITLE = "UTT — Ultimate Texture Toolkit"
APP_VERSION = "2.1.1"

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

THEMES = {
    "grey_black": {
        "name": "Grey to Black",
        "gradient": ("#2d2e33", "#050506"),
        "surface": "#2b2c2f", "border": "#4a4d52",
        "line": "#3c4043", "outline": "#5f6368", "card": "#292a2d",
        "ps3": {
            "accent": "#5b86e5", "hover": "#7299ec",
            "dark": "#536d9f", "soft": "#8ab4f8",
        },
        "xbx": {
            "accent": "#107c10", "hover": "#1e9e5a",
            "dark": "#2d7a46", "soft": "#6fbf73",
        },
    },
    "blue_purple": {
        "name": "Blue to Purple",
        "gradient": ("#28336b", "#160d38"),
        "surface": "#262843", "border": "#464868",
        "line": "#373a56", "outline": "#5f6287", "card": "#23263d",
        "ps3": {
            "accent": "#8f6ff0", "hover": "#a68bf5",
            "dark": "#6d50cf", "soft": "#b8a3fa",
        },
        "xbx": {
            "accent": "#7b8ff0", "hover": "#95a5f5",
            "dark": "#5d6fcf", "soft": "#aab8fa",
        },
    },
}

DEFAULT_THEME = "grey_black"


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
    imageDropped = pyqtSignal(list)
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source_pixmap = QPixmap()
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setText("Drop images here\nor click to choose multiple")

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
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if paths:
            self.imageDropped.emit(paths)
            event.acceptProposedAction()

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


class _AliasEdit(QLineEdit):
    """Hex name input that selects its pre-filled text on focus so typing
    replaces it (same as the original Franks-Painting tool)."""

    def focusInEvent(self, event):
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)


class _ArrowButton(QPushButton):
    """Resolution arrow that paints its triangle with QPainter instead of a
    text glyph, so the arrow is visible regardless of font glyph issues.
    The color is fed through QSS via qproperty-arrowColor."""

    def __init__(self, up: bool, parent=None):
        super().__init__(parent)
        self._up = up
        self._arrow_color = QColor("#8ab4f8")
        self.setText("")
        self.setFixedSize(31, 23)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    @pyqtProperty(QColor)
    def arrowColor(self):
        return self._arrow_color

    @arrowColor.setter
    def arrowColor(self, value):
        self._arrow_color = QColor(value)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(7, 6, -7, -6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffffff") if self.underMouse() else self._arrow_color)
        if self._up:
            points = [
                QPoint(rect.center().x(), rect.top()),
                QPoint(rect.left(), rect.bottom()),
                QPoint(rect.right(), rect.bottom()),
            ]
        else:
            points = [
                QPoint(rect.center().x(), rect.bottom()),
                QPoint(rect.left(), rect.top()),
                QPoint(rect.right(), rect.top()),
            ]
        painter.drawPolygon(QPolygon(points))
        painter.end()


class _RemoveButton(QPushButton):
    """Remove button that paints an X with QPainter (same rationale as
    _ArrowButton). Color fed through QSS via qproperty-removeColor."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._remove_color = QColor("#aeb4bd")
        self.setText("")
        self.setFixedSize(30, 31)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    @pyqtProperty(QColor)
    def removeColor(self):
        return self._remove_color

    @removeColor.setter
    def removeColor(self, value):
        self._remove_color = QColor(value)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(8, 8, -8, -8)
        painter.setPen(QPen(
            QColor("#ff6b6b") if self.underMouse() else self._remove_color,
            2, Qt.PenStyle.SolidLine,
        ))
        painter.drawLine(rect.topLeft(), rect.bottomRight())
        painter.drawLine(rect.topRight(), rect.bottomLeft())
        painter.end()


class ConvertJobCard(QFrame):
    """One queued image in the texture convert tab.

    Mirrors the original Franks-Painting card: thumbnail and file name on
    the left, alias input underneath, output resolution and opacity
    controls on the right, remove button at the top right.
    """

    changed = pyqtSignal()
    remove_requested = pyqtSignal(object)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.setObjectName("convertJobCard")
        self.setFixedHeight(131)
        self.path = str(Path(path).resolve())
        self.setToolTip(self.path)

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(14)

        thumb = QLabel()
        thumb.setObjectName("jobThumb")
        thumb.setFixedSize(93, 93)
        pixmap = QPixmap(self.path)
        if not pixmap.isNull():
            thumb.setPixmap(pixmap.scaled(
                93, 93,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        root.addWidget(thumb)

        info = QVBoxLayout()
        info.setSpacing(4)
        name = QLabel(Path(self.path).name)
        name.setObjectName("jobFileName")
        info.addWidget(name)
        alias_label = QLabel("Alias")
        alias_label.setObjectName("jobFieldLabel")
        info.addWidget(alias_label)
        self.alias_input = _AliasEdit("0x0000000000000000")
        self.alias_input.setObjectName("jobAliasInput")
        self.alias_input.setMaxLength(HEX_LENGTH)
        self.alias_input.setFixedWidth(221)
        self.alias_input.setValidator(QRegularExpressionValidator(
            QRegularExpression(r"0x[0-9A-Fa-f]{0,16}"), self.alias_input
        ))
        self.alias_input.textChanged.connect(lambda _text: self.changed.emit())
        info.addWidget(self.alias_input)
        info.addStretch(1)
        root.addLayout(info, 1)

        res = QVBoxLayout()
        res.setSpacing(4)
        res_label = QLabel("Output res")
        res_label.setObjectName("jobFieldLabel")
        res_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        res.addWidget(res_label)
        self.resolution_label = QLabel("512")
        self.resolution_label.setObjectName("jobResDisplay")
        self.resolution_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        res.addWidget(self.resolution_label)
        res_row = QHBoxLayout()
        res_row.setSpacing(6)
        down = _ArrowButton(False)
        down.setObjectName("resArrowButton")
        down.clicked.connect(self._decrease_resolution)
        up = _ArrowButton(True)
        up.setObjectName("resArrowButton")
        up.clicked.connect(self._increase_resolution)
        res_row.addWidget(down)
        res_row.addWidget(up)
        res.addLayout(res_row)
        res.addStretch(1)
        root.addLayout(res)

        opacity = QVBoxLayout()
        opacity.setSpacing(4)
        opacity_label = QLabel("opacity")
        opacity_label.setObjectName("jobFieldLabel")
        opacity_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        opacity.addWidget(opacity_label)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(10)
        self.opacity.setFixedWidth(141)
        self.opacity.valueChanged.connect(self._on_opacity_changed)
        opacity.addWidget(self.opacity, 0, Qt.AlignmentFlag.AlignCenter)
        self.opacity_text = QLabel("10%")
        self.opacity_text.setObjectName("jobResDisplay")
        self.opacity_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        opacity.addWidget(self.opacity_text)
        opacity.addStretch(1)
        root.addLayout(opacity)

        remove = _RemoveButton()
        remove.setObjectName("jobRemoveButton")
        remove.setToolTip("Remove from queue")
        remove.clicked.connect(lambda: self.remove_requested.emit(self))
        root.addWidget(remove, 0, Qt.AlignmentFlag.AlignTop)

    @property
    def resolution_value(self) -> int:
        return int(self.resolution_label.text())

    def _increase_resolution(self):
        self.resolution_label.setText(str(min(4096, self.resolution_value * 2)))
        self.changed.emit()

    def _decrease_resolution(self):
        self.resolution_label.setText(str(max(128, self.resolution_value // 2)))
        self.changed.emit()

    def _on_opacity_changed(self, value: int):
        self.opacity_text.setText(f"{value}%")
        self.changed.emit()

    def alias(self) -> str:
        return self.alias_input.text().strip()


class FullWidthTabWidget(QTabWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.tabBar().setFixedWidth(self.width())


class ComboOverlay(QFrame):
    """In-window dropdown used instead of the native popup window.

    The native popup is a separate top-level window that carries an
    unavoidable DWM shadow (grey box). A plain child widget has no window
    frame or shadow, so the list is drawn inside the app instead; the
    rounded corners show the parent's background through them.
    """

    MAX_ROWS = 5

    def __init__(self, combo: "AppComboBox"):
        super().__init__(combo.window())
        self.combo = combo
        self.setObjectName("comboOverlay")
        self.list = QListWidget(self)
        self.list.setObjectName("comboOverlayList")
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.list)
        self.list.itemClicked.connect(self._choose)
        self._apply_theme()
        self.hide()

    def _app_window(self):
        top = self.window()
        while top is not None and not isinstance(top, QMainWindow):
            top = top.parentWidget()
        return top

    def _apply_theme(self):
        """Give the overlay its own stylesheet so ancestor sheets (e.g. the
        settings dialog's `QFrame { background: transparent; }`) cannot make
        it see-through."""
        main = self._app_window()
        accent_dark = getattr(main, "accent_dark", "#536d9f")
        accent_soft = getattr(main, "accent_soft", "#8ab4f8")
        surface = getattr(main, "surface", "#2b2c2f")
        border = getattr(main, "border", "#4a4d52")
        line = getattr(main, "line", "#3c4043")
        self.setStyleSheet(
            f"#comboOverlay {{ background: {surface}; border: 1px solid {border}; "
            "border-radius: 9px; }"
            "QListWidget#comboOverlayList { background: transparent; border: none; "
            "outline: none; color: #f1f3f4; font-size: 14px; }"
            "QListWidget#comboOverlayList::item { padding: 4px 8px; border-radius: 6px; }"
            f"QListWidget#comboOverlayList::item:hover {{ background: {line}; "
            "color: #ffffff; }"
            f"QListWidget#comboOverlayList::item:selected {{ background: {accent_dark}; "
            f"color: #ffffff; border: 1px solid {accent_soft}; }}"
        )

    def populate(self):
        self.list.clear()
        for index in range(self.combo.count()):
            item = QListWidgetItem(self.combo.itemText(index))
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.list.addItem(item)
        current = self.combo.currentIndex()
        self.list.setCurrentRow(current)
        if current >= 0:
            self.list.scrollToItem(self.list.item(current))

    def move_selection(self, delta: int):
        row = self.list.currentRow()
        if row < 0:
            row = 0 if delta > 0 else self.list.count() - 1
        else:
            row = min(max(row + delta, 0), self.list.count() - 1)
        self.list.setCurrentRow(row)

    def _choose(self, item):
        index = item.data(Qt.ItemDataRole.UserRole)
        if index != self.combo.currentIndex():
            self.combo.setCurrentIndex(index)
        self.combo._hide_overlay()

    def eventFilter(self, obj, event):
        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress:
            target = QApplication.widgetAt(event.globalPosition().toPoint())
            if target is None or (
                target is not self.combo and not self.isAncestorOf(target)
            ):
                self.combo._hide_overlay()
        elif etype in (
            QEvent.Type.WindowDeactivate,
            QEvent.Type.Resize,
            QEvent.Type.Move,
        ):
            if self.isVisible():
                self.combo._hide_overlay()
        return False


class AppComboBox(QComboBox):
    """Combo with a drawn triangle arrow and an in-window dropdown list.

    Qt stylesheets cannot reliably render a down-arrow on this platform, so
    the arrow triangle is painted directly in paintEvent. The native popup
    window carries an unavoidable DWM shadow, so showPopup draws a child
    widget of the top-level window instead.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._overlay: ComboOverlay | None = None

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        x0 = self.width() - 18
        y0 = self.height() / 2 - 3.0
        triangle = QPainterPath()
        triangle.moveTo(x0, y0)
        triangle.lineTo(x0 + 10.0, y0)
        triangle.lineTo(x0 + 5.0, y0 + 6.0)
        triangle.closeSubpath()
        painter.fillPath(triangle, QColor(174, 180, 189))

    def showPopup(self):
        if self.count() == 0:
            return
        if self._overlay is not None and self._overlay.isVisible():
            self._hide_overlay()
            return
        if self._overlay is None:
            self._overlay = ComboOverlay(self)
        self._overlay._apply_theme()
        self._overlay.populate()
        top = self.window()
        margin = 6
        width = max(self.width(), 140)
        row_height = self._overlay.list.sizeHintForRow(0)
        height = min(self.count(), ComboOverlay.MAX_ROWS) * row_height + 12
        pos = self.mapTo(top, QPoint(0, self.height() + margin))
        if pos.y() + height > top.height():
            pos = self.mapTo(top, QPoint(0, -height - margin))
        self._overlay.setGeometry(pos.x(), max(0, pos.y()), width, height)
        self._overlay.show()
        self._overlay.raise_()
        top.installEventFilter(self._overlay)

    def hidePopup(self):
        super().hidePopup()
        self._hide_overlay()

    def keyPressEvent(self, event):
        if self._overlay is not None and self._overlay.isVisible():
            key = event.key()
            if key in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if key != Qt.Key.Key_Escape:
                    row = self._overlay.list.currentRow()
                    item = (
                        self._overlay.list.item(row) if row >= 0 else None
                    )
                    if item is not None:
                        index = item.data(Qt.ItemDataRole.UserRole)
                        if index != self.currentIndex():
                            self.setCurrentIndex(index)
                self._hide_overlay()
                event.accept()
                return
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._overlay.move_selection(
                    1 if key == Qt.Key.Key_Down else -1
                )
                event.accept()
                return
        super().keyPressEvent(event)

    def _hide_overlay(self):
        if self._overlay is None:
            return
        top = self._overlay.parentWidget()
        if top is not None:
            top.removeEventFilter(self._overlay)
        self._overlay.hide()


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

        version_label = QLabel(f"v{APP_VERSION}")
        version_label.setObjectName("versionLabel")
        layout.addWidget(version_label)
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


class PackModeCard(QWidget):
    """Clickable pack-mode card: 256x256 image blurs and shows a text overlay on hover."""

    clicked = pyqtSignal()

    def __init__(self, image_path: str, text: str, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("packModeCard")
        self.setFixedSize(256, 256)

        layout = QGridLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            pixmap = QPixmap(256, 256)
            pixmap.fill(Qt.GlobalColor.darkGray)
        self.image_label.setPixmap(pixmap.scaled(
            256,
            256,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        layout.addWidget(self.image_label, 0, 0)

        self.text_label = QLabel(text)
        self.text_label.setObjectName("packModeText")
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


class PackModePicker(QDialog):
    """First-run gate: choose 'Keep files packed' or 'Keep files unpacked'."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected: bool | None = None  # True = packed, False = unpacked
        self.setWindowTitle(f"{APP_TITLE} — How to keep your game files")
        self.setModal(True)
        self.setMinimumSize(700, 520)
        self.setStyleSheet("""
            QWidget { background: #202124; color: #f1f3f4; font-size: 14px; }
            QLabel#packModeTitle { font-size: 26px; font-weight: 700; }
            QLabel#packModeSubtitle { color: #aeb4bd; }
            QLabel#packModeText {
                background: rgba(20, 20, 24, 210); color: #ffffff;
                font-size: 15px; font-weight: 700; padding: 12px 16px;
                border-radius: 10px;
            }
            QWidget#packModeCard {
                background: #2b2c2f; border: 1px solid #3c4043;
                border-radius: 16px;
            }
            QWidget#packModeCard:hover { border: 1px solid #8ab4f8; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(16)

        title = QLabel("How do you want to keep your game files?")
        title.setObjectName("packModeTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        subtitle = QLabel(
            "Keep files packed to have the game read a .big archive, or keep "
            "files unpacked to read loose files directly and replace textures "
            "and models more easily. You can change this later in Settings."
        )
        subtitle.setObjectName("packModeSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        row = QHBoxLayout()
        row.setSpacing(24)
        self.packed_card = PackModeCard(
            str(resource_file("Keep Files Packed.png")),
            "Keep files packed, recommended for newer users and Xbox users",
            self,
        )
        self.unpacked_card = PackModeCard(
            str(resource_file("Keep Files Unpacked.png")),
            "Keep files unpacked, recommended for Recomp, easier texture and model replacing",
            self,
        )
        self.packed_card.clicked.connect(lambda: self._choose(True))
        self.unpacked_card.clicked.connect(lambda: self._choose(False))
        row.addWidget(self.packed_card)
        row.addWidget(self.unpacked_card)
        layout.addLayout(row, 1)

    def _choose(self, packed: bool):
        self.selected = packed
        self.accept()


def _dialog_theme_style(platform: str) -> str:
    """Themed stylesheet for standalone setup dialogs.

    Dialogs shown before the main window exists (or re-shown from Choose
    archive) need their own theme so they match the app instead of falling
    back to the default Fusion look.
    """
    theme = THEMES[get_saved_theme()]
    accent = theme[platform]
    return (
        "QDialog { background: #202124; color: #f1f3f4; font-size: 14px; }"
        "QLabel#dialogTitle { font-size: 20px; font-weight: 700; }"
        "QPushButton { background: " + accent["accent"] + "; border: 0; "
        "border-radius: 16px; padding: 9px 16px; font-weight: 600; }"
        "QPushButton:hover { background: " + accent["hover"] + "; }"
        "QPushButton:disabled { background: #45474b; color: #9aa0a6; }"
        "QPushButton#skipButton { background: transparent; color: #aeb4bd; "
        "border: 1px solid " + theme["border"] + "; }"
        "QPushButton#skipButton:hover { background: " + theme["line"] + "; "
        "color: #ffffff; }"
    )


class GameFolderPicker(QDialog):
    """First-run gate: pick the game folder UTT will work from."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_path = ""
        self.setWindowTitle("Select your game location")
        self.setModal(True)
        self.setMinimumWidth(560)
        platform = get_saved_platform() or "xbx"
        self.setStyleSheet(_dialog_theme_style(platform))

        layout = QVBoxLayout(self)
        title = QLabel("Select your game location")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        hint = (
            "Pick your RPCS3 folder — UTT finds the game in "
            "games/Skate_3_BLUS or games/Skate_3_BLES, then reads "
            "PS3_GAME/USRDIR/data/content."
            if platform == "ps3"
            else "Pick the folder that contains the game's data folder — "
            "UTT looks for the archive in data/content."
        )
        layout.addWidget(QLabel(
            "UTT needs to know where your game files are so it can find "
            "createacharacter.big and unpack your files into the right place. "
            + hint
        ))
        row = QHBoxLayout()
        self.path_label = QLabel("No game folder selected")
        self.path_label.setWordWrap(True)
        self.pick_button = QPushButton("Browse…")
        self.pick_button.clicked.connect(self.pick_folder)
        row.addWidget(self.path_label, 1)
        row.addWidget(self.pick_button)
        layout.addLayout(row)
        self.continue_button = QPushButton("Continue")
        self.continue_button.setEnabled(False)
        self.continue_button.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        skip_all = QPushButton("Skip all")
        skip_all.setObjectName("skipButton")
        skip_all.clicked.connect(self.reject)
        buttons.addWidget(skip_all)
        buttons.addStretch(1)
        buttons.addWidget(self.continue_button)
        layout.addLayout(buttons)
        layout.addWidget(QLabel(
            "Skip all opens UTT without a game folder — you can still browse "
            "the catalog, convert, and use the quick viewer. You can set up "
            "the game folder later from the Choose archive button."
        ))

    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select your game location", ""
        )
        if not folder:
            return
        self.selected_path = folder
        self.path_label.setText(folder)
        self.continue_button.setEnabled(True)


CONFIG_FILE_NAME = "utt_config.json"


def config_file() -> Path:
    return working_dir() / CONFIG_FILE_NAME


_config_cache: dict | None = None

_LEGACY_SETTING_FILES = (
    "platform.txt", "export_mode.txt", "theme.txt",
    "skip_update_version.txt", "skip_archive.txt", "skip_4k_warning.txt",
)


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""


def _load_config() -> dict:
    """Load the JSON settings file, migrating the old .txt files once."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    data = {}
    try:
        parsed = json.loads(config_file().read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            data = parsed
    except (OSError, ValueError):
        data = {}
    if not data:
        data = _migrate_legacy_config()
    _config_cache = data
    return data


def _migrate_legacy_config() -> dict:
    data = {}
    folder = working_dir()
    platform = _read_text_file(folder / "platform.txt")
    if platform in PLATFORMS:
        data["platform"] = platform
    export_mode = _read_text_file(folder / "export_mode.txt")
    if export_mode in ("bones", "mesh"):
        data["export_mode"] = export_mode
    theme = _read_text_file(folder / "theme.txt")
    if theme in THEMES:
        data["theme"] = theme
    skip_update = _read_text_file(folder / "skip_update_version.txt")
    if skip_update:
        data["skip_update_version"] = skip_update
    if (folder / "skip_archive.txt").is_file():
        data["skip_archive"] = True
    if (folder / "skip_4k_warning.txt").is_file():
        data["skip_4k_warning"] = True
    if data:
        _write_config(data)
        for name in _LEGACY_SETTING_FILES:
            try:
                (folder / name).unlink()
            except OSError:
                pass
    return data


def _write_config(data: dict) -> None:
    global _config_cache
    try:
        config_file().write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    _config_cache = data


def get_saved_platform() -> str:
    value = _load_config().get("platform", "")
    return value if value in PLATFORMS else ""


def save_platform(platform: str) -> None:
    if platform not in PLATFORMS:
        return
    data = dict(_load_config())
    data["platform"] = platform
    _write_config(data)


def get_keep_files_packed() -> bool | None:
    """Return True (keep packed), False (keep unpacked), or None when unset."""
    value = _load_config().get("keep_files_packed")
    if value is None:
        return None
    return bool(value)


def save_keep_files_packed(packed: bool) -> None:
    data = dict(_load_config())
    data["keep_files_packed"] = bool(packed)
    _write_config(data)


def get_game_folder(platform: str | None = None) -> str:
    """Return the saved game folder path for *platform* ('' when unset)."""
    platform = platform or get_saved_platform()
    data = _load_config()
    folders = data.get("game_folders")
    if isinstance(folders, dict):
        value = folders.get(platform, "")
        if isinstance(value, str):
            return value
    # Legacy single-platform value saved before per-platform folders.
    legacy = data.get("game_folder")
    if platform == "xbx" and isinstance(legacy, str):
        return legacy
    return ""


def save_game_folder(folder: str, platform: str | None = None) -> None:
    platform = platform or get_saved_platform()
    data = dict(_load_config())
    folders = data.get("game_folders")
    if not isinstance(folders, dict):
        folders = {}
    if folder:
        folders[platform] = str(Path(folder))
    else:
        folders.pop(platform, None)
    data["game_folders"] = folders
    data.pop("game_folder", None)  # drop the legacy single-platform value
    _write_config(data)


def get_rpcs3_folder(platform: str | None = None) -> str:
    """Return the saved RPCS3 root folder for *platform* ('' when unset).

    Kept separate from the game folder because when RPCS3 detection fails UTT
    saves a manually-chosen game folder instead, but the install-folder
    cleanup still needs the RPCS3 root to find dev_hdd0.
    """
    platform = platform or get_saved_platform()
    data = _load_config()
    folders = data.get("rpcs3_folders")
    if isinstance(folders, dict):
        value = folders.get(platform, "")
        if isinstance(value, str):
            return value
    return ""


def save_rpcs3_folder(folder: str, platform: str | None = None) -> None:
    platform = platform or get_saved_platform()
    data = dict(_load_config())
    folders = data.get("rpcs3_folders")
    if not isinstance(folders, dict):
        folders = {}
    if folder:
        folders[platform] = str(Path(folder))
    else:
        folders.pop(platform, None)
    data["rpcs3_folders"] = folders
    _write_config(data)


def get_skipped_setup() -> bool:
    """True when the user skipped all first-run setup (Skip all)."""
    return bool(_load_config().get("skipped_setup"))


def save_skipped_setup(skipped: bool) -> None:
    data = dict(_load_config())
    if skipped:
        data["skipped_setup"] = True
    else:
        data.pop("skipped_setup", None)
    _write_config(data)


def get_always_unpack() -> bool:
    """True when the startup unpack prompt should be skipped (always unpack)."""
    return bool(_load_config().get("always_unpack"))


def save_always_unpack(value: bool) -> None:
    data = dict(_load_config())
    if value:
        data["always_unpack"] = True
    else:
        data.pop("always_unpack", None)
    _write_config(data)


def get_saved_export_mode() -> str:
    """Return "bones", "mesh", or "" when no default is saved."""
    value = _load_config().get("export_mode", "")
    return value if value in ("bones", "mesh") else ""


def save_export_mode(mode: str) -> None:
    data = dict(_load_config())
    if mode not in ("bones", "mesh"):
        data.pop("export_mode", None)
    else:
        data["export_mode"] = mode
    _write_config(data)


def get_saved_theme() -> str:
    value = _load_config().get("theme", "")
    return value if value in THEMES else DEFAULT_THEME


def save_theme(name: str) -> None:
    if name not in THEMES:
        return
    data = dict(_load_config())
    data["theme"] = name
    _write_config(data)


def get_theme(name: str | None = None) -> dict:
    return THEMES[name or get_saved_theme()]


def get_skipped_update_version() -> str:
    """Return the version the user asked to never be prompted about."""
    return _load_config().get("skip_update_version", "")


def save_skipped_update_version(version: str) -> None:
    data = dict(_load_config())
    if not version:
        data.pop("skip_update_version", None)
    else:
        data["skip_update_version"] = version
    _write_config(data)


def get_skipped_archive() -> bool:
    """True once the user has skipped the createacharacter.big picker."""
    return bool(_load_config().get("skip_archive"))


def save_skipped_archive() -> None:
    data = dict(_load_config())
    data["skip_archive"] = True
    _write_config(data)


def get_skipped_4k_warning() -> bool:
    """True once the user asked to never see the 4K recomp warning again."""
    return bool(_load_config().get("skip_4k_warning"))


def save_skipped_4k_warning() -> None:
    data = dict(_load_config())
    data["skip_4k_warning"] = True
    _write_config(data)


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


def choose_pack_mode(parent=None) -> bool:
    """Return True once the user has picked a pack mode (or one is already saved)."""
    if get_keep_files_packed() is not None:
        return True
    picker = PackModePicker(parent)
    picker.exec()
    if picker.selected is not None:
        save_keep_files_packed(picker.selected)
        return True
    return False


def prompt_ps3_game_folder(parent=None) -> str:
    """Ask for the Skate 3 game folder manually when RPCS3 detection fails.

    Returns the chosen path, or '' when cancelled.
    """
    QMessageBox.information(
        parent,
        APP_TITLE,
        "UTT couldn't find Skate 3 (Skate_3_BLUS or Skate_3_BLES) under "
        "that RPCS3 folder.\n\nPlease select your game location manually — "
        "the PS3_GAME folder (the one that contains USRDIR/data/content).",
    )
    return QFileDialog.getExistingDirectory(parent, "Select your game location", "")


def resolve_ps3_game_selection(parent, folder: str) -> str:
    """Return the folder to save after a PS3 RPCS3 selection.

    When the RPCS3 root contains a standard Skate 3 title it is kept so the
    game path can be derived from it; otherwise the user picks the game
    folder (PS3_GAME) themselves and that path is saved instead.
    """
    if ps3_game_dir(folder) is None:
        manual = prompt_ps3_game_folder(parent)
        if manual:
            return manual
    return folder


def choose_game_folder(parent=None, platform: str | None = None) -> bool:
    """Return True when a game folder is chosen (or already saved).

    Returns False when the user skips all setup — the app then opens without
    a game folder or cache, and Choose archive can re-run this later.
    """
    platform = platform or get_saved_platform() or "xbx"
    if get_game_folder(platform):
        return True
    picker = GameFolderPicker(parent)
    picker.exec()
    if picker.selected_path:
        chosen = picker.selected_path
        if platform == "ps3":
            save_rpcs3_folder(chosen, platform)
            chosen = resolve_ps3_game_selection(parent, chosen)
        save_game_folder(chosen, platform)
        save_skipped_setup(False)
        return True
    save_skipped_setup(True)
    return False


def rx2_preview_image(path: Path, opaque: bool = False, alpha_mask: bool = False):
    """Decode the first decodable texture of an RX2 file into a PIL RGBA image.

    With opaque=True, any partially transparent pixel (alpha > 0) is raised
    to full opacity, mirroring PSGTx's "force visible pixels" cleanup.
    With alpha_mask=True, the alpha channel is replaced with the texture's
    own luminance (Alpha Mask), removing DXT5 alpha grain and transparent dots.
    The mask is applied first, then opaque, so both together leave the
    image fully opaque with the grain removed.
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
        if alpha_mask:
            image = apply_alpha_mask(image, image)
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


def reveal_in_explorer(path: Path):
    """Open the file's folder in Explorer with the file selected/highlighted.

    Uses SHOpenFolderAndSelectItems, which works even when the folder window
    is already open (plain ``explorer /select,`` can just focus the folder).
    Falls back to opening the parent folder when the file is missing or on
    non-Windows platforms.
    """
    path = Path(path).resolve()
    if sys.platform != "win32":
        subprocess.Popen(["xdg-open", str(path.parent)])
        return
    if not path.exists():
        folder = path.parent if path.parent.is_dir() else Path.home()
        os.startfile(str(folder))
        return
    try:
        import ctypes

        shell32 = ctypes.windll.shell32
        shell32.SHParseDisplayName.argtypes = [
            ctypes.c_wchar_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        shell32.SHParseDisplayName.restype = ctypes.c_long
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong,
        ]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
        ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

        def to_pidl(name: str):
            pidl = ctypes.c_void_p()
            attrs = ctypes.c_ulong()
            if shell32.SHParseDisplayName(
                ctypes.c_wchar_p(name), None,
                ctypes.byref(pidl), 0, ctypes.byref(attrs),
            ) != 0:
                return None
            return pidl

        folder_pidl = to_pidl(str(path.parent))
        file_pidl = to_pidl(str(path))
        if folder_pidl is None:
            os.startfile(str(path.parent))
            return
        try:
            if file_pidl is None:
                shell32.SHOpenFolderAndSelectItems(folder_pidl, 0, None, 0)
            else:
                items = (ctypes.c_void_p * 1)(file_pidl)
                shell32.SHOpenFolderAndSelectItems(folder_pidl, 1, items, 0)
        finally:
            for pidl in (folder_pidl, file_pidl):
                if pidl:
                    ctypes.windll.ole32.CoTaskMemFree(pidl)
    except Exception:
        subprocess.Popen(
            f'explorer /select,"{path}"',
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


class ArchivePicker(QDialog):
    """Optional first-run picker for createacharacter.big.

    Closing it (X or Escape) skips the cache and continues into the app.
    """
    def __init__(self, parent=None, platform: str = "ps3", unpacked: bool = False):
        super().__init__(parent)
        self.selected_path: Path | None = None
        self.setWindowTitle("Select createacharacter.big")
        self.setModal(True)
        self.setMinimumWidth(520)

        info = PLATFORMS[platform]
        self.setStyleSheet(_dialog_theme_style(platform))
        layout = QVBoxLayout(self)
        title = QLabel("Select your createacharacter.big file")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        if unpacked:
            dest_text = (
                "The archive will be unpacked into your game folder "
                "(data/content), where the game reads loose files."
            )
        else:
            dest_text = (
                "The archive will be extracted into a local cache folder beside "
                f"UTT, as .{info['extension']} files under cache\\{platform}."
            )
        layout.addWidget(QLabel(
            "UTT needs the game archive before it can list or preview textures. "
            f"{dest_text}\n\n"
            "You can close this window to skip it — the app still works, "
            "you just won't have textures or models from the archive. "
            "Skipping is remembered, so this window won't appear again "
            "on the next launches."
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


class UpdateDialog(QDialog):
    """Ask whether to install a newer UTT release found on GitHub."""

    def __init__(self, parent=None, current: str = "", release=None):
        super().__init__(parent)
        self.install = False
        self.dont_ask_again = False
        self.setWindowTitle("Update available")
        self.setModal(True)
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("A new version of UTT is available")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        version_row = QLabel(
            f"Current version: {current}   →   New version: {release['version']}"
        )
        version_row.setObjectName("convertDescription")
        layout.addWidget(version_row)

        if release.get("body"):
            layout.addWidget(QLabel("What's new:"))
            changelog = QPlainTextEdit()
            changelog.setReadOnly(True)
            changelog.setPlainText(release["body"])
            changelog.setMaximumHeight(200)
            layout.addWidget(changelog)

        self.skip_check = QCheckBox("Don't ask again about this version")
        layout.addWidget(self.skip_check)

        row = QHBoxLayout()
        row.addStretch(1)
        later = QPushButton("Later")
        later.clicked.connect(self.reject)
        install_button = QPushButton("Update now")
        install_button.clicked.connect(self.accept)
        row.addWidget(later)
        row.addWidget(install_button)
        layout.addLayout(row)

    def accept(self):
        self.install = True
        self.dont_ask_again = self.skip_check.isChecked()
        super().accept()


class _UpdateDownloadWorker(QObject):
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, url: str, dest: str, expected_size: int = 0):
        super().__init__()
        self.url = url
        self.dest = dest
        self.expected_size = expected_size
        self.cancel_requested = False

    def run(self):
        try:
            def on_progress(done, total):
                self.progress.emit(done, total if total else -1)
                return not self.cancel_requested

            updater.download_file(
                self.url,
                self.dest,
                expected_size=self.expected_size,
                progress=on_progress,
            )
            self.finished.emit(self.dest)
        except updater.UpdateCancelled:
            self.cancelled.emit()
        except Exception:
            self.failed.emit(traceback.format_exc())


class UpdateDownloadDialog(QDialog):
    """Modal progress dialog for downloading the update installer."""

    def __init__(self, parent=None, url: str = "", dest: str = "",
                 expected_size: int = 0):
        super().__init__(parent)
        self.result_path: Path | None = None
        self.error: str | None = None
        self.cancelled = False
        self.setWindowTitle("Downloading update")
        self.setModal(True)
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("Downloading the update…")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        self.file_label = QLabel(Path(dest).name)
        self.file_label.setObjectName("convertDescription")
        layout.addWidget(self.file_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        layout.addWidget(self.progress_bar)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_clicked)
        layout.addWidget(self.cancel_button)

        self._thread = QThread(self)
        self._worker = _UpdateDownloadWorker(url, dest, expected_size)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._worker.cancelled.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_progress(self, done: int, total: int):
        if total > 0:
            if self.progress_bar.maximum() == 0:
                self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(int(done * 100 / total))
        else:
            self.progress_bar.setRange(0, 0)

    def _on_finished(self, path):
        self.result_path = Path(path)
        self.accept()

    def _on_failed(self, details: str):
        self.error = details
        self.reject()

    def _on_cancelled(self):
        self.cancelled = True
        self.reject()

    def _cancel_clicked(self):
        self.cancel_button.setEnabled(False)
        self._worker.cancel_requested = True


class GradientDialog(QDialog):
    """Frameless dialog with a grey-to-black gradient background.

    Rounded corners are drawn with a translucent window; the gradient is
    painted directly, so there is no snapshot or timing involved.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setModal(True)
        self.setStyleSheet("QDialog, QLabel, QFrame { background: transparent; }")
        self._theme = None

    def set_theme(self, theme: dict):
        self._theme = theme
        if self.isVisible():
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 18, 18)
        painter.save()
        painter.setClipPath(path)
        theme = self._theme or get_theme()
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor(theme["gradient"][0]))
        gradient.setColorAt(1.0, QColor(theme["gradient"][1]))
        painter.fillRect(self.rect(), gradient)
        painter.restore()
        painter.setPen(QPen(QColor(120, 124, 132, 150), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        super().paintEvent(event)


class TextureZoomPreview(QWidget):
    """Texture preview with smooth, cursor-anchored mouse wheel zoom.

    Paints everything itself: the gradient panel (with the dashed border)
    always fills the whole preview area — no box appears around the image
    when zooming. Scroll the wheel to zoom toward the mouse position,
    drag with the left button to pan, double-click to reset to fit.
    Images re-render on resize, so a preview always fits its window even
    when it was drawn before the surrounding page finished laying out.
    """

    ZOOM_MIN = 1.0  # 1.0 = fit to window
    ZOOM_MAX = 8.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw_image = None      # PIL RGBA (kept for dimensions)
        self._qimage = None         # QImage copy for painting
        self._zoom = 1.0            # 1.0 = fit to window
        self._offset = QPointF(0, 0)  # pan in screen pixels (0 = centered)
        self._message = "Select a texture to preview"
        self._dragging = False
        self._drag_last = QPointF()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    # -- public API -----------------------------------------------------

    def set_message(self, text: str) -> None:
        self._message = text
        self._raw_image = None
        self._qimage = None
        self._zoom = 1.0
        self._offset = QPointF(0, 0)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def set_image(self, image) -> None:
        rgba = image.convert("RGBA")
        if self._raw_image is None or rgba.size != self._raw_image.size:
            self._zoom = 1.0
            self._offset = QPointF(0, 0)
        self._raw_image = rgba
        self._qimage = ImageQt(rgba)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()

    def fit_to_window(self) -> None:
        self._zoom = 1.0
        self._offset = QPointF(0, 0)
        self.update()

    # -- zooming / panning ---------------------------------------------

    def wheelEvent(self, event):
        if self._qimage is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        new_zoom = min(self.ZOOM_MAX, max(self.ZOOM_MIN, self._zoom * 1.2 ** (delta / 120.0)))
        if new_zoom == self._zoom:
            event.accept()
            return
        mouse = event.position()
        # Keep the image pixel under the cursor in place while zooming.
        old_scale = self._fit_scale() * self._zoom
        new_scale = self._fit_scale() * new_zoom
        if old_scale > 0:
            old_origin = self._origin()
            image_point = (mouse - old_origin) / old_scale
            new_origin = mouse - image_point * new_scale
            self._offset = new_origin - self._centered_origin(new_zoom)
        else:
            self._offset = QPointF(0, 0)
        self._zoom = new_zoom
        if self._zoom == self.ZOOM_MIN:
            self._offset = QPointF(0, 0)
        event.accept()
        self.update()

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self._qimage is not None):
            self._dragging = True
            self._drag_last = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging and self._qimage is not None:
            pos = event.position()
            self._offset += pos - self._drag_last
            self._drag_last = pos
            self.update()
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.fit_to_window()
            event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update()

    # -- painting -------------------------------------------------------

    def _fit_scale(self) -> float:
        if self._raw_image is None:
            return 1.0
        rect = self.rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return 1.0
        return min(
            rect.width() / self._raw_image.width,
            rect.height() / self._raw_image.height,
        )

    def _centered_origin(self, zoom: float) -> QPointF:
        scale = self._fit_scale() * zoom
        width = self._raw_image.width * scale
        height = self._raw_image.height * scale
        rect = self.rect()
        return QPointF(
            (rect.width() - width) / 2.0,
            (rect.height() - height) / 2.0,
        )

    def _origin(self) -> QPointF:
        return self._centered_origin(self._zoom) + self._offset

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        theme = get_theme()
        gradient = QLinearGradient(0, 0, 0, rect.height())
        gradient.setColorAt(0.0, QColor(theme["gradient"][0]))
        gradient.setColorAt(1.0, QColor(theme["gradient"][1]))
        painter.fillRect(rect, gradient)

        if self._qimage is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            scale = self._fit_scale() * self._zoom
            width = max(1, int(self._raw_image.width * scale))
            height = max(1, int(self._raw_image.height * scale))
            origin = self._origin()
            target = QRectF(origin, QSizeF(width, height))
            painter.drawImage(target, self._qimage)
        else:
            painter.setPen(QColor("#aeb4bd"))
            painter.drawText(
                rect.adjusted(12, 12, -12, -12),
                int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
                self._message,
            )

        painter.setPen(QPen(QColor(theme["outline"]), 1, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(
            QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 16, 16
        )


class MainWindow(QMainWindow):
    def __init__(self, model_loader, platform: str = "ps3"):
        super().__init__()
        self.model_loader = model_loader
        self.platform = platform
        self.platform_info = PLATFORMS[platform]
        self.theme_name = get_saved_theme()
        theme = get_theme(self.theme_name)
        self.gradient_top, self.gradient_bottom = theme["gradient"]
        self.surface = theme["surface"]
        self.border = theme["border"]
        self.line = theme["line"]
        self.outline = theme["outline"]
        self.card = theme["card"]
        self.accent = theme[platform]["accent"]
        self.accent_hover = theme[platform]["hover"]
        self.accent_dark = theme[platform]["dark"]
        self.accent_soft = theme[platform]["soft"]
        self.psg_extension = self.platform_info["extension"]
        self.root_dir = resource_dir()
        self.user_dir = working_dir()
        self.assets_dir = self.root_dir / "assets"
        self.keep_unpacked = get_keep_files_packed() is False
        self.game_folder = (
            Path(get_game_folder(self.platform))
            if get_game_folder(self.platform)
            else None
        )
        self._clean_ps3_install_folders()
        self.drawing_from_game = (
            self.game_folder is not None
            and self._has_loose_content(content_dir(self.game_folder, self.platform))
        )
        self.cache_dir = self._resolve_cache_dir()
        self.content_root = self._resolve_content_root()
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
        self._thread: QThread | None = None
        self._threads: list[QThread] = []
        self._workers: list[Worker] = []
        self.unpack_progress: QProgressDialog | None = None
        self.repack_progress: QProgressDialog | None = None
        self._rpcs3 = None
        self._attached = None
        self._character_items: list = []
        self._character_from_save = False
        self._character_model_hex = ""
        self._character_texture: PSGTx | None = None
        self._character_rx2_image = None
        self._last_character_export: str | None = None
        self._update_checking = False

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
        if getattr(sys, "frozen", False):
            QTimer.singleShot(1500, self._check_for_updates)

    def _resolve_cache_dir(self) -> Path:
        """Where UTT reads and writes game files.

        Unpacked mode always draws from the game folder's content directory.
        Packed mode draws from the game folder too whenever loose
        createacharacter + recipe folders are already there; otherwise it uses
        the local cache (cache/<platform>).
        """
        if self.game_folder:
            content = content_dir(self.game_folder, self.platform)
            if self.keep_unpacked or self._has_loose_content(content):
                return content
        return self.user_dir / "cache" / self.platform

    def _resolve_content_root(self) -> Path:
        """Where the createacharacter model folder lives for the current source."""
        if self.keep_unpacked or self.drawing_from_game:
            return self.cache_dir
        return self.cache_dir / "data" / "content"

    def _has_loose_content(self, content: Path) -> bool:
        """True when the content dir has loose createacharacter (with model
        files) and recipe folders the game reads."""
        return (
            content.is_dir()
            and any(content.rglob(f"*.{self.psg_extension}"))
            and (content / "recipe").is_dir()
        )

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
        self.browser_mode = AppComboBox()
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
        self.model_tree.setHeaderLabels([f"Model folder / {self.platform_info['label']} file"])
        self.model_tree.setMinimumWidth(300)
        self.model_tree.itemSelectionChanged.connect(self._on_texture_selected)
        self.model_tree.itemExpanded.connect(self._on_model_expanded)
        self.model_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.model_tree.customContextMenuRequested.connect(
            lambda pos: self._show_cache_menu(self.model_tree, pos)
        )

        self.preview_container = QWidget()
        self.preview_container.setObjectName("detailsPanel")
        preview_layout = QVBoxLayout(self.preview_container)
        preview_layout.setContentsMargins(20, 20, 20, 20)
        preview_layout.setSpacing(12)
        self.texture_preview = TextureZoomPreview()
        self.texture_preview.setMinimumSize(430, 380)
        preview_layout.addWidget(self.texture_preview, 1)
        self.selected_label = QLabel("No texture selected")
        preview_layout.addWidget(self.selected_label)
        self.force_opaque = QCheckBox("Force visible pixels to 255 opacity")
        self.force_opaque.setToolTip(
            "Off preserves the alpha stored in the texture. On raises every "
            "visible pixel (alpha > 0) to full opacity."
        )
        self.force_opaque.toggled.connect(self._refresh_preview_alpha)
        preview_layout.addWidget(self.force_opaque)
        self.alpha_mask_checkbox = QCheckBox("Fix alpha grain (Alpha Mask)")
        self.alpha_mask_checkbox.setToolTip(
            "Replaces the alpha channel with the texture's brightness (the "
            "Alpha Mask Paint.NET plugin port). Removes DXT5 alpha grain and "
            "transparent dots. Applied before the opacity fix, so keeping both "
            "checked gives clean full opacity; dark texture areas only go "
            "translucent when this is on and the opacity fix is off. Disable "
            "for mask-style textures (decal2, blurmask)."
        )
        self.alpha_mask_checkbox.toggled.connect(self._refresh_preview_alpha)
        preview_layout.addWidget(self.alpha_mask_checkbox)
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

        self.model_preview = ModelPreview(file_label=self.platform_info["label"])
        self.model_preview.export_requested.connect(self._export_model)

        self.model_texture_page = QWidget()
        self.model_texture_page.setObjectName("detailsPanel")
        model_texture_layout = QVBoxLayout(self.model_texture_page)
        model_texture_layout.setContentsMargins(20, 20, 20, 20)
        model_texture_layout.setSpacing(12)
        self.model_texture_preview = TextureZoomPreview()
        self.model_texture_preview.setMinimumSize(430, 380)
        model_texture_layout.addWidget(self.model_texture_preview, 1)
        self.model_texture_label = QLabel("Select a model texture to preview")
        model_texture_layout.addWidget(self.model_texture_label)
        self.model_texture_force_opaque = QCheckBox("Force visible pixels to 255 opacity")
        self.model_texture_force_opaque.setToolTip(
            "Off preserves the alpha stored in the texture. On raises every "
            "visible pixel (alpha > 0) to full opacity."
        )
        self.model_texture_force_opaque.toggled.connect(self._refresh_model_texture_preview)
        model_texture_layout.addWidget(self.model_texture_force_opaque)
        self.model_texture_alpha_mask = QCheckBox("Fix alpha grain (Alpha Mask)")
        self.model_texture_alpha_mask.setToolTip(
            "Replaces the alpha channel with the texture's brightness (the "
            "Alpha Mask Paint.NET plugin port). Removes DXT5 alpha grain and "
            "transparent dots. Applied before the opacity fix, so keeping both "
            "checked gives clean full opacity; dark texture areas only go "
            "translucent when this is on and the opacity fix is off. Disable "
            "for mask-style textures (decal2, blurmask)."
        )
        self.model_texture_alpha_mask.toggled.connect(self._refresh_model_texture_preview)
        model_texture_layout.addWidget(self.model_texture_alpha_mask)
        self.model_texture_export_button = QPushButton("Export preview…")
        self.model_texture_export_button.setEnabled(False)
        self.model_texture_export_button.clicked.connect(self._export_model_texture)
        model_texture_layout.addWidget(self.model_texture_export_button)

        self.model_right_stack = QStackedWidget()
        self.model_right_stack.addWidget(self.model_preview)
        self.model_right_stack.addWidget(self.model_texture_page)
        self.model_splitter.addWidget(self.model_tree)
        self.model_splitter.addWidget(self.model_right_stack)
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
        self.convert_mode = AppComboBox()
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
            f"Drop or choose one or more images, set each alias, output "
            f"resolution and opacity, then convert them all at once."
        )
        description.setObjectName("convertDescription")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(description)

        body = QHBoxLayout()
        body.setSpacing(14)

        self._convert_job_cards: list[ConvertJobCard] = []
        self.convert_list = QListWidget()
        self.convert_list.setObjectName("convertList")
        self.convert_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.convert_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.convert_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.convert_list.setVerticalScrollMode(
            QListWidget.ScrollMode.ScrollPerPixel
        )
        self.convert_list.setSpacing(10)
        self.convert_list.setFrameShape(QFrame.Shape.NoFrame)
        self.convert_list.setMinimumHeight(200)
        body.addWidget(self.convert_list, 1)

        sidebar = QVBoxLayout()
        sidebar.setSpacing(12)
        sidebar.setContentsMargins(0, 0, 0, 0)
        self.image_drop_zone = ImageDropZone()
        self.image_drop_zone.setObjectName("imageDropZone")
        self.image_drop_zone.setMinimumHeight(0)
        self.image_drop_zone.setFixedHeight(110)
        self.image_drop_zone.clicked.connect(self._choose_image)
        self.image_drop_zone.imageDropped.connect(self._add_convert_images)
        sidebar.addWidget(self.image_drop_zone)

        output_row = QHBoxLayout()
        output_row.setSpacing(10)
        output_row.addWidget(QLabel("Output"))
        self.convert_output_input = QLineEdit(str(self.output_dir))
        self.convert_output_input.setToolTip(
            f"{self.platform_info['label']} files will be saved into this folder. "
            "Defaults to the exports folder."
        )
        output_row.addWidget(self.convert_output_input, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._choose_output_folder)
        output_row.addWidget(browse_btn)
        sidebar.addLayout(output_row)

        sidebar.addStretch(1)

        self.convert_button = QPushButton(
            f"Convert images to {self.platform_info['label']}"
        )
        self.convert_button.setObjectName("convertButton")
        self.convert_button.setEnabled(False)
        self.convert_button.clicked.connect(self._convert_image)
        sidebar.addWidget(self.convert_button)

        body.addLayout(sidebar)
        layout.addLayout(body, 1)

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
        """Live Skate 3 character items: read the recipe from the running game
        (RPCS3 for PS3; skate3recomp or Xenia for Xbox) and preview the worn
        models/textures from the local cache."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.attach_status = QLabel("Not attached")
        self.attach_status.setObjectName("status")
        header.addWidget(self.attach_status, 1)
        attach_btn = QPushButton("Attach")
        attach_btn.clicked.connect(self._attach_target)
        header.addWidget(attach_btn)
        self.character_scan_button = QPushButton("Scan")
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

        self.character_model_preview = ModelPreview(file_label=self.platform_info["label"])
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
        self.character_texture_preview = TextureZoomPreview()
        self.character_texture_preview.setMinimumSize(430, 380)
        texture_layout.addWidget(self.character_texture_preview, 1)
        self.character_force_opaque = QCheckBox("Force visible pixels to 255 opacity")
        self.character_force_opaque.setToolTip("Off preserves the alpha stored in the PSG. On applies PSGTx's alpha cleanup.")
        self.character_force_opaque.toggled.connect(self._refresh_character_texture_preview)
        texture_layout.addWidget(self.character_force_opaque)
        self.character_alpha_mask_checkbox = QCheckBox("Fix alpha grain (Alpha Mask)")
        self.character_alpha_mask_checkbox.setToolTip(
            "Replaces the alpha channel with the texture's brightness (the "
            "Alpha Mask Paint.NET plugin port). Removes DXT5 alpha grain and "
            "transparent dots. Applied before the opacity fix, so keeping both "
            "checked gives clean full opacity; dark texture areas only go "
            "translucent when this is on and the opacity fix is off. Disable "
            "for mask-style textures (decal2, blurmask)."
        )
        self.character_alpha_mask_checkbox.toggled.connect(self._refresh_character_texture_preview)
        texture_layout.addWidget(self.character_alpha_mask_checkbox)
        self.character_export_button = QPushButton("Export texture…")
        self.character_export_button.clicked.connect(self._export_character_texture)
        texture_layout.addWidget(self.character_export_button)
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
            QWidget { color: #f1f3f4; font-size: 14px; }
            QFrame#titleBar { background: transparent; border-bottom: 1px solid @line; }
            QFrame#titleBar QLabel { background: transparent; }
            QLabel#windowTitle { color: #d9dce1; font-size: 13px; font-weight: 600; }
            QLabel#versionLabel { color: #8a919c; font-size: 11px; margin-top: 2px; }
            QPushButton#creditsButton, QPushButton#settingsButton { background: transparent; color: #aeb4bd; border-radius: 8px; padding: 6px 11px; font-weight: 500; }
            QPushButton#creditsButton:hover, QPushButton#settingsButton:hover { background: @line; color: #ffffff; }
            QPushButton#titleWindowButton, QPushButton#titleCloseButton { background: transparent; border-radius: 0; padding: 0; font-size: 17px; font-weight: 400; }
            QPushButton#titleWindowButton:hover { background: @line; }
            QPushButton#titleCloseButton:hover { background: #c42b1c; }
            QTabWidget::pane { border: 0; border-top: 1px solid @line; }
            QTabBar::tab { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 @gradient_top, stop:1 @gradient_bottom); color: #c7ccd4; padding: 9px 26px; margin: 7px 5px; border: 1px solid @border; border-radius: 18px; }
            QTabBar::tab:hover { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3c3d42, stop:1 #0e0e10); color: #ffffff; }
            QTabBar::tab:selected { background: @accent; color: #ffffff; border: 1px solid @accent; }
            QPushButton { background: @accent; border: 0; border-radius: 16px; padding: 9px 16px; font-weight: 600; }
            QPushButton:hover { background: @accent_hover; }
            QPushButton:disabled { background: #45474b; color: #9aa0a6; }
            QLineEdit, QSpinBox, QDoubleSpinBox { background: @surface; border: 1px solid @border; border-radius: 9px; padding: 7px; }
            QTreeWidget { background: transparent; border: 1px solid @border; border-radius: 9px; padding: 7px; }
            QHeaderView::section { background: transparent; border: none; color: #aeb4bd; padding: 4px 6px; font-weight: 600; }
            QComboBox { background: @surface; border: 1px solid @border; border-radius: 9px; padding: 7px 10px; color: #f1f3f4; }
            QComboBox:hover { border: 1px solid @accent_soft; }
            QComboBox QAbstractItemView { background: transparent; border: none; color: #f1f3f4; padding: 4px; outline: none; }
            QComboBox QAbstractItemView::item { padding: 4px 8px; border-radius: 6px; }
            QComboBox QAbstractItemView::item:hover { background: @line; color: #ffffff; }
            QComboBox QAbstractItemView::item:selected { background: @accent_dark; color: #ffffff; }
            QComboBox QFrame { background: @surface; border: 1px solid @border; border-radius: 9px; }
            QComboBox::down-arrow { image: none; background: transparent; }
            QComboBox::drop-down { border: none; background: transparent; width: 24px; }
            #comboOverlay { background: @surface; border: 1px solid @border; border-radius: 9px; }
            QListWidget#comboOverlayList { background: transparent; border: none; outline: none; color: #f1f3f4; }
            QListWidget#comboOverlayList::item { padding: 4px 8px; border-radius: 6px; }
            QListWidget#comboOverlayList::item:hover { background: @line; color: #ffffff; }
            QListWidget#comboOverlayList::item:selected { background: @accent_dark; color: #ffffff; }
            QRadioButton { background: transparent; color: #f1f3f4; spacing: 8px; }
            QRadioButton::indicator { width: 16px; height: 16px; border: 1px solid @border; border-radius: 8px; background: @surface; }
            QRadioButton::indicator:hover { border-color: @accent_soft; }
            QRadioButton::indicator:checked { background: @accent_dark; border: 1px solid @accent_soft; }
            QTreeWidget::item { padding: 5px; border-radius: 6px; }
            QTreeWidget::item:selected { background: @accent_dark; }
            QLabel#preview { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 @gradient_top, stop:1 @gradient_bottom); border: 1px dashed @outline; border-radius: 16px; }
            QLabel#dialogTitle { font-size: 20px; font-weight: 700; }
            QLabel#status { color: #a9c7fa; }
            QWidget#detailsPanel { background: @surface; border-radius: 16px; }
            QFrame#card { background: @card; border: 1px solid @line; border-radius: 18px; }
            QLabel#convertTitle { font-size: 26px; font-weight: 700; }
            QLabel#convertDescription, QLabel#selectedImagePath, QLabel#modelDetails { color: #aeb4bd; }
            QLabel#imageDropZone { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 @gradient_top, stop:1 @gradient_bottom); border: 2px dashed @outline; border-radius: 16px; color: #aeb4bd; font-size: 17px; }
            QLabel#imageDropZone:hover { border-color: @accent_soft; color: #f1f3f4; }
            QLabel#modelTitle { font-size: 18px; font-weight: 700; }
            QLineEdit#aliasInput { font-family: Consolas; font-size: 16px; padding: 10px; }
            QPushButton#convertButton { font-size: 16px; padding: 12px 18px; }
            QPlainTextEdit#glbLog { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 @gradient_top, stop:1 @gradient_bottom); border: 1px solid @line; border-radius: 12px; font-family: Consolas; font-size: 12px; padding: 8px; }
            QSlider::groove:horizontal { height: 7px; background: @border; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: @accent_soft; border-radius: 3px; }
            QSlider::handle:horizontal { width: 18px; margin: -6px 0; background: #d2e3fc; border-radius: 9px; }
            QFrame#convertJobCard { background: @card; border: 1px solid @line; border-radius: 12px; }
            QLabel#jobFileName { font-size: 15px; font-weight: 700; }
            QLabel#jobFieldLabel { color: #8a93a0; font-size: 11px; }
            QLabel#jobResDisplay { font-size: 20px; font-weight: 700; color: @accent_soft; }
            QLabel#jobThumb { background: @surface; border: 1px solid @line; border-radius: 8px; }
            QLineEdit#jobAliasInput { font-family: Consolas; font-size: 13px; padding: 5px 8px; }
            QPushButton#resArrowButton { background: @surface; border: 1px solid @border; border-radius: 4px; qproperty-arrowColor: @accent_soft; }
            QPushButton#resArrowButton:hover { border-color: @accent_soft; }
            QPushButton#resArrowButton:pressed { background: @accent_dark; }
            QPushButton#jobRemoveButton { background: transparent; border: none; qproperty-removeColor: #aeb4bd; }
            QPushButton#jobRemoveButton:hover { background: rgba(255, 107, 107, 30); border-radius: 6px; }
            QListWidget#convertList { background: transparent; border: none; outline: none; }
            QListWidget#convertList::item { border: none; background: transparent; padding: 0px; }
            QListWidget#convertList::item:hover { background: transparent; }
            QListWidget#convertList::item:selected { background: transparent; }
            QListWidget#convertList QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
            QListWidget#convertList QScrollBar::handle:vertical { background: @accent_dark; border-radius: 4px; min-height: 24px; }
            QListWidget#convertList QScrollBar::handle:vertical:hover { background: @accent_soft; }
            QListWidget#convertList QScrollBar::add-line:vertical, QListWidget#convertList QScrollBar::sub-line:vertical { height: 0px; }
            QListWidget#convertList QScrollBar::add-page:vertical, QListWidget#convertList QScrollBar::sub-page:vertical { background: transparent; }
        """.replace("@gradient_top", self.gradient_top)
            .replace("@gradient_bottom", self.gradient_bottom)
            .replace("@surface", self.surface)
            .replace("@border", self.border)
            .replace("@line", self.line)
            .replace("@outline", self.outline)
            .replace("@card", self.card)
            .replace("@accent_hover", self.accent_hover)
            .replace("@accent_dark", self.accent_dark)
            .replace("@accent_soft", self.accent_soft)
            .replace("@accent", self.accent))

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
            self.texture_preview.setVisible(False)
            self.texture_export_button.setVisible(False)
            self.force_opaque.setVisible(False)
            self.alpha_mask_checkbox.setVisible(False)
            self.selected_label.setVisible(False)
        else:
            self.browser_stack.setCurrentWidget(self.texture_page)
            self.texture_tree.setHeaderLabels(["Texture category / hex ID"])
            self._load_catalog()
            self.texture_preview.setVisible(True)
            self.texture_export_button.setVisible(True)
            self.force_opaque.setVisible(True)
            self.alpha_mask_checkbox.setVisible(True)
            self.selected_label.setVisible(True)
            self.selected_label.setText("No texture selected")

    def _load_models_tree(self):
        """Build the model categories directly from the cached cas_db folders."""
        self.model_tree.clear()
        models_root = self.content_root / "createacharacter" / "model" / "cas_db"
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
                leaf.addChild(QTreeWidgetItem(["…"]))
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
        if get_skipped_setup() or not get_game_folder(self.platform):
            if not choose_game_folder(self, self.platform):
                return
            save_skipped_setup(False)
            choose_pack_mode(self)
            self._refresh_game_mode()
            if self._cache_ready():
                self._index_cache()
                return
            source = self._locate_game_source()
            if source is not None:
                self._extract_archive(source)
                return
        if self.drawing_from_game and not self.keep_unpacked:
            # Packed mode browsing loose files in the game folder. Offer to
            # switch to the cache-based loop: unpack a chosen archive into
            # the cache after removing the loose folders.
            answer = QMessageBox.question(
                self,
                APP_TITLE,
                f"UTT found loose folders which the {self.psg_extension}'s are "
                "already being drawn from. Choose your createacharacter.big to "
                "replace these loose folders with the cache.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            picker = ArchivePicker(self, self.platform, self.keep_unpacked)
            if (
                picker.exec() == QDialog.DialogCode.Accepted
                and picker.selected_path
            ):
                self._remove_loose_game_folders()
                self._refresh_game_mode()
                self._extract_archive(picker.selected_path)
            return
        if self.drawing_from_game:
            QMessageBox.information(
                self,
                APP_TITLE,
                "You're already browsing loose files in your game folder. "
                "Repack to pack them into createacharacter.big, or unpack "
                "into the cache from here later.",
            )
            return
        picker = ArchivePicker(self, self.platform, self.keep_unpacked)
        if picker.exec() == QDialog.DialogCode.Accepted and picker.selected_path:
            self._extract_archive(picker.selected_path)
        else:
            save_skipped_archive()

    def _request_repack(self):
        if self.drawing_from_game:
            if not self.game_folder:
                self._show_error("Set your game location in Settings before repacking.")
                return
            data_path = self.cache_dir
            repack_dir = game_root(self.game_folder, self.platform)
            data_rel_path = (
                "USRDIR/data/content" if self.platform == "ps3" else "data/content"
            )
            subdirs = ("createacharacter", "recipe")
            source_label = "createacharacter + recipe"
        else:
            data_path = self.cache_dir / "data"
            repack_dir = self.cache_dir
            data_rel_path = "data"
            subdirs = None
            source_label = "cache\\data"
        if not data_path.is_dir():
            self._show_error("Extract createacharacter.big before repacking it.")
            return

        self._protect_original_big()
        target_path = data_path / "createacharacter.big"
        message = (
            f"Repack every file under {source_label} into:\n\n"
            f"{target_path}\n\n"
            "Files are packed without compression for speed."
        )
        if target_path.exists():
            message += "\n\nAn existing archive at that location will be replaced."
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
            f"Packing {source_label}, please wait…", None, 0, 0, self
        )
        self.repack_progress.setWindowTitle(APP_TITLE)
        self.repack_progress.setWindowModality(
            Qt.WindowModality.ApplicationModal
        )
        self.repack_progress.setCancelButton(None)
        self.repack_progress.setMinimumDuration(0)
        self.repack_progress.setAutoClose(False)
        self.repack_progress.show()
        def task():
            result = self.archive_manager.repack(
                repack_dir, data_rel_path=data_rel_path, subdirs=subdirs
            )
            if self.drawing_from_game:
                self._remove_loose_game_folders()
            return result
        self._start_worker(task, self._repack_finished, self._repack_failed)

    def _protect_original_big(self):
        """Before repacking, move an original-sized .big in game/data/content
        into the Backup folder so it can never be overwritten."""
        if not self.game_folder:
            return
        content = content_dir(self.game_folder, self.platform)
        dest = content / "createacharacter.big"
        if dest.is_file() and is_original_big(dest, self.platform):
            backup_dir = content / "Backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            os.replace(dest, backup_dir / "createacharacter.big")

    def _place_archive_in_game(self, repacked_path: Path) -> Path:
        """Move a freshly repacked archive into game/data/content."""
        content = content_dir(self.game_folder, self.platform)
        content.mkdir(parents=True, exist_ok=True)
        dest = content / "createacharacter.big"
        if dest.exists():
            dest.unlink()
        shutil.move(str(repacked_path), str(dest))
        return dest

    def _remove_loose_game_folders(self):
        """Remove the loose createacharacter + recipe folders from the game
        folder's data/content. Used after packing them into an archive and
        when replacing loose files with the cache. The original archive stays
        safe in Backup."""
        if not self.game_folder:
            return
        content = content_dir(self.game_folder, self.platform)
        for folder in ("createacharacter", "recipe"):
            target = content / folder
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)

    def _repack_finished(self, result):
        if self.repack_progress:
            self.repack_progress.close()
            self.repack_progress = None
        self.repack_button.setEnabled(True)
        final_path = result.path
        if self.drawing_from_game:
            self._refresh_game_mode()
            self._index_cache()
        elif not self.keep_unpacked and self.game_folder:
            final_path = self._place_archive_in_game(result.path)
        size_mb = result.size / (1024 * 1024)
        self.archive_status.setText(
            f"Repacked {result.file_count:,} files — {size_mb:,.1f} MB"
        )
        QMessageBox.information(
            self,
            APP_TITLE,
            f"createacharacter.big created:\n{final_path}\n\n"
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
        if self._cache_ready():
            self.archive_status.setText("Loading existing texture cache…")
            self._start_worker(self._build_psg_index, self._archive_loaded)
        elif get_skipped_archive() or get_skipped_setup():
            pass
        else:
            source = self._locate_game_source()
            if source is not None:
                self._extract_archive(source)
            else:
                self._request_archive()

    def _cache_ready(self) -> bool:
        """True when UTT already has game files to browse.

        Unpacked Xbox mode additionally needs the recipe folder the game
        reads, so it is only considered ready when both are present.
        """
        if not self.cache_dir.is_dir():
            return False
        if self.keep_unpacked:
            return any(self.cache_dir.rglob(f"*.{self.psg_extension}")) and (
                self.cache_dir / "recipe"
            ).is_dir()
        return any(self.cache_dir.rglob(f"*.{self.psg_extension}"))

    def _locate_game_source(self) -> Path | None:
        """Find the archive to unpack in the saved game folder (loose or backup).

        Returns None when nothing is found, so the archive picker can ask the
        user to select createacharacter.big manually.
        """
        game_folder = get_game_folder(self.platform)
        if not game_folder:
            return None
        source = locate_or_backup_source(game_folder, self.platform)
        if source is not None:
            self.archive_status.setText(f"Found createacharacter.big: {source}")
        return source

    def _clean_ps3_install_folders(self):
        """Silently remove the contents of the PS3 INSTALL folders on launch.

        Only runs for PS3 (and only when an RPCS3 root is saved), so the
        installed copy under dev_hdd0/game/<serial>_INSTALL never shadows the
        loose files UTT manages in PS3_GAME/USRDIR/data/content. dev_hdd0 is
        a fixed RPCS3 path, so both serials are checked unconditionally.
        """
        if self.platform != "ps3":
            return
        rpcs3_root = get_rpcs3_folder(self.platform)
        if not rpcs3_root:
            return
        try:
            clean_ps3_install_folders(rpcs3_root)
        except OSError:
            pass

    def _refresh_game_mode(self):
        """Recompute mode-dependent paths after the setup gates are re-run."""
        self.keep_unpacked = get_keep_files_packed() is False
        self.game_folder = (
            Path(get_game_folder(self.platform))
            if get_game_folder(self.platform)
            else None
        )
        self.drawing_from_game = (
            self.game_folder is not None
            and self._has_loose_content(content_dir(self.game_folder, self.platform))
        )
        self.cache_dir = self._resolve_cache_dir()
        self.content_root = self._resolve_content_root()

    def _loose_repack_big(self) -> Path | None:
        """A non-original .big sitting loose in game/data/content (a repack)."""
        if not self.game_folder:
            return None
        loose = content_dir(self.game_folder, self.platform) / "createacharacter.big"
        if loose.is_file() and not is_original_big(loose, self.platform):
            return loose
        return None

    def _maybe_confirm_repack_unpack(self, archive: Path) -> bool:
        """Ask before unpacking a repacked .big that sits loose in the game folder.

        The untouched original is never prompted for — the size guard keeps it
        safe in Backup. Returns True to proceed with the unpack.
        """
        loose_repack = self._loose_repack_big()
        if loose_repack is None or get_always_unpack():
            return True
        if self.keep_unpacked:
            action = (
                "Unpack it into loose files so you can edit textures and "
                "models? The packed archive is removed once unpacked (you "
                "can always repack it again)."
            )
        else:
            action = (
                "Unpack it to the cache so you can edit textures and models? "
                "The packed archive stays in place — you edit in the cache "
                "and repack when you're ready."
            )
        box = QMessageBox(
            QMessageBox.Icon.Question,
            APP_TITLE,
            "A createacharacter.big is packed in your game folder.\n\n"
            f"{loose_repack}\n\n{action}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        always = QCheckBox("Always unpack without asking", box)
        box.setCheckBox(always)
        if box.exec() != QMessageBox.StandardButton.Yes:
            self._index_cache()
            self.archive_status.setText(
                "Packed archive left in place — click Choose archive to unpack it later"
            )
            return False
        if always.isChecked():
            save_always_unpack(True)
        return True

    def _extract_archive(self, archive: Path):
        if self.keep_unpacked:
            self._extract_archive_unpacked(archive)
            return
        if not self._maybe_confirm_repack_unpack(archive):
            return
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

    def _extract_archive_unpacked(self, archive: Path):
        """Unpack createacharacter + recipe straight into the game folder.

        Only the two subtrees the game reads are copied out, and existing
        loose files are never overwritten. When the archive is a repacked
        .big sitting loose in game/data/content, ask first (with an "always
        unpack" remember option) and delete it once unpacked — the loose
        folders now hold the same content, and the game would otherwise
        prefer the .big over them.
        """
        loose_repack = self._loose_repack_big()
        if not self._maybe_confirm_repack_unpack(archive):
            return
        self.archive_status.setText("Unpacking into game folder…")
        self.repack_button.setEnabled(False)
        self.unpack_progress = QProgressDialog(
            "Unpacking createacharacter.big into your game folder…", None, 0, 0, self
        )
        self.unpack_progress.setWindowTitle(APP_TITLE)
        self.unpack_progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.unpack_progress.setCancelButton(None)
        self.unpack_progress.setMinimumDuration(0)
        self.unpack_progress.setAutoClose(False)
        self.unpack_progress.show()

        def extract():
            unpack_into_game(
                self.assets_dir / "bigfile.exe", archive, self.game_folder,
                self.platform,
            )
            if loose_repack is not None:
                loose_repack.unlink(missing_ok=True)
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
        self._refresh_game_mode()
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
        if isinstance(alias, tuple):
            if mode == "textures":
                return
            kind = alias[0]
            if kind == "psg_model":
                path = Path(alias[1])
                self._populate_model_texture_children(items[0], path)
                self.current_model_path = path
                self.model_right_stack.setCurrentWidget(self.model_preview)
                self.model_preview.show_loading(path)
                self._start_worker(
                    lambda: (path, self.model_loader(path)),
                    self._model_loaded,
                )
                return
            if kind == "model_texture":
                hex_name, path = alias[1], alias[2]
                self._load_model_texture(
                    hex_name, Path(path) if path else None
                )
                return
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
        self.texture_preview.set_message("Loading preview…")
        path = matches[0]
        if path.suffix.lower() == ".rx2":
            self._rx2_path = path
            self._start_worker(
                lambda: rx2_preview_image(
                    path, self.force_opaque.isChecked(),
                    self.alpha_mask_checkbox.isChecked(),
                ),
                self._rx2_preview_loaded,
            )
            return
        self._rx2_path = None
        self._start_worker(lambda: PSGTx(str(path)), self._preview_loaded)

    def _on_model_expanded(self, item: QTreeWidgetItem):
        """Populate the model's texture children as soon as it is expanded."""
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and data[0] == "psg_model":
            self._populate_model_texture_children(item, Path(data[1]))

    def _clear_model_texture_children(self, node: QTreeWidgetItem):
        """Remove the child nodes under a model node (placeholder and textures)."""
        try:
            count = node.childCount()
        except RuntimeError:
            return
        for index in reversed(range(count)):
            node.removeChild(node.child(index))

    def _model_textures_for(self, path: Path) -> list | None:
        """Return the cached (channel, alias) texture list of a model file,
        or None when the file could not be read."""
        cache = getattr(self, "_model_textures_cache", None)
        if cache is None:
            cache = self._model_textures_cache = {}
        key = str(path)
        if key in cache:
            return cache[key]
        try:
            data = path.read_bytes()
        except OSError:
            cache[key] = None
            return None
        entries = extract_material_textures(data)
        cache[key] = entries
        return entries

    def _populate_model_texture_children(self, node: QTreeWidgetItem, path: Path):
        """Show the model's material textures as children of its tree node."""
        self._clear_model_texture_children(node)
        entries = self._model_textures_for(path)
        if entries is None:
            child = QTreeWidgetItem(node, ["Could not read the model file"])
            child.setData(
                0, Qt.ItemDataRole.UserRole, ("model_texture", None, None)
            )
        elif entries:
            for channel, texture_hex in entries:
                file_path = self._find_psg(texture_hex)
                child = QTreeWidgetItem(
                    node, [f"{channel}: {texture_hex}"]
                )
                child.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    (
                        "model_texture",
                        texture_hex,
                        str(file_path) if file_path is not None else None,
                    ),
                )
        else:
            child = QTreeWidgetItem(node, ["No textures found in the model file"])
            child.setData(
                0, Qt.ItemDataRole.UserRole, ("model_texture", None, None)
            )
        node.setExpanded(True)

    def _load_model_texture(self, hex_name: str | None, path: Path | None):
        """Load a model's material texture into the model texture preview."""
        self._model_texture_hex = hex_name
        self._model_texture = None
        self._model_texture_rx2_image = None
        self.model_right_stack.setCurrentWidget(self.model_texture_page)
        self.model_texture_export_button.setEnabled(False)
        if path is None:
            self.model_texture_label.setText(hex_name or "No texture selected")
            self.model_texture_preview.set_message(
                "Texture not found in cache — unpack createacharacter.big to preview."
            )
            return
        self.model_texture_label.setText(hex_name or "")
        self.model_texture_preview.set_message("Loading preview…")
        if path.suffix.lower() == ".rx2":
            self._start_worker(
                lambda: (hex_name, rx2_preview_image(path)),
                self._model_texture_loaded,
            )
        else:
            self._start_worker(
                lambda: (hex_name, PSGTx(str(path))),
                self._model_texture_loaded,
            )

    def _model_texture_loaded(self, result):
        hex_name, texture = result
        if hex_name != self._model_texture_hex:
            return
        if isinstance(texture, PSGTx):
            self._model_texture = texture
            self._model_texture_rx2_image = None
        else:
            self._model_texture = None
            self._model_texture_rx2_image = texture
        self.model_texture_export_button.setEnabled(True)
        self._refresh_model_texture_preview()

    def _refresh_model_texture_preview(self):
        image = None
        if self._model_texture_rx2_image is not None:
            image = self._model_texture_rx2_image
            if self.model_texture_alpha_mask.isChecked():
                image = apply_alpha_mask(image, image)
            if self.model_texture_force_opaque.isChecked():
                image = _force_opaque(image)
        elif self._model_texture is not None:
            image = self._model_texture.get_tx_image(
                self.model_texture_force_opaque.isChecked(),
                alpha_mask=self.model_texture_alpha_mask.isChecked(),
            )
        if image is None:
            return
        self.model_texture_preview.set_image(image)

    def _export_model_texture(self):
        if self._model_texture is None and self._model_texture_rx2_image is None:
            QMessageBox.information(self, APP_TITLE, "Load a texture before exporting.")
            return
        default = self.output_dir / f"{self._model_texture_hex}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export texture", str(default),
            "PNG (*.png);;JPEG (*.jpg *.jpeg);;BMP (*.bmp);;TIFF (*.tiff);;All files (*.*)",
        )
        if not path:
            return
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            if self._model_texture is not None:
                self._model_texture.export_tx(
                    path,
                    self.model_texture_force_opaque.isChecked(),
                    alpha_mask=self.model_texture_alpha_mask.isChecked(),
                )
            else:
                image = self._model_texture_rx2_image
                if self.model_texture_alpha_mask.isChecked():
                    image = apply_alpha_mask(image, image)
                if self.model_texture_force_opaque.isChecked():
                    image = _force_opaque(image)
                if path.lower().endswith((".jpg", ".jpeg")):
                    image = image.convert("RGB")
                image.save(path)
            QMessageBox.information(self, APP_TITLE, f"Exported:\n{path}")
        except Exception as exc:
            self._show_error(str(exc))

    def _model_loaded(self, result):
        path, model = result
        if path == self.current_model_path:
            self.model_preview.set_model(path, model)

    def _preview_loaded(self, texture: PSGTx):
        self.current_texture = texture
        self.current_image = texture.get_tx_image(
            self.force_opaque.isChecked(),
            alpha_mask=self.alpha_mask_checkbox.isChecked(),
        )
        self.texture_preview.set_image(self.current_image)

    def _rx2_preview_loaded(self, image):
        if image is None:
            self.current_image = None
            self.current_texture = None
            self.texture_preview.set_message(
                "This RX2 contains no decodable textures."
            )
            return
        self.current_image = image
        self.current_texture = None
        self.texture_preview.set_image(self.current_image)

    def _refresh_preview_alpha(self):
        if self.current_texture is not None:
            self._preview_loaded(self.current_texture)
            return
        rx2_path = getattr(self, "_rx2_path", None)
        if rx2_path is not None:
            self.texture_preview.set_message("Loading preview…")
            self._start_worker(
                lambda: rx2_preview_image(
                    rx2_path, self.force_opaque.isChecked(),
                    self.alpha_mask_checkbox.isChecked(),
                ),
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
                self.current_texture.export_tx(
                    path, self.force_opaque.isChecked(),
                    alpha_mask=self.alpha_mask_checkbox.isChecked(),
                )
            elif getattr(self, "_rx2_path", None) is not None:
                image = rx2_preview_image(
                    self._rx2_path,
                    self.force_opaque.isChecked(),
                    self.alpha_mask_checkbox.isChecked(),
                )
                if image is None:
                    raise RuntimeError("The RX2 could not be re-decoded for export.")
                if path.lower().endswith((".jpg", ".jpeg")):
                    image = image.convert("RGB")
                image.save(path)
            else:
                image = self.current_image
                if path.lower().endswith((".jpg", ".jpeg")):
                    image = image.convert("RGB")
                image.save(path)
            QMessageBox.information(self, APP_TITLE, f"Exported:\n{path}")
        except Exception as exc:
            self._show_error(str(exc))

    def _export_model(self, model, source_path, with_skin=False):
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
            lambda: export_gltf(model, path, with_skin=with_skin),
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

    def _attach_target(self):
        """Attach to the platform's game process: RPCS3 on PS3, skate3recomp
        or Xenia on Xbox (auto-detected)."""
        if self.platform == "xbx":
            try:
                proc, label = recipe.find_target()
            except recipe.GameNotFoundError as exc:
                self.attach_status.setText("Not attached")
                self._show_error(str(exc))
                return
            self._attached = proc
            self.attach_status.setText(f"Attached to {label} — process {proc.process_id}")
            return
        self._attach_rpcs3()

    def _attach_rpcs3(self):
        try:
            self._rpcs3 = recipe.find_rpcs3()
        except recipe.GameNotFoundError as exc:
            self.attach_status.setText("RPCS3 not attached")
            self._show_error(str(exc))
            return
        self.attach_status.setText(
            f"Attached to RPCS3 — process {self._rpcs3.process_id}"
        )

    def _character_output_dir(self) -> Path:
        return recipe.get_base_path() / ("output_xbx" if self.platform == "xbx" else "output")

    def _scan_character(self):
        self.character_scan_button.setEnabled(False)
        self.attach_status.setText("Scanning character…")
        self._start_worker(
            lambda: recipe.scan_and_save(self._character_output_dir(), self.platform),
            self._character_scan_finished,
            self._character_scan_failed,
        )

    def _character_scan_finished(self, result):
        self._populate_character_tree(result["items"], result["output_folder"])
        self.character_scan_button.setEnabled(True)
        self.attach_status.setText(
            f"Found {len(self._character_items)} items — saved to {result['txt_path']}"
        )

    def _populate_character_tree(self, items, output_folder):
        """Rebuild the character tree from scanned items (kept collapsed).

        Mirrors current_items.txt: every part gets a "model" branch with the
        hex id and a "textures" branch with every texture channel.
        """
        self._character_items = items
        self._character_output_folder = output_folder
        self.character_tree.clear()
        for item in items:
            part = QTreeWidgetItem([item["name"]])
            part.setData(0, Qt.ItemDataRole.UserRole, ("part", item))
            if not self._character_item_cached(item):
                part.setText(0, f"{item['name']}  (not in cache)")

            model_group = QTreeWidgetItem(["model"])
            model_hex = item.get("model")
            if model_hex:
                path = self._find_psg(model_hex)
                model = QTreeWidgetItem([model_hex])
                if path is not None:
                    model.setData(0, Qt.ItemDataRole.UserRole, ("model", model_hex, str(path)))
                else:
                    model.setText(0, f"{model_hex} — not found")
                    model.setData(0, Qt.ItemDataRole.UserRole, ("model", model_hex, None))
            else:
                model = QTreeWidgetItem(["not found"])
                model.setData(0, Qt.ItemDataRole.UserRole, ("model", None, None))
            model_group.addChild(model)
            part.addChild(model_group)

            textures_group = QTreeWidgetItem(["textures"])
            textures = item.get("textures", {})
            if textures:
                for channel, texture_hex in textures.items():
                    path = self._find_psg(texture_hex)
                    texture = QTreeWidgetItem([f"{channel}: {texture_hex}"])
                    if path is not None:
                        texture.setData(0, Qt.ItemDataRole.UserRole, ("texture", texture_hex, str(path)))
                    else:
                        texture.setText(0, f"{channel}: {texture_hex} — not found")
                        texture.setData(0, Qt.ItemDataRole.UserRole, ("texture", texture_hex, None))
                    textures_group.addChild(texture)
            else:
                missing = QTreeWidgetItem(["no textures"])
                missing.setData(0, Qt.ItemDataRole.UserRole, ("texture", None, None))
                textures_group.addChild(missing)
            part.addChild(textures_group)
            self.character_tree.addTopLevelItem(part)
        self.character_open_button.setEnabled(True)

    def _load_saved_character_items(self):
        """Reuse the previous scan when the platform's current_items.txt exists."""
        txt_path = self._character_output_dir() / "current_items.txt"
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
        self.attach_status.setText(
            f"Loaded {len(items)} items from current_items.txt — rescan to refresh"
        )

    def _character_scan_failed(self, details: str):
        self.character_scan_button.setEnabled(True)
        self.attach_status.setText("Scan failed")
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
                self._character_rx2_image = None
                self.character_texture_preview.set_message(
                    "Texture not found in cache — unpack createacharacter.big to preview."
                )
                self.character_selected_label.setText(hex_name or "")
                return
            self.character_texture_preview.set_message("Loading preview…")
            if Path(path).suffix.lower() == ".rx2":
                self._start_worker(
                    lambda: (hex_name, rx2_preview_image(Path(path))),
                    self._character_texture_loaded,
                )
            else:
                self._start_worker(
                    lambda: (hex_name, PSGTx(str(path))),
                    self._character_texture_loaded,
                )
            return
        model_hex, path = data[1], data[2]
        self._character_texture = None
        self._character_rx2_image = None
        self.character_texture_preview.set_message("Select a texture to preview")
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
        if isinstance(texture, PSGTx):
            self._character_texture = texture
            self._character_rx2_image = None
        else:
            self._character_texture = None
            self._character_rx2_image = texture
        self.character_selected_label.setText(hex_name)
        self.character_stack.setCurrentWidget(self.character_texture_page)
        self._refresh_character_texture_preview()

    def _refresh_character_texture_preview(self):
        image = None
        if self._character_rx2_image is not None:
            image = self._character_rx2_image
            if self.character_alpha_mask_checkbox.isChecked():
                image = apply_alpha_mask(image, image)
            if self.character_force_opaque.isChecked():
                image = _force_opaque(image)
        elif self._character_texture is not None:
            image = self._character_texture.get_tx_image(
                self.character_force_opaque.isChecked(),
                alpha_mask=self.character_alpha_mask_checkbox.isChecked(),
            )
        if image is None:
            return
        self.character_texture_preview.set_image(image)

    def _export_character_texture(self):
        if self._character_texture is None and self._character_rx2_image is None:
            QMessageBox.information(self, APP_TITLE, "Load a texture before exporting.")
            return
        default = self.output_dir / f"{self.character_selected_label.text()}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export texture", str(default),
            "PNG (*.png);;JPEG (*.jpg *.jpeg);;BMP (*.bmp);;TIFF (*.tiff);;All files (*.*)",
        )
        if not path:
            return
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            if self._character_texture is not None:
                self._character_texture.export_tx(
                    path, self.character_force_opaque.isChecked(),
                    alpha_mask=self.character_alpha_mask_checkbox.isChecked(),
                )
            else:
                image = self._character_rx2_image
                if self.character_alpha_mask_checkbox.isChecked():
                    image = apply_alpha_mask(image, image)
                if self.character_force_opaque.isChecked():
                    image = _force_opaque(image)
                if path.lower().endswith((".jpg", ".jpeg")):
                    image = image.convert("RGB")
                image.save(path)
            self._last_character_export = path
            QMessageBox.information(self, APP_TITLE, f"Exported:\n{path}")
        except Exception as exc:
            self._show_error(str(exc))

    def _open_character_output(self):
        """Open the output folder, selecting the last exported texture if any."""
        folder = self._character_output_dir()
        if not folder.is_dir():
            return
        last = getattr(self, "_last_character_export", None)
        target = Path(last) if last and Path(last).is_file() else folder
        reveal_in_explorer(target)

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
            elif kind == "model_texture":
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
        reveal_in_explorer(path)

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
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose images", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tiff *.tif *.webp *.gif);;All files (*.*)",
        )
        if paths:
            self._add_convert_images(paths)

    @staticmethod
    def _is_valid_alias(text: str) -> bool:
        text = text.strip()
        return (
            len(text) == HEX_LENGTH
            and text.startswith("0x")
            and all(character in "0123456789abcdefABCDEF" for character in text[2:])
        )

    def _add_convert_images(self, paths):
        queued = {card.path for card in self._convert_job_cards}
        added = 0
        for path in paths:
            resolved = str(Path(path).resolve())
            if resolved in queued or QPixmap(resolved).isNull():
                continue
            self._add_convert_job(resolved)
            queued.add(resolved)
            added += 1
        if added == 0:
            self._show_error("No supported images were added.")

    def _add_convert_job(self, path: str):
        card = ConvertJobCard(path)
        card.changed.connect(self._refresh_convert_ui)
        card.remove_requested.connect(self._remove_convert_job)
        self._convert_job_cards.append(card)
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 131))
        self.convert_list.addItem(item)
        self.convert_list.setItemWidget(item, card)
        self._refresh_convert_ui()

    def _remove_convert_job(self, card):
        if card not in self._convert_job_cards:
            return
        self._convert_job_cards.remove(card)
        for row in range(self.convert_list.count()):
            item = self.convert_list.item(row)
            if self.convert_list.itemWidget(item) is card:
                self.convert_list.takeItem(row)
                break
        card.deleteLater()
        self._refresh_convert_ui()

    def _refresh_convert_ui(self):
        count = len(self._convert_job_cards)
        label = self.platform_info["label"]
        self.convert_button.setText(
            f"Convert {count} image{'s' if count != 1 else ''} to {label}"
        )
        self.convert_button.setEnabled(
            count > 0
            and all(
                self._is_valid_alias(card.alias())
                for card in self._convert_job_cards
            )
        )

    def _convert_image(self):
        jobs = list(self._convert_job_cards)
        if not jobs:
            self._show_error("Choose an input image.")
            return
        invalid = [card for card in jobs if not self._is_valid_alias(card.alias())]
        if invalid:
            names = ", ".join(Path(card.path).name for card in invalid[:3])
            if len(invalid) > 3:
                names += ", …"
            self._show_error(f"Fix the hex name before exporting: {names}")
            return
        seen = {}
        duplicates = []
        for card in jobs:
            alias = card.alias().lower()
            if alias in seen:
                duplicates.append(alias)
            seen[alias] = card
        if duplicates:
            self._show_error(
                "Each image needs a unique hex name. "
                f"Duplicates: {', '.join(sorted(set(duplicates)))}"
            )
            return
        folder = self.convert_output_input.text().strip() or str(self.output_dir)
        big = [card for card in jobs if card.resolution_value > 2048]
        if big and self.platform == "xbx" and not get_skipped_4k_warning():
            names = ", ".join(Path(card.path).name for card in big[:3])
            if len(big) > 3:
                names += ", …"
            box = QMessageBox(
                QMessageBox.Icon.Warning, APP_TITLE,
                "4K RX2 exports use the extended header and will NOT load "
                "on a real Xbox 360 - they are for the PC recomp only.\n\n"
                f"{names}",
                QMessageBox.StandardButton.Ok, self,
            )
            never_again = QCheckBox("Don't show this warning again", box)
            box.setCheckBox(never_again)
            box.exec()
            if never_again.isChecked():
                save_skipped_4k_warning()
        self.convert_button.setEnabled(False)

        def task():
            outputs = []
            for card in jobs:
                alias = card.alias().lower()
                resolution = card.resolution_value
                opacity = card.opacity.value() / 100
                if self.platform == "xbx":
                    outputs.append(self._convert_image_rx2(
                        card.path, folder, alias, resolution, opacity,
                    ))
                else:
                    outputs.append(self.converter.convert_image(
                        card.path, folder, alias, resolution, opacity,
                    ))
            return outputs

        self._start_worker(task, self._conversion_finished)

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
                                  resolution, resolution, rgba,
                                  hash_name=alias)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        return str(output_path)

    def _conversion_finished(self, outputs):
        self._refresh_convert_ui()
        files = "\n".join(str(path) for path in outputs)
        QMessageBox.information(
            self, APP_TITLE,
            f"Created {len(outputs)} {self.platform_info['label']} file(s):\n{files}",
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
        self._refresh_convert_ui()
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
        dialog = GradientDialog(self)
        dialog.setWindowTitle("Settings")
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        title = QLabel("Settings")
        title.setObjectName("dialogTitle")
        header.addWidget(title)
        header.addStretch(1)
        close_button = QPushButton("✕")
        close_button.setObjectName("titleCloseButton")
        close_button.setFixedSize(30, 30)
        close_button.clicked.connect(dialog.reject)
        header.addWidget(close_button)
        layout.addLayout(header)

        tabs = QTabWidget()

        general = QWidget()
        general_layout = QVBoxLayout(general)
        general_layout.setContentsMargins(4, 8, 4, 4)
        general_layout.setSpacing(14)

        platform_row = QHBoxLayout()
        platform_row.setSpacing(12)
        platform_row.addWidget(QLabel("Platform mode"))
        combo = AppComboBox()
        for key, info in PLATFORMS.items():
            combo.addItem(info["name"], key)
        combo.setCurrentIndex(combo.findData(self.platform))
        platform_row.addWidget(combo, 1)
        general_layout.addLayout(platform_row)

        note = QLabel(
            "Switching platform restarts UTT in that mode. Textures are read from "
            f"the {PLATFORMS[self.platform]['name']} cache; if no cache exists for the "
            "new platform you will be asked to extract its createacharacter.big."
        )
        note.setWordWrap(True)
        note.setObjectName("convertDescription")
        general_layout.addWidget(note)

        if self.platform in ("xbx", "ps3"):
            pack_header = QLabel("Game files")
            pack_header.setObjectName("dialogTitle")
            general_layout.addWidget(pack_header)

            game_row = QHBoxLayout()
            game_row.setSpacing(12)
            game_row.addWidget(
                QLabel("RPCS3 location" if self.platform == "ps3" else "Game location")
            )
            settings_game_folder = get_game_folder(self.platform)
            settings_rpcs3_folder = get_rpcs3_folder(self.platform)
            self.game_folder_label = QLabel(settings_game_folder or "Not set")
            self.game_folder_label.setWordWrap(True)
            game_row.addWidget(self.game_folder_label, 1)
            game_browse = QPushButton("Change…")

            def _pick_game_folder():
                nonlocal settings_game_folder, settings_rpcs3_folder
                dialog_title = (
                    "Select your RPCS3 folder"
                    if self.platform == "ps3"
                    else "Select your game location"
                )
                folder = QFileDialog.getExistingDirectory(
                    self, dialog_title, settings_game_folder or ""
                )
                if folder:
                    if self.platform == "ps3":
                        settings_rpcs3_folder = folder
                        folder = resolve_ps3_game_selection(self, folder)
                    settings_game_folder = folder
                    self.game_folder_label.setText(folder or "Not set")

            game_browse.clicked.connect(_pick_game_folder)
            game_row.addWidget(game_browse)
            general_layout.addLayout(game_row)

            pack_row = QHBoxLayout()
            pack_row.setSpacing(12)
            pack_row.addWidget(QLabel("How UTT keeps your game files"))
            self.pack_mode_combo = AppComboBox()
            self.pack_mode_combo.addItem("Keep files packed", True)
            self.pack_mode_combo.addItem("Keep files unpacked", False)
            saved_pack = get_keep_files_packed()
            self.pack_mode_combo.setCurrentIndex(0 if saved_pack is not False else 1)
            pack_row.addWidget(self.pack_mode_combo, 1)
            general_layout.addLayout(pack_row)

            if self.platform == "xbx":
                pack_note_text = (
                    "Keep files packed is best for Xbox consoles. Keep files "
                    "unpacked is for the PC recomp and makes replacing "
                    "textures and models easier."
                )
            else:
                pack_note_text = (
                    "Keep files packed is best for PS3. Keep files unpacked "
                    "reads loose files directly and makes replacing textures "
                    "and models easier."
                )
            pack_note = QLabel(pack_note_text)
            pack_note.setWordWrap(True)
            pack_note.setObjectName("convertDescription")
            general_layout.addWidget(pack_note)

            self.always_unpack_check = QCheckBox(
                "Always unpack a packed archive without asking"
            )
            self.always_unpack_check.setChecked(get_always_unpack())
            general_layout.addWidget(self.always_unpack_check)

            separator = QFrame()
            separator.setFrameShape(QFrame.Shape.HLine)
            separator.setStyleSheet("color: #3c4043;")
            general_layout.addWidget(separator)

        export_header = QLabel("glTF export")
        export_header.setObjectName("dialogTitle")
        general_layout.addWidget(export_header)

        saved_mode = get_saved_export_mode()
        self.export_bones_check = QCheckBox("Always export glTF with bones")
        self.export_bones_check.setChecked(saved_mode == "bones")
        self.export_bones_check.toggled.connect(
            lambda checked: checked and self.export_mesh_check.setChecked(False)
        )
        self.export_mesh_check = QCheckBox("Always export glTF mesh")
        self.export_mesh_check.setChecked(saved_mode == "mesh")
        self.export_mesh_check.toggled.connect(
            lambda checked: checked and self.export_bones_check.setChecked(False)
        )
        general_layout.addWidget(self.export_bones_check)
        general_layout.addWidget(self.export_mesh_check)

        export_note = QLabel(
            "When a model with a skeleton is loaded, the export mode is "
            "preselected from this choice. You can still change it per export."
        )
        export_note.setWordWrap(True)
        export_note.setObjectName("convertDescription")
        general_layout.addWidget(export_note)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("color: #3c4043;")
        general_layout.addWidget(separator)

        update_row = QHBoxLayout()
        update_row.setSpacing(12)
        update_row.addWidget(QLabel(f"Version {APP_VERSION}"))
        update_row.addStretch(1)
        check_button = QPushButton("Check for updates")
        check_button.clicked.connect(lambda: self._check_for_updates(manual=True))
        update_row.addWidget(check_button)
        general_layout.addLayout(update_row)
        tabs.addTab(general, "General")

        appearance = QWidget()
        appearance_layout = QVBoxLayout(appearance)
        appearance_layout.setContentsMargins(4, 8, 4, 4)
        appearance_layout.setSpacing(14)

        appearance_header = QLabel("Gradient theme")
        appearance_header.setObjectName("dialogTitle")
        appearance_layout.addWidget(appearance_header)

        appearance_note = QLabel(
            "Choose the background gradient for UTT. The PS3 accent (or Xbox "
            "accent in Xbox mode) changes to match the theme you pick."
        )
        appearance_note.setWordWrap(True)
        appearance_note.setObjectName("convertDescription")
        appearance_layout.addWidget(appearance_note)

        selected_theme = get_saved_theme()
        theme_radios: dict[str, QRadioButton] = {}
        ps3_swatch = QFrame()
        ps3_swatch.setFixedSize(26, 26)
        xbx_swatch = QFrame()
        xbx_swatch.setFixedSize(26, 26)

        def update_accent_swatches(name: str):
            theme = THEMES[name]
            ps3_swatch.setStyleSheet(
                f"QFrame {{ background: {theme['ps3']['accent']}; "
                f"border: 1px solid {theme['border']}; border-radius: 6px; }}"
            )
            xbx_swatch.setStyleSheet(
                f"QFrame {{ background: {theme['xbx']['accent']}; "
                f"border: 1px solid {theme['border']}; border-radius: 6px; }}"
            )

        def on_theme_toggled(checked: bool, name: str):
            if not checked:
                return
            update_accent_swatches(name)
            dialog.set_theme(THEMES[name])
            update_radio_indicators(name)

        def update_radio_indicators(name: str):
            theme = THEMES[name]
            accent = theme[self.platform]
            base = (
                "QRadioButton { background: transparent; color: #f1f3f4; spacing: 8px; }"
                f"QRadioButton::indicator {{ width: 16px; height: 16px; "
                f"border: 1px solid {theme['border']}; border-radius: 8px; "
                f"background: {theme['surface']}; }}"
            )
            accent_style = (
                f"QRadioButton::indicator:hover {{ border-color: {accent['soft']}; }}"
                f"QRadioButton::indicator:checked {{ background: {accent['dark']}; "
                f"border: 1px solid {accent['soft']}; }}"
            )
            for radio in theme_radios.values():
                radio.setStyleSheet(base + accent_style)

        for name, theme in THEMES.items():
            row = QHBoxLayout()
            row.setSpacing(10)
            radio = QRadioButton(theme["name"])
            radio.setChecked(name == selected_theme)
            swatch = QFrame()
            swatch.setFixedSize(72, 22)
            gradient_top, gradient_bottom = theme["gradient"]
            swatch.setStyleSheet(
                "QFrame { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                f"stop:0 {gradient_top}, stop:1 {gradient_bottom}); "
                f"border: 1px solid {theme['border']}; border-radius: 5px; }}"
            )
            radio.toggled.connect(
                lambda checked, name=name: on_theme_toggled(checked, name)
            )
            row.addWidget(radio)
            row.addStretch(1)
            row.addWidget(swatch)
            appearance_layout.addLayout(row)
            theme_radios[name] = radio

        accent_row = QHBoxLayout()
        accent_row.setSpacing(10)
        accent_row.addWidget(QLabel("PS3 accent"))
        accent_row.addWidget(ps3_swatch)
        accent_row.addSpacing(14)
        accent_row.addWidget(QLabel("Xbox accent"))
        accent_row.addWidget(xbx_swatch)
        accent_row.addStretch(1)
        appearance_layout.addLayout(accent_row)
        update_accent_swatches(selected_theme)
        update_radio_indicators(selected_theme)
        dialog.set_theme(THEMES[selected_theme])

        reset_button = QPushButton("Reset to default")
        reset_button.clicked.connect(
            lambda: theme_radios[DEFAULT_THEME].setChecked(True)
        )
        appearance_layout.addWidget(reset_button, 0, Qt.AlignmentFlag.AlignRight)
        appearance_layout.addStretch(1)
        tabs.addTab(appearance, "Appearance")

        layout.addWidget(tabs)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(dialog.reject)
        apply_button = QPushButton("Apply")
        apply_button.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply_button)
        layout.addLayout(buttons)

        dialog.adjustSize()
        dialog.move(
            (self.width() - dialog.width()) // 2,
            (self.height() - dialog.height()) // 2,
        )

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        save_export_mode(
            "bones"
            if self.export_bones_check.isChecked()
            else "mesh" if self.export_mesh_check.isChecked() else ""
        )
        chosen_theme = next(
            name for name, radio in theme_radios.items() if radio.isChecked()
        )
        if chosen_theme != get_saved_theme():
            save_theme(chosen_theme)
            theme = get_theme(chosen_theme)
            self.theme_name = chosen_theme
            self.gradient_top, self.gradient_bottom = theme["gradient"]
            self.surface = theme["surface"]
            self.border = theme["border"]
            self.line = theme["line"]
            self.outline = theme["outline"]
            self.card = theme["card"]
            self.accent = theme[self.platform]["accent"]
            self.accent_hover = theme[self.platform]["hover"]
            self.accent_dark = theme[self.platform]["dark"]
            self.accent_soft = theme[self.platform]["soft"]
            self._apply_style()
            self.update()
        if self.platform in ("xbx", "ps3"):
            prev_mode = get_keep_files_packed()
            prev_folder = get_game_folder(self.platform)
            new_mode = bool(self.pack_mode_combo.currentData())
            save_keep_files_packed(new_mode)
            if settings_game_folder != prev_folder:
                save_game_folder(settings_game_folder, self.platform)
            if self.platform == "ps3" and settings_rpcs3_folder:
                save_rpcs3_folder(settings_rpcs3_folder, self.platform)
            save_always_unpack(self.always_unpack_check.isChecked())
            if new_mode != prev_mode or settings_game_folder != prev_folder:
                self._refresh_game_mode()
                if self._cache_ready():
                    self._start_worker(self._build_psg_index, self._archive_loaded)
                else:
                    self._index_cache()
        target = combo.currentData()
        if target == self.platform:
            return
        save_platform(target)
        self._restart_app()

    def _check_for_updates(self, manual: bool = False):
        """Look up the newest GitHub release; prompt when it is newer.

        Manual checks (Settings) always report the outcome. The startup
        check stays silent unless an update is actually available.
        """
        if self._update_checking:
            return
        self._update_checking = True
        self._start_worker(
            updater.fetch_latest,
            on_success=lambda release: self._update_fetch_done(release, manual),
            on_failure=lambda details: self._update_fetch_failed(details, manual),
        )

    def _update_fetch_done(self, release, manual: bool):
        self._update_checking = False
        if release is None:
            if manual:
                QMessageBox.information(
                    self, APP_TITLE,
                    "You're up to date — no newer version is available.",
                )
            return
        if not updater.is_newer(release["version"], APP_VERSION):
            if manual:
                QMessageBox.information(
                    self, APP_TITLE,
                    f"You're up to date — you are on the latest version "
                    f"({APP_VERSION}).",
                )
            return
        if not manual and get_skipped_update_version() == release["version"]:
            return
        dialog = UpdateDialog(self, APP_VERSION, release)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.dont_ask_again:
            save_skipped_update_version(release["version"])
        self._install_update(release)

    def _update_fetch_failed(self, _details: str, manual: bool):
        self._update_checking = False
        if manual:
            QMessageBox.warning(
                self, APP_TITLE,
                "Couldn't check for updates. Check your internet connection "
                "and try again.",
            )

    def _install_update(self, release):
        asset = updater.find_installer_asset(release)
        if asset is None:
            QMessageBox.warning(
                self, APP_TITLE,
                "The release doesn't include an installer file.\n"
                f"Open it in your browser: {release['html_url']}",
            )
            return
        destination = Path(tempfile.gettempdir()) / asset["name"]
        dialog = UpdateDownloadDialog(
            self, url=asset["url"], dest=str(destination),
            expected_size=asset["size"],
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.error is not None:
            QMessageBox.warning(
                self, APP_TITLE,
                "The update download failed.\n"
                + dialog.error.splitlines()[-1],
            )
            return
        self._apply_update(dialog.result_path)

    def _fresh_instance_env(self) -> dict:
        """Child env that spawns a new, independent PyInstaller instance.

        A frozen onefile app passes its private _PYI_* variables (including
        the extracted _MEI temp dir) to anything it spawns. The installer
        inherits them and passes them on to the new UTT.exe it launches, whose
        bootloader then mistakes itself for a worker of the old instance,
        reuses the old (already deleted) temp dir and fails with
        "Failed to load Python DLL ..._MEIxxxx\\python313.dll".
        Stripping those variables and setting PYINSTALLER_RESET_ENVIRONMENT
        forces a fresh top-level instance that unpacks its own temp dir.
        """
        env = os.environ.copy()
        for name in (
            "_MEIPASS2",
            "_PYI_ARCHIVE_FILE",
            "_PYI_APPLICATION_HOME_DIR",
            "_PYI_PARENT_PROCESS_LEVEL",
            "_PYI_SPLASH_IPC",
        ):
            env.pop(name, None)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        return env

    def _terminate_other_utts(self) -> list[int]:
        """Force-kill every other UTT.exe process before a silent update.

        A frozen onefile app runs as two processes: the bootloader parent
        (splash/Tcl) and the app child. After the app exits, the parent can
        intermittently hang in teardown, leaving a windowless zombie that
        keeps UTT.exe open indefinitely. Inno Setup's CloseApplications
        (RestartManager) cannot close such a process and aborts the silent
        update, so the installer never runs. This terminates all other
        UTT.exe processes — old zombies, hung bootloader parents, and any
        other running UTT window — so the installer can replace the file.
        """
        killed: list[int] = []
        if not getattr(sys, "frozen", False):
            return killed
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_TERMINATE = 0x0001
            TH32CS_SNAPPROCESS = 0x00000002

            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260),
                ]

            own_pid = os.getpid()
            snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snapshot == -1:
                return killed
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            try:
                if kernel32.Process32First(snapshot, ctypes.byref(entry)):
                    while True:
                        if (
                            entry.th32ProcessID != own_pid
                            and entry.szExeFile.decode(errors="ignore").lower()
                            == "utt.exe"
                        ):
                            handle = kernel32.OpenProcess(
                                PROCESS_TERMINATE, False, entry.th32ProcessID
                            )
                            if handle:
                                if kernel32.TerminateProcess(handle, 0):
                                    killed.append(entry.th32ProcessID)
                                kernel32.CloseHandle(handle)
                        if not kernel32.Process32Next(
                            snapshot, ctypes.byref(entry)
                        ):
                            break
            finally:
                kernel32.CloseHandle(snapshot)
        except Exception:
            pass
        return killed

    def _apply_update(self, installer_path):
        if not getattr(sys, "frozen", False):
            QMessageBox.information(
                self, APP_TITLE,
                "Running from source — the downloaded installer is ready at:\n"
                f"{installer_path}",
            )
            return
        try:
            self._terminate_other_utts()
            subprocess.Popen(
                [str(installer_path), "/VERYSILENT", "/SUPPRESSMSGBOXES"],
                env=self._fresh_instance_env(),
            )
        except OSError:
            QMessageBox.warning(
                self, APP_TITLE,
                f"Couldn't start the installer:\n{installer_path}",
            )
            return
        self.close()

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
            self._terminate_other_utts()
            subprocess.Popen(
                command,
                cwd=str(Path(__file__).resolve().parent),
                env=self._fresh_instance_env(),
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

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor(self.gradient_top))
        gradient.setColorAt(1.0, QColor(self.gradient_bottom))
        painter.fillRect(self.rect(), gradient)
