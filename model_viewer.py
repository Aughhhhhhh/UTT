from __future__ import annotations

from pathlib import Path

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mdl_parser import Mesh, PSGModel


MAX_VIEWPORT_TRIANGLES = 200_000
INTERACTION_TRIANGLES = 3_000
SUBDIVISION_TRIANGLE_LIMIT = 50_000


def _subdivide_mesh(mesh: Mesh) -> Mesh:
    if mesh.normals is None or mesh.triangle_count == 0 or mesh.triangle_count > SUBDIVISION_TRIANGLE_LIMIT:
        return mesh
    verts = mesh.vertices
    norms = mesh.normals
    faces = mesh.faces
    uvs = mesh.uvs
    edge_map: dict[tuple[int, int], int] = {}
    new_verts = list(verts)
    new_norms = list(norms)
    new_uvs = list(uvs) if uvs is not None else None

    def midpoint(i: int, j: int) -> int:
        key = (i, j) if i < j else (j, i)
        idx = edge_map.get(key)
        if idx is not None:
            return idx
        idx = len(new_verts)
        new_verts.append((verts[i] + verts[j]) * 0.5)
        avg = norms[i] + norms[j]
        new_norms.append(avg / np.linalg.norm(avg))
        if new_uvs is not None:
            new_uvs.append((uvs[i] + uvs[j]) * 0.5)
        edge_map[key] = idx
        return idx

    new_faces = []
    for a, b, c in faces:
        ab = midpoint(a, b)
        bc = midpoint(b, c)
        ca = midpoint(c, a)
        new_faces.append((a, ab, ca))
        new_faces.append((b, bc, ab))
        new_faces.append((c, ca, bc))
        new_faces.append((ab, bc, ca))

    return Mesh(
        name=mesh.name,
        vertices=np.asarray(new_verts, dtype=np.float32),
        faces=np.asarray(new_faces, dtype=np.uint32),
        uvs=np.asarray(new_uvs, dtype=np.float32) if new_uvs is not None else None,
        normals=np.asarray(new_norms, dtype=np.float32),
        material_name=mesh.material_name,
        vertex_stride=mesh.vertex_stride,
        attributes=mesh.attributes,
        source_offsets=mesh.source_offsets,
    )


