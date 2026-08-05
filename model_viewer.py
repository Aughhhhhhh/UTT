from __future__ import annotations

from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QMatrix4x4, QPainter, QSurfaceFormat, QVector3D
from PyQt6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLFunctions_2_0,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLVertexArrayObject,
)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
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

# OpenGL constants (PyQt6 does not export the GL_* enums)
_GL_COLOR_BUFFER_BIT = 0x4000
_GL_DEPTH_BUFFER_BIT = 0x0100
_GL_DEPTH_TEST = 0x0B71
_GL_FRONT_AND_BACK = 0x0408
_GL_FILL = 0x1B02
_GL_LINE = 0x1B01
_GL_TRIANGLES = 0x0004
_GL_POINTS = 0x0000
_GL_UNSIGNED_INT = 0x1405
_GL_FLOAT = 0x1406

SUBDIVISION_TRIANGLE_LIMIT = 50_000

_VERTEX_SHADER = """
#version 120
attribute vec3 a_position;
attribute vec3 a_normal;
uniform mat4 u_mvp;
varying vec3 v_normal;
void main() {
    v_normal = a_normal;
    gl_Position = u_mvp * vec4(a_position, 1.0);
}
"""

_FRAGMENT_SHADER = """
#version 120
varying vec3 v_normal;
uniform vec3 u_light_dir;
uniform vec3 u_tint;
void main() {
    vec3 n = normalize(v_normal);
    if (!gl_FrontFacing) {
        n = -n;
    }
    vec3 l = normalize(u_light_dir);
    float ndl = clamp(dot(n, l), 0.0, 1.0);
    float hemi = clamp(n.y * 0.5 + 0.5, 0.0, 1.0);
    float intensity = 0.18 + 0.20 * hemi + 0.62 * pow(ndl, 0.8);
    gl_FragColor = vec4(u_tint * intensity, 1.0);
}
"""


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


class _MeshBuffers:
    """GPU-side geometry for one mesh: interleaved vertices + indices."""

    def __init__(self):
        self.vao: QOpenGLVertexArrayObject | None = None
        self.vbo: QOpenGLBuffer | None = None
        self.ibo: QOpenGLBuffer | None = None
        self.index_count = 0
        self.vertex_count = 0
        self.is_points = False
        self.tint = (1.0, 1.0, 1.0)


