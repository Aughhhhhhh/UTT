"""Lightweight .rx2/.psg folder viewer for files opened directly with UTT.

When UTT is launched by opening an .rx2 or .psg file (file association,
"Open with", etc.), this window opens instead of the main app. It lists every
.rx2/.psg file in the same folder, previews textures, and exports textures as
PNG or models as GLB — no game archive or cache required.
"""
from __future__ import annotations

import shutil
import sys
import traceback
from pathlib import Path

from PIL.ImageQt import ImageQt
from PyQt6.QtCore import Qt, QThread
from PyQt6.QtGui import QColor, QGuiApplication, QLinearGradient, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QRadioButton,
    QSplitter, QVBoxLayout, QWidget,
)

from mainui import (
    APP_TITLE, DEFAULT_THEME, THEMES, Worker, _force_opaque, TextureZoomPreview,
    get_saved_theme, get_theme, reveal_in_explorer, save_theme,
)
from alpha_mask import apply_alpha_mask
from model_viewer import ModelPreview
from PSGTx import PSGTx

FILE_SUFFIXES = (".rx2", ".psg")

MODEL_KINDS = ("model",)
TEXTURE_KINDS = ("texture",)


def describe_file(path: Path) -> tuple[str, str]:
    """Classify a file and return ``(kind, info_text)``.

    ``kind`` is ``"model"``, ``"texture"`` or ``"other"``.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        return "other", f"Unreadable: {exc}"

    if data[:7] == b"\x89RW4xb2":
        type_id = data[0x58:0x5C] if len(data) >= 0x5C else b""
        if type_id in (b"\x00\x00\x10\x00", b"\x00\x00\x00\x10"):
            return "texture", _describe_rx2_texture(path, data)
        if type_id in (b"\x00\x00\x00\x04", b"\x00\x00\x08\x00"):
            return "model", "RX2 model"
        return "other", f"RX2 file (type {type_id.hex(' ')})"

    if data[:7] == b"\x89RW4ps3":
        from mdl_parser import is_psg_model
        if is_psg_model(data):
            return "model", "PSG model"
        try:
            tx = PSGTx(data)
        except Exception as exc:
            return "other", f"PSG file — failed to read: {exc}"
        return "texture", f"PSG texture | {tx.tx_width}x{tx.tx_height} | {tx.tx_format.decode()}"

    return "other", "Unrecognized file"


def _describe_rx2_texture(path: Path, data: bytes) -> str:
    try:
        from rx2_parser import parse_rx2
        rx2 = parse_rx2(path)
    except Exception as exc:
        return f"RX2 texture — failed to decode: {exc}"
    if not rx2.textures:
        return "RX2 texture — no decodable textures"
    texture = rx2.textures[0]
    return f"RX2 texture | {texture.width}x{texture.height} | {texture.fmt_name}"


def _describe_model(path: Path, model=None) -> str:
    if model is None:
        try:
            from mdl_parser import load_model
            model = load_model(path)
        except Exception as exc:
            return f"{path.suffix[1:].upper()} model — failed to parse: {exc}"
    vertices = sum(len(mesh.vertices) for mesh in model.meshes)
    triangles = sum(len(mesh.faces) for mesh in model.meshes)
    bones = len(model.bones)
    return (
        f"{path.suffix[1:].upper()} model | {len(model.meshes)} mesh(es) | "
        f"{vertices} verts | {triangles} tris | {bones} bone(s)"
    )


def preview_texture(path: Path, opaque: bool, alpha_mask: bool = False) -> object | None:
    """Decode a texture file into a PIL image, or None when it has none.

    Uses the same parsers as the main tool: ``rx2_parser`` for RX2 files
    (Xbox 360) and ``PSGTx`` for PSG files (PS3). The alpha mask is applied
    first, then the opacity fix, mirroring the main tool.
    """
    if path.suffix.lower() == ".rx2":
        from rx2_parser import parse_rx2
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
    try:
        return PSGTx(str(path)).get_tx_image(opaque, alpha_mask=alpha_mask)
    except Exception:
        return None


def load_model_preview(path: Path):
    """Load a PSG or RX2 model with the main tool's parser, or None."""
    from mdl_parser import load_model
    try:
        return load_model(path)
    except Exception:
        return None


def hex_name_of(path: Path) -> str:
    stem = path.stem
    if stem.lower().startswith("0x"):
        return stem
    return "0x" + stem