class ModelPreview(QWidget):
    export_requested = pyqtSignal(object, object)  # (PSGModel, Path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model: PSGModel | None = None
        self.path: Path | None = None
        self._full_artists = []
        self._interaction_artists = []
        self._interactive_lod_active = False
        self._lod_restore_timer = QTimer(self)
        self._lod_restore_timer.setSingleShot(True)
        self._lod_restore_timer.timeout.connect(self._restore_full_detail)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title_column = QVBoxLayout()
        self.title_label = QLabel("Select a model to preview")
        self.title_label.setObjectName("modelTitle")
        self.details_label = QLabel("Models are parsed directly from the cached PSG files.")
        self.details_label.setObjectName("modelDetails")
        title_column.addWidget(self.title_label)
        title_column.addWidget(self.details_label)
        header.addLayout(title_column, 1)

        self.wireframe = QCheckBox("Wireframe")
        self.wireframe.toggled.connect(self.redraw_model)
        header.addWidget(self.wireframe)
        self.warnings_button = QPushButton("Warnings")
        self.warnings_button.setEnabled(False)
        self.warnings_button.clicked.connect(self.show_warnings)
        header.addWidget(self.warnings_button)
        reset_button = QPushButton("Reset view")
        reset_button.clicked.connect(self.reset_view)
        header.addWidget(reset_button)
        layout.addLayout(header)

        self.figure = Figure(facecolor="#18191b")
        self.axes = self.figure.add_subplot(111, projection="3d")
        self.figure.subplots_adjust(left=0, right=1, top=1, bottom=0)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.canvas.mpl_connect("button_press_event", self._on_button_press)
        self.canvas.mpl_connect("button_release_event", self._on_button_release)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        layout.addWidget(self.canvas, 1)

        self.status_label = QLabel("Click and drag to rotate  •  Mouse wheel to zoom")
        self.status_label.setObjectName("modelDetails")
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.export_btn = QPushButton("Export as glTF…")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._on_export)
        button_row.addWidget(self.export_btn)
        layout.addLayout(button_row)
        self._draw_empty("Select a PSG model from the list")

    def show_loading(self, path: Path) -> None:
        self.model = None
        self.path = path
        self.title_label.setText(path.name)
        self.details_label.setText("Parsing model…")
        self.warnings_button.setEnabled(False)
        self.export_btn.setEnabled(False)
        self._draw_empty("Loading…")

    def clear(self, message: str = "No model loaded") -> None:
        self.model = None
        self.path = None
        self.title_label.setText("Select a model to preview")
        self.details_label.setText("")
        self.warnings_button.setEnabled(False)
        self.export_btn.setEnabled(False)
        self._draw_empty(message)

    def set_model(self, path: Path, model: PSGModel) -> None:
        self.path = path
        original_count = model.triangle_count
        self._subdivide_meshes(model)
        subdivided_count = model.triangle_count
        self.model = model
        self.title_label.setText(path.name)
        self.details_label.setText(
            f"{len(model.meshes):,} meshes  •  {model.vertex_count:,} vertices  •  "
            f"{model.triangle_count:,} triangles  •  {len(model.bones):,} bones"
        )
        self.warnings_button.setText(f"Warnings ({len(model.warnings)})")
        self.warnings_button.setEnabled(bool(model.warnings))
        self.export_btn.setEnabled(True)
        self.redraw_model(reset_camera=True)

        shown = min(model.triangle_count, MAX_VIEWPORT_TRIANGLES)
        parts = []
        if original_count != subdivided_count:
            parts.append(f"subdivided to {model.triangle_count:,}")
        if shown < model.triangle_count:
            parts.append(f"showing {shown:,}")
        label = "  •  ".join(parts)
        base = "Click and drag to rotate  •  Mouse wheel to zoom"
        self.status_label.setText(
            f"{label}  •  {base}" if label else base
        )

    @staticmethod
    def _subdivide_meshes(model: PSGModel) -> None:
        for i in range(len(model.meshes)):
            model.meshes[i] = _subdivide_mesh(model.meshes[i])

    def _on_export(self) -> None:
        if self.model is not None:
            self.export_requested.emit(self.model, self.path)

    def show_warnings(self) -> None:
        if self.model is None or not self.model.warnings:
            return
        warnings = self.model.warnings[:100]
        text = "\n\n".join(warnings)
        if len(self.model.warnings) > len(warnings):
            text += f"\n\n…and {len(self.model.warnings) - len(warnings)} more."
        QMessageBox.warning(self, "PSG parser warnings", text)

    def redraw_model(self, _checked=False, reset_camera: bool = False) -> None:
        if self.model is None:
            return

        previous_view = (self.axes.elev, self.axes.azim)
        previous_limits = (
            self.axes.get_xlim3d(),
            self.axes.get_ylim3d(),
            self.axes.get_zlim3d(),
        )
        self.axes.clear()
        self._style_axes()
        self._lod_restore_timer.stop()
        self._full_artists = []
        self._interaction_artists = []
        self._interactive_lod_active = False

        minimum, maximum = self.model.bounds
        center = (minimum + maximum) * 0.5
        scale = float(np.max(np.abs(maximum - minimum)))
        if not np.isfinite(scale) or scale <= 1e-12:
            scale = 1.0

        remaining_budget = MAX_VIEWPORT_TRIANGLES
        remaining_triangles = max(1, self.model.triangle_count)
        interaction_remaining_budget = INTERACTION_TRIANGLES
        interaction_remaining_triangles = remaining_triangles
        use_interaction_lod = self.model.triangle_count > INTERACTION_TRIANGLES
        for mesh_index, mesh in enumerate(self.model.meshes):
            if mesh.vertex_count == 0:
                continue

            vertices = (mesh.vertices - center) / scale * 2.0
            if mesh.triangle_count == 0:
                stride = max(1, mesh.vertex_count // 5_000)
                points = vertices[::stride]
                artist = self.axes.scatter(
                    points[:, 0],
                    points[:, 1],
                    points[:, 2],
                    s=1.0,
                    c="#d8d8d8",
                    depthshade=True,
                )
                self._full_artists.append(artist)
                continue

            allocation = self._mesh_allocation(
                mesh.triangle_count,
                remaining_budget,
                remaining_triangles,
            )
            remaining_budget = max(0, remaining_budget - allocation)
            remaining_triangles = max(0, remaining_triangles - mesh.triangle_count)

            sampled_faces = self._sample_faces(mesh.faces, allocation)
            face_normals = (
                mesh.normals[sampled_faces]
                if mesh.normals is not None
                else None
            )
            artist = self._make_collection(
                vertices[sampled_faces],
                mesh_index,
                normals=face_normals,
            )
            self.axes.add_collection3d(artist)
            self._full_artists.append(artist)

            if use_interaction_lod:
                interaction_allocation = self._mesh_allocation(
                    mesh.triangle_count,
                    interaction_remaining_budget,
                    interaction_remaining_triangles,
                )
                interaction_remaining_budget = max(
                    0, interaction_remaining_budget - interaction_allocation
                )
                interaction_remaining_triangles = max(
                    0,
                    interaction_remaining_triangles - mesh.triangle_count,
                )
                interaction_sampled = self._sample_faces(
                    mesh.faces, interaction_allocation
                )
                interaction_normals = (
                    mesh.normals[interaction_sampled]
                    if mesh.normals is not None
                    else None
                )
                interaction_artist = self._make_collection(
                    vertices[interaction_sampled],
                    mesh_index,
                    normals=interaction_normals,
                )
                interaction_artist.set_visible(False)
                self.axes.add_collection3d(interaction_artist)
                self._interaction_artists.append(interaction_artist)

        if reset_camera:
            self._reset_axes(redraw=False)
        else:
            self.axes.view_init(elev=previous_view[0], azim=previous_view[1])
            self.axes.set_xlim3d(previous_limits[0])
            self.axes.set_ylim3d(previous_limits[1])
            self.axes.set_zlim3d(previous_limits[2])
        self.canvas.draw_idle()

    def _make_collection(
        self, triangles: np.ndarray, mesh_index: int,
        normals: np.ndarray | None = None,
    ) -> Poly3DCollection:
        if self.wireframe.isChecked():
            return Poly3DCollection(
                triangles,
                facecolors=(0.055, 0.055, 0.055, 1.0),
                edgecolors=(0.78, 0.78, 0.78, 0.82),
                linewidths=0.18,
                antialiased=False,
                zsort="average",
            )

        light = np.array((0.35, -0.45, 0.82), dtype=np.float32)
        light /= np.linalg.norm(light)

        if normals is not None:
            vert_intensity = np.clip(
                0.25 + 0.68 * np.abs(normals @ light), 0.22, 0.93
            )
            intensity = vert_intensity.mean(axis=1)
        else:
            edge_a = triangles[:, 1] - triangles[:, 0]
            edge_b = triangles[:, 2] - triangles[:, 0]
            face_normals = np.cross(edge_a, edge_b)
            lengths = np.linalg.norm(face_normals, axis=1)
            valid = lengths > 1e-12
            face_normals[valid] /= lengths[valid, None]
            intensity = np.clip(
                0.25 + 0.68 * np.abs(face_normals @ light), 0.22, 0.93
            )

        intensity = np.clip(
            intensity + ((mesh_index % 3) - 1) * 0.035, 0.18, 0.96
        )
        facecolors = np.empty((len(triangles), 4), dtype=np.float32)
        facecolors[:, :3] = intensity[:, None]
        facecolors[:, 3] = 1.0

        return Poly3DCollection(
            triangles,
            facecolors=facecolors,
            edgecolors="none",
            linewidths=0.0,
            antialiased=False,
            zsort="average",
        )

    @staticmethod
    def _mesh_allocation(
        triangle_count: int, remaining_budget: int, remaining_triangles: int
    ) -> int:
        if remaining_budget <= 0:
            return 0
        return min(
            triangle_count,
            max(
                1,
                round(
                    remaining_budget
                    * (triangle_count / max(1, remaining_triangles))
                ),
            ),
        )

    @staticmethod
    def _sample_faces(faces: np.ndarray, allocation: int) -> np.ndarray:
        if allocation <= 0:
            return faces[:0]
        if allocation >= len(faces):
            return faces
        face_ids = np.linspace(0, len(faces) - 1, allocation, dtype=np.int64)
        return faces[face_ids]

    def reset_view(self) -> None:
        self._reset_axes(redraw=True)

    def _reset_axes(self, redraw: bool) -> None:
        self._style_axes()
        self.axes.view_init(elev=22, azim=-55)
        self.axes.set_xlim3d(-1.15, 1.15)
        self.axes.set_ylim3d(-1.15, 1.15)
        self.axes.set_zlim3d(-1.15, 1.15)
        try:
            self.axes.set_box_aspect((1, 1, 1), zoom=1.05)
        except TypeError:
            self.axes.set_box_aspect((1, 1, 1))
        if redraw:
            self.canvas.draw_idle()

    def _style_axes(self) -> None:
        self.axes.set_facecolor("#18191b")
        self.axes.set_axis_off()
        self.axes.grid(False)

    def _draw_empty(self, message: str) -> None:
        self._lod_restore_timer.stop()
        self._full_artists = []
        self._interaction_artists = []
        self._interactive_lod_active = False
        self.axes.clear()
        self._style_axes()
        self.axes.text2D(
            0.5,
            0.5,
            message,
            color="#9aa0a6",
            horizontalalignment="center",
            verticalalignment="center",
            transform=self.axes.transAxes,
        )
        self._reset_axes(redraw=True)

    def _on_button_press(self, event) -> None:
        if event.inaxes is self.axes:
            self._set_interaction_lod(True, immediate=True)

    def _on_button_release(self, _event) -> None:
        self._restore_full_detail()

    def _on_scroll(self, event) -> None:
        if event.inaxes is not self.axes:
            return
        self._set_interaction_lod(True)
        factor = 0.84 if event.step > 0 else 1.19
        for getter, setter in (
            (self.axes.get_xlim3d, self.axes.set_xlim3d),
            (self.axes.get_ylim3d, self.axes.set_ylim3d),
            (self.axes.get_zlim3d, self.axes.set_zlim3d),
        ):
            low, high = getter()
            center = (low + high) * 0.5
            half = (high - low) * 0.5 * factor
            setter(center - half, center + half)
        self.canvas.draw_idle()
        self._lod_restore_timer.start(180)

    def _set_interaction_lod(
        self, enabled: bool, immediate: bool = False
    ) -> None:
        if (
            not self._interaction_artists
            or self._interactive_lod_active == enabled
        ):
            return
        for artist in self._full_artists:
            artist.set_visible(not enabled)
        for artist in self._interaction_artists:
            artist.set_visible(enabled)
        self._interactive_lod_active = enabled
        if immediate:
            self.canvas.draw()
        else:
            self.canvas.draw_idle()

    def _restore_full_detail(self) -> None:
        self._set_interaction_lod(False)