class _GlViewport(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        fmt = QSurfaceFormat()
        fmt.setVersion(2, 1)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
        fmt.setDepthBufferSize(24)
        self.setFormat(fmt)
        self._meshes: list[tuple[np.ndarray, np.ndarray | None, float]] = []
        self._buffers: list[_MeshBuffers] = []
        self._needs_upload = False
        self._message = ""
        self._yaw = -55.0
        self._pitch = 22.0
        self._distance = 3.4
        self._last_pos = None
        self._wireframe = False
        self._light_dir = np.array((0.35, -0.45, 0.82), dtype=np.float32)
        self._light_dir /= np.linalg.norm(self._light_dir)
        self._empty_program: QOpenGLShaderProgram | None = None

    # ------------------------------------------------------------------ model

    def set_meshes(self, meshes: list[Mesh]) -> None:
        self._meshes = []
        for index, mesh in enumerate(meshes):
            if mesh.vertex_count == 0:
                continue
            positions = mesh.vertices.astype(np.float32)
            if mesh.normals is not None:
                normals = mesh.normals.astype(np.float32)
            else:
                normals = np.zeros_like(positions)
            # interleave position + normal (stride 24 bytes)
            interleaved = np.empty((len(positions), 6), dtype=np.float32)
            interleaved[:, 0:3] = positions
            interleaved[:, 3:6] = normals
            tint = 1.0 + ((index % 3) - 1) * 0.06
            faces = mesh.faces.astype(np.uint32) if mesh.triangle_count else None
            self._meshes.append((interleaved, faces, tint))
        self._needs_upload = True
        self.update()

    def set_message(self, message: str) -> None:
        self._message = message
        self._meshes = []
        self._buffers = []
        self._needs_upload = False
        self.update()

    def set_wireframe(self, enabled: bool) -> None:
        self._wireframe = enabled
        self.update()

    def reset_camera(self) -> None:
        self._yaw = -55.0
        self._pitch = 22.0
        self._distance = 3.4
        self.update()

    # ------------------------------------------------------------- GL setup

    def initializeGL(self) -> None:
        self._empty_program = QOpenGLShaderProgram(self)
        self._empty_program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Vertex, _VERTEX_SHADER
        )
        self._empty_program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Fragment, _FRAGMENT_SHADER
        )
        self._empty_program.bindAttributeLocation("a_position", 0)
        self._empty_program.bindAttributeLocation("a_normal", 1)
        self._empty_program.link()
        self._upload_meshes()

    def _upload_meshes(self) -> None:
        for buffer in self._buffers:
            if buffer.vao is not None:
                buffer.vao.destroy()
        self._buffers = []
        if not self._meshes:
            return
        gl = QOpenGLFunctions_2_0()
        gl.initializeOpenGLFunctions()
        for interleaved, faces, tint in self._meshes:
            buffer = _MeshBuffers()
            buffer.vertex_count = interleaved.shape[0]
            buffer.is_points = faces is None
            buffer.tint = (tint, tint, tint)
            buffer.vao = QOpenGLVertexArrayObject(self)
            buffer.vao.create()
            buffer.vao.bind()
            buffer.vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            buffer.vbo.create()
            buffer.vbo.bind()
            buffer.vbo.allocate(interleaved.tobytes(), interleaved.nbytes)
            self._empty_program.enableAttributeArray(0)
            self._empty_program.setAttributeBuffer(
                0, _GL_FLOAT, 0, 3, 6 * np.dtype(np.float32).itemsize
            )
            self._empty_program.enableAttributeArray(1)
            self._empty_program.setAttributeBuffer(
                1, _GL_FLOAT, 3 * np.dtype(np.float32).itemsize, 3,
                6 * np.dtype(np.float32).itemsize,
            )
            if faces is not None:
                buffer.ibo = QOpenGLBuffer(QOpenGLBuffer.Type.IndexBuffer)
                buffer.ibo.create()
                buffer.ibo.bind()
                buffer.ibo.allocate(faces.tobytes(), faces.nbytes)
                buffer.index_count = faces.shape[0] * 3
            buffer.vao.release()
            self._buffers.append(buffer)
        self._needs_upload = False

    # -------------------------------------------------------------- painting

    def paintGL(self) -> None:
        gl = QOpenGLFunctions_2_0()
        gl.initializeOpenGLFunctions()
        gl.glClearColor(0.094, 0.098, 0.106, 1.0)
        gl.glClear(_GL_COLOR_BUFFER_BIT | _GL_DEPTH_BUFFER_BIT)

        if self._needs_upload:
            self._upload_meshes()

        if not self._buffers:
            if self._message:
                painter = QPainter(self)
                painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
                painter.setPen(QColor("#9aa0a6"))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
                painter.end()
            return

        gl.glEnable(_GL_DEPTH_TEST)
        aspect = self.width() / max(1, self.height())
        projection = QMatrix4x4()
        projection.perspective(45.0, aspect, 0.05, 200.0)
        rad_y = np.radians(self._yaw)
        rad_p = np.radians(self._pitch)
        eye_x = self._distance * np.cos(rad_p) * np.cos(rad_y)
        eye_y = self._distance * np.sin(rad_p)
        eye_z = self._distance * np.cos(rad_p) * np.sin(rad_y)
        view = QMatrix4x4()
        view.lookAt(
            QVector3D(eye_x, eye_y, eye_z),
            QVector3D(0.0, 0.0, 0.0),
            QVector3D(0.0, 1.0, 0.0),
        )
        mvp = projection * view

        if self._wireframe:
            self._empty_program.bind()
            self._empty_program.setUniformValue("u_mvp", mvp)
            self._empty_program.setUniformValue(
                "u_light_dir",
                QVector3D(self._light_dir[0], self._light_dir[1], self._light_dir[2]),
            )
            gl.glPolygonMode(_GL_FRONT_AND_BACK, _GL_LINE)
            gl.glLineWidth(1.0)
            for buffer in self._buffers:
                if buffer.is_points:
                    continue
                self._empty_program.setUniformValue(
                    "u_tint",
                    QVector3D(buffer.tint[0], buffer.tint[1], buffer.tint[2]),
                )
                self._draw_buffer(gl, buffer, mvp)
            gl.glPolygonMode(_GL_FRONT_AND_BACK, _GL_FILL)
        else:
            self._empty_program.bind()
            self._empty_program.setUniformValue("u_mvp", mvp)
            self._empty_program.setUniformValue(
                "u_light_dir",
                QVector3D(self._light_dir[0], self._light_dir[1], self._light_dir[2]),
            )
            for buffer in self._buffers:
                if buffer.is_points:
                    gl.glPointSize(2.0)
                self._empty_program.setUniformValue(
                    "u_tint",
                    QVector3D(buffer.tint[0], buffer.tint[1], buffer.tint[2]),
                )
                self._draw_buffer(gl, buffer, mvp)
        gl.glDisable(_GL_DEPTH_TEST)

    def _draw_buffer(self, gl, buffer: _MeshBuffers, mvp) -> None:
        if buffer.vao is not None:
            buffer.vao.bind()
        if buffer.ibo is not None:
            gl.glDrawElements(
                _GL_TRIANGLES, buffer.index_count, _GL_UNSIGNED_INT, None
            )
        elif buffer.vertex_count:
            gl.glDrawArrays(_GL_POINTS, 0, buffer.vertex_count)
        if buffer.vao is not None:
            buffer.vao.release()

    # -------------------------------------------------------------- input

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._last_pos = e.position()

    def mouseMoveEvent(self, e) -> None:
        if self._last_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            delta = e.position() - self._last_pos
            self._last_pos = e.position()
            self._yaw += delta.x() * 0.6
            self._pitch = max(-89.0, min(89.0, self._pitch + delta.y() * 0.6))
            self.update()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._last_pos = None

    def wheelEvent(self, e) -> None:
        factor = 0.85 if e.angleDelta().y() > 0 else 1.18
        self._distance = max(0.6, min(30.0, self._distance * factor))
        self.update()