class QuickFileViewer(QMainWindow):
    def __init__(self, opened_path: Path):
        super().__init__()
        self.opened_path = Path(opened_path).resolve()
        self.folder = self.opened_path.parent
        self._threads: list[QThread] = []
        self._workers: list[Worker] = []
        self._current_image = None
        self._current_kind = "other"
        self._current_path: Path | None = None

        self.platform_key = "xbx" if self.opened_path.suffix.lower() == ".rx2" else "ps3"
        self.theme_name = get_saved_theme()
        theme = get_theme(self.theme_name)
        self.gradient_top, self.gradient_bottom = theme["gradient"]
        self.surface = theme["surface"]
        self.border = theme["border"]
        self.line = theme["line"]
        self.outline = theme["outline"]
        self._apply_style()

        self.setWindowTitle("UTT — Ultimate Texture Toolkit")
        self.resize(980, 620)

        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)

        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        header = QLabel(f"Folder: {self.folder}")
        header.setObjectName("folderLabel")
        header.setWordWrap(True)
        header_row.addWidget(header, 1)
        theme_button = QPushButton("Theme…")
        theme_button.clicked.connect(self._choose_theme)
        header_row.addWidget(theme_button)
        root.addLayout(header_row)

        splitter = QSplitter(Qt.Orientation.Horizontal, central)
        root.addWidget(splitter, 1)

        self.file_list = QListWidget()
        self.file_list.setObjectName("fileList")
        splitter.addWidget(self.file_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(10, 0, 0, 0)

        self.texture_preview = TextureZoomPreview()
        self.texture_preview.setMinimumHeight(420)
        right_layout.addWidget(self.texture_preview, 1)

        self.model_preview = ModelPreview()
        self.model_preview.export_requested.connect(self._export_loaded_model)
        self.model_preview.hide()
        right_layout.addWidget(self.model_preview, 1)

        self.info_label = QLabel("")
        self.info_label.setObjectName("details")
        self.info_label.setWordWrap(True)
        right_layout.addWidget(self.info_label)

        controls = QHBoxLayout()
        self.force_opaque = QCheckBox("Force visible pixels (255 opacity)")
        self.force_opaque.setToolTip(
            "Raise every visible pixel (alpha > 0) to full opacity, "
            "hiding DXT compression transparency bleed."
        )
        controls.addWidget(self.force_opaque)
        self.alpha_mask_checkbox = QCheckBox("Fix alpha grain (Alpha Mask)")
        self.alpha_mask_checkbox.setToolTip(
            "Replaces the alpha channel with the texture's brightness (the "
            "Alpha Mask Paint.NET plugin port). Removes DXT5 alpha grain and "
            "transparent dots. Applied before the opacity fix, so keeping both "
            "checked gives clean full opacity; dark texture areas only go "
            "translucent when this is on and the opacity fix is off. Disable "
            "for mask-style textures (decal2, blurmask)."
        )
        controls.addWidget(self.alpha_mask_checkbox)
        controls.addStretch(1)
        self.export_png_button = QPushButton("Export PNG…")
        self.export_png_button.setEnabled(False)
        controls.addWidget(self.export_png_button)
        right_layout.addLayout(controls)

        splitter.addWidget(right)
        splitter.setSizes([300, 660])

        self.statusBar().showMessage("")

        self.file_list.currentItemChanged.connect(self._on_selection_changed)
        self.file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._show_context_menu)
        self.force_opaque.toggled.connect(self._on_texture_options_toggled)
        self.alpha_mask_checkbox.toggled.connect(self._on_texture_options_toggled)
        self.export_png_button.clicked.connect(self._export_png)

        self._populate()
        self._select_path(self.opened_path)

    def _apply_style(self) -> None:
        theme = get_theme(self.theme_name)[self.platform_key]
        accent = theme["accent"]
        self.setStyleSheet(
            """
            QWidget { color: #f1f3f4; font-size: 14px; }
            QLabel#folderLabel { font-weight: 600; padding: 4px; }
            QListWidget#fileList { background: @surface; border: 1px solid @border; border-radius: 9px; padding: 4px; }
            QListWidget#fileList::item { padding: 5px; border-radius: 6px; }
            QListWidget#fileList::item:selected { background: @accent_dark; }
            QLabel#preview { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 @gradient_top, stop:1 @gradient_bottom); border: 1px dashed @outline; border-radius: 16px; }
            QLabel#details, QLabel#modelDetails { color: #aeb4bd; }
            QLabel#modelTitle { font-weight: 600; }
            QPushButton { background: @accent; border: 0; border-radius: 16px; padding: 8px 15px; font-weight: 600; }
            QPushButton:hover { background: @accent_hover; }
            QPushButton:disabled { background: #45474b; color: #9aa0a6; }
            QCheckBox { padding: 4px; }
            QRadioButton { background: transparent; color: #f1f3f4; spacing: 8px; }
            QRadioButton::indicator { width: 16px; height: 16px; border: 1px solid @border; border-radius: 8px; background: @surface; }
            QRadioButton::indicator:checked { background: @accent_dark; border: 1px solid @accent_soft; }
            QMenu { background: @surface; border: 1px solid @border; }
            QMenu::item:selected { background: @accent_dark; }
            """
            .replace("@gradient_top", self.gradient_top)
            .replace("@gradient_bottom", self.gradient_bottom)
            .replace("@surface", self.surface)
            .replace("@border", self.border)
            .replace("@line", self.line)
            .replace("@outline", self.outline)
            .replace("@accent_hover", theme["hover"])
            .replace("@accent_dark", theme["dark"])
            .replace("@accent_soft", theme["soft"])
            .replace("@accent", accent)
        )

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor(self.gradient_top))
        gradient.setColorAt(1.0, QColor(self.gradient_bottom))
        painter.fillRect(self.rect(), gradient)

    def _choose_theme(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Theme")
        dialog.setModal(True)
        dialog.setStyleSheet(
            "QDialog { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            f"stop:0 {self.gradient_top}, stop:1 {self.gradient_bottom}); }}"
        )
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        dialog.setMinimumWidth(340)
        label = QLabel(
            "Choose the background gradient. The accent (blue for PS3 files, "
            "green for Xbox files) changes to match the theme."
        )
        label.setWordWrap(True)
        label.setObjectName("details")
        layout.addWidget(label)

        theme_radios: dict[str, QRadioButton] = {}
        selected_theme = get_saved_theme()
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
            row.addWidget(radio)
            row.addStretch(1)
            row.addWidget(swatch)
            layout.addLayout(row)
            theme_radios[name] = radio

        reset_button = QPushButton("Reset to default")
        reset_button.clicked.connect(
            lambda: theme_radios[DEFAULT_THEME].setChecked(True)
        )
        layout.addWidget(reset_button, 0, Qt.AlignmentFlag.AlignRight)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(dialog.reject)
        apply_button = QPushButton("Apply")
        apply_button.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply_button)
        layout.addLayout(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = next(
            name for name, radio in theme_radios.items() if radio.isChecked()
        )
        if chosen == get_saved_theme():
            return
        save_theme(chosen)
        self.theme_name = chosen
        theme = THEMES[chosen]
        self.gradient_top, self.gradient_bottom = theme["gradient"]
        self.surface = theme["surface"]
        self.border = theme["border"]
        self.line = theme["line"]
        self.outline = theme["outline"]
        self._apply_style()
        self.update()

    def _populate(self) -> None:
        try:
            entries = sorted(
                (
                    entry for entry in self.folder.iterdir()
                    if entry.is_file() and entry.suffix.lower() in FILE_SUFFIXES
                ),
                key=lambda entry: entry.name.lower(),
            )
        except OSError as exc:
            self.statusBar().showMessage(f"Could not list folder: {exc}")
            return
        self.file_list.clear()
        for entry in entries:
            item = QListWidgetItem(entry.name)
            item.setData(Qt.ItemDataRole.UserRole, str(entry))
            item.setToolTip(str(entry))
            self.file_list.addItem(item)
        self.statusBar().showMessage(f"{len(entries)} file(s) in folder")

    def _select_path(self, path: Path) -> None:
        for index in range(self.file_list.count()):
            item = self.file_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == str(path):
                self.file_list.setCurrentItem(item)
                return

    def _current_file(self) -> Path | None:
        item = self.file_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return Path(value) if value else None

    def _on_selection_changed(self, current, previous):
        if current is None:
            return
        path = self._current_file()
        if path is None:
            return
        self._current_path = path
        self.export_png_button.setEnabled(False)
        self._current_image = None
        self._hide_model()
        self.texture_preview.set_message("Loading…")
        self.info_label.setText("")
        self._run_task(
            lambda: self._preview_file(
                path, self.force_opaque.isChecked(),
                self.alpha_mask_checkbox.isChecked(),
            ),
            self._preview_finished,
        )

    @staticmethod
    def _preview_file(path: Path, opaque: bool, alpha_mask: bool = False):
        kind, info = describe_file(path)
        payload = None
        if kind in MODEL_KINDS:
            payload = load_model_preview(path)
            if payload is not None:
                info = _describe_model(path, payload)
        else:
            payload = preview_texture(path, opaque, alpha_mask)
            if payload is None and "cas_db" in {part.lower() for part in path.parts}:
                # Files under cas_db are models: fall back to the model parser
                # when the texture decode produces nothing.
                payload = load_model_preview(path)
                if payload is not None:
                    kind = "model"
                    info = _describe_model(path, payload)
        return path, kind, info, payload

    def _preview_finished(self, result) -> None:
        path, kind, info, payload = result
        if path != self._current_path:
            return
        self._current_kind = kind
        self.info_label.setText(info)
        self.export_png_button.setEnabled(kind in TEXTURE_KINDS)
        self.force_opaque.setVisible(kind in TEXTURE_KINDS)
        self.alpha_mask_checkbox.setVisible(kind in TEXTURE_KINDS)
        if kind in MODEL_KINDS and payload is not None:
            self._show_model(path, payload)
            return
        self._hide_model()
        if payload is not None:
            self._current_image = payload
            self._show_image(payload)
            return
        self._current_image = None
        if kind in TEXTURE_KINDS:
            self.texture_preview.set_message("No decodable texture found.")
        elif kind in MODEL_KINDS:
            self.texture_preview.set_message("3D preview is not available.")
        else:
            self.texture_preview.set_message("This file has no preview.")

    def _show_model(self, path: Path, model) -> None:
        self.texture_preview.hide()
        self.model_preview.show()
        self.model_preview.set_model(path, model)

    def _hide_model(self) -> None:
        self.model_preview.hide()
        self.texture_preview.show()

    def _show_image(self, image) -> None:
        self.texture_preview.set_image(image)

    def _on_texture_options_toggled(self) -> None:
        if self._current_path is None:
            return
        if self._current_kind in TEXTURE_KINDS:
            self.texture_preview.set_message("Loading…")
            self._run_task(
                lambda: preview_texture(
                    self._current_path, self.force_opaque.isChecked(),
                    self.alpha_mask_checkbox.isChecked(),
                ),
                self._options_preview_finished,
            )

    def _options_preview_finished(self, image) -> None:
        if image is None:
            return
        self._current_image = image
        self._show_image(image)

    def _show_context_menu(self, pos) -> None:
        item = self.file_list.itemAt(pos)
        if item is None:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        menu = QMenu(self)
        png_action = menu.addAction("Export PNG…")
        png_action.setEnabled(self._current_path == path and self._current_kind in TEXTURE_KINDS)
        menu.addSeparator()
        copy_action = menu.addAction("Copy hex name")
        export_action = menu.addAction("Export file…")
        explore_action = menu.addAction("Open in Explorer")
        chosen = menu.exec(self.file_list.viewport().mapToGlobal(pos))
        if chosen is png_action:
            self._export_png()
        elif chosen is copy_action:
            QGuiApplication.clipboard().setText(hex_name_of(path))
        elif chosen is export_action:
            self._export_raw_file(path)
        elif chosen is explore_action:
            self._open_in_explorer(path)

    def _export_png(self) -> None:
        if self._current_path is None or self._current_kind not in TEXTURE_KINDS:
            return
        default = self._current_path.with_suffix(".png")
        target, _ = QFileDialog.getSaveFileName(
            self, "Export texture as PNG", str(default), "PNG (*.png);;All files (*.*)"
        )
        if not target:
            return
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            if self._current_image is None:
                QMessageBox.information(self, APP_TITLE, "No decodable texture to export.")
                return
            self._current_image.save(target)
            QMessageBox.information(self, APP_TITLE, f"Exported:\n{target}")
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))

    def _export_loaded_model(self, model, path: Path, with_skin=False) -> None:
        default = Path(path).with_suffix(".glb")
        target, _ = QFileDialog.getSaveFileName(
            self, "Export model as glTF Binary", str(default),
            "glTF Binary (*.glb);;All files (*.*)",
        )
        if not target:
            return

        def task():
            from gltf_exporter import export_gltf
            return export_gltf(model, target, with_skin=with_skin)

        def done(result):
            QMessageBox.information(self, APP_TITLE, f"Model exported:\n{result}")

        self._run_task(task, done)

    def _export_raw_file(self, path: Path) -> None:
        default = path.parent / f"{path.stem}_copy{path.suffix}"
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
            QMessageBox.critical(self, APP_TITLE, str(exc))

    def _open_in_explorer(self, path: Path) -> None:
        reveal_in_explorer(path)

    def _run_task(self, task, on_done) -> None:
        thread = QThread(self)
        worker = Worker(task)
        self._workers.append(worker)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(on_done)
        worker.finished.connect(thread.quit)
        worker.failed.connect(lambda error: QMessageBox.critical(self, APP_TITLE, error))
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(
            lambda: self._workers.remove(worker) if worker in self._workers else None
        )
        self._threads.append(thread)
        self._threads = [t for t in self._threads if t.isRunning()]
        thread.start()

    def closeEvent(self, event) -> None:
        for thread in list(self._threads):
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait(1000)
            except RuntimeError:
                pass
        super().closeEvent(event)