class ModelPreview(QWidget):
    export_requested = pyqtSignal(object, object)  # (PSGModel, Path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model: PSGModel | None = None
        self.path: Path | None = None

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
        self.wireframe.toggled.connect(self._toggle_wireframe)
        header.addWidget(self.wireframe)
        self.warnings_button = QPushButton("Warnings")
        self.warnings_button.setEnabled(False)
        self.warnings_button.clicked.connect(self.show_warnings)
        header.addWidget(self.warnings_button)
        reset_button = QPushButton("Reset view")
        reset_button.clicked.connect(self.reset_view)
        header.addWidget(reset_button)
        layout.addLayout(header)

        self.viewport = _GlViewport()
        self.viewport.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(self.viewport, 1)

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

        self.viewport.set_message("Select a PSG model from the list")

    def show_loading(self, path: Path) -> None:
        self.model = None
        self.path = path
        self.title_label.setText(path.name)
        self.details_label.setText("Parsing model…")
        self.warnings_button.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.viewport.set_message("Loading…")

    def clear(self, message: str = "No model loaded") -> None:
        self.model = None
        self.path = None
        self.title_label.setText("Select a model to preview")
        self.details_label.setText("")
        self.warnings_button.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.viewport.set_message(message)

    def set_model(self, path: Path, model: PSGModel) -> None:
        self.path = path
        self._subdivide_meshes(model)
        self.model = model
        self.title_label.setText(path.name)
        self.details_label.setText(
            f"{len(model.meshes):,} meshes  •  {model.vertex_count:,} vertices  •  "
            f"{model.triangle_count:,} triangles  •  {len(model.bones):,} bones"
        )
        self.warnings_button.setText(f"Warnings ({len(model.warnings)})")
        self.warnings_button.setEnabled(bool(model.warnings))
        self.export_btn.setEnabled(True)
        self._upload_view_model(reset_camera=True)

    @staticmethod
    def _subdivide_meshes(model: PSGModel) -> None:
        for i in range(len(model.meshes)):
            model.meshes[i] = _subdivide_mesh(model.meshes[i])

    def _upload_view_model(self, reset_camera: bool = False) -> None:
        if self.model is None:
            return
        minimum, maximum = self.model.bounds
        center = (minimum + maximum) * 0.5
        scale = float(np.max(np.abs(maximum - minimum)))
        if not np.isfinite(scale) or scale <= 1e-12:
            scale = 1.0

        meshes = []
        for mesh in self.model.meshes:
            if mesh.vertex_count == 0:
                continue
            verts = (mesh.vertices - center) / scale * 2.0
            normals = (
                mesh.normals if mesh.normals is not None
                else np.zeros_like(verts)
            )
            meshes.append(
                Mesh(
                    name=mesh.name,
                    vertices=verts.astype(np.float32),
                    faces=mesh.faces,
                    uvs=mesh.uvs,
                    normals=normals.astype(np.float32),
                    material_name=mesh.material_name,
                    vertex_stride=mesh.vertex_stride,
                    attributes=mesh.attributes,
                    source_offsets=mesh.source_offsets,
                )
            )
        self.viewport.set_meshes(meshes)
        if reset_camera:
            self.viewport.reset_camera()

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
        self._upload_view_model(reset_camera=reset_camera)

    def reset_view(self) -> None:
        self.viewport.reset_camera()

    def _toggle_wireframe(self, checked: bool) -> None:
        self.viewport.set_wireframe(checked)
