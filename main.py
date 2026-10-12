#!/usr/bin/env python3
"""On-screen ruler in centimeters.

Stays above other windows and opens centered. Drag the body to move it
and the blue tip to rotate it. The scale follows the monitor's physical size.
The overlay uses the screen geometry on Windows, macOS, and X11. Wayland
ignores that request, so there the window is fullscreen instead.

    python -m pip install -r requirements.txt
    python main.py
"""

from __future__ import annotations

import json
import math
import os
import sys

from PySide6.QtCore import QLocale, QPoint, QPointF, QRectF, QStandardPaths, Qt, QTimer
from PySide6.QtGui import (
    QActionGroup,
    QColor,
    QFont,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QRegion,
    QSurfaceFormat,
    QTransform,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

THICK = 56
KNOB_R = 14
CLOSE_R = 12
CLOSE_X = -28
GROW_R = 12
GROW_X = -60
GROW_STEP_CM = 2.0
MIN_CM = 3.0
MAX_CM = 200.0
STATE_VERSION = 2
LEMMA = "Just another ruler, but better."

LANGS: dict[str, dict[str, str]] = {
    "pt": {
        "name": "Português",
        "title": "Régua",
        "hint": "Arraste para mover. A ponta azul gira.",
        "scale": "escala {n}%",
        "center": "centro",
        "language": "Idioma",
        "center_cmd": "Centralizar",
        "horizontal": "Horizontal",
        "vertical": "Vertical",
        "rotate45": "Girar 45°",
        "longer": "Mais longa",
        "shorter": "Mais curta",
        "reset_scale": "Resetar escala",
        "close": "Fechar",
    },
    "en": {
        "name": "English",
        "title": "Ruler",
        "hint": "Drag to move. The blue tip rotates.",
        "scale": "scale {n}%",
        "center": "center",
        "language": "Language",
        "center_cmd": "Center",
        "horizontal": "Horizontal",
        "vertical": "Vertical",
        "rotate45": "Rotate 45°",
        "longer": "Longer",
        "shorter": "Shorter",
        "reset_scale": "Reset scale",
        "close": "Close",
    },
    "es": {
        "name": "Español",
        "title": "Regla",
        "hint": "Arrastra para mover. La punta azul gira.",
        "scale": "escala {n}%",
        "center": "centro",
        "language": "Idioma",
        "center_cmd": "Centrar",
        "horizontal": "Horizontal",
        "vertical": "Vertical",
        "rotate45": "Girar 45°",
        "longer": "Más larga",
        "shorter": "Más corta",
        "reset_scale": "Restablecer escala",
        "close": "Cerrar",
    },
}


def _system_lang() -> str:
    name = QLocale.system().name().lower()
    if name.startswith("pt"):
        return "pt"
    if name.startswith("es"):
        return "es"
    return "en"


class Ruler(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.angle = 0.0
        self.length_cm = 15.0
        self.user_scale = 1.0
        self.anchor = QPoint(0, 0)
        self._px = 38.0
        self._drag: str | None = None
        self._in_present = False
        self._press_global = QPointF()
        self._press_anchor = QPoint()
        self._ready = False
        self._hint = ""
        self.lang = _system_lang()

        self.setWindowFlags(self._window_flags())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save)
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.timeout.connect(self._clear_hint)

        self._load()
        self._ready = True

    def _window_flags(self) -> Qt.WindowType:
        kind = Qt.WindowType.Window if self._wayland() else Qt.WindowType.Tool
        return kind | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint

    def _wayland(self) -> bool:
        return QGuiApplication.platformName() == "wayland"

    def _tr(self, key: str) -> str:
        return LANGS[self.lang][key]

    def _fmt_cm(self, value: float) -> str:
        if abs(value - round(value)) < 0.05:
            return f"{round(value)} cm"
        text = f"{value:.1f}"
        if self.lang != "en":
            text = text.replace(".", ",")
        return f"{text} cm"

    def _fmt_scale(self) -> str:
        return self._tr("scale").format(n=f"{self.user_scale * 100:.0f}")

    def _apply_lang(self) -> None:
        title = f"{self._tr('title')} — {LEMMA}"
        self.setWindowTitle(title)
        self.setToolTip(LEMMA)
        app = QApplication.instance()
        if app is not None:
            app.setApplicationDisplayName(title)

    def px_per_cm(self) -> float:
        return self._px * self.user_scale

    def _length_px(self) -> float:
        return self.length_cm * self.px_per_cm()

    def _screen_for_anchor(self):
        for screen in QGuiApplication.screens():
            if screen.geometry().contains(self.anchor):
                return screen
        return QGuiApplication.primaryScreen()

    def _update_dpi(self) -> None:
        screen = self._screen_for_anchor()
        if screen is None:
            return
        phys = screen.physicalSize()
        geo = screen.size()
        px = None
        if phys.width() > 20 and phys.height() > 20 and geo.width() > 0 and geo.height() > 0:
            x = geo.width() / phys.width() * 10.0
            y = geo.height() / phys.height() * 10.0
            dpi = (x + y) / 2.0 * 2.54
            if 40 <= dpi <= 450 and abs(x - y) / max(x, y) < 0.25:
                px = (x + y) / 2.0
        if px is None:
            px = (screen.logicalDotsPerInch() or 96.0) / 2.54
        self._px = px

    def _center_anchor(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        self._update_dpi()
        geo = screen.availableGeometry()
        length = round(self._length_px())
        self.anchor = QPoint(
            geo.x() + max(0, (geo.width() - length) // 2),
            geo.y() + max(0, (geo.height() - THICK) // 2),
        )

    def _origin_widget(self) -> QPointF:
        origin = self.mapToGlobal(QPoint(0, 0))
        return QPointF(self.anchor.x() - origin.x(), self.anchor.y() - origin.y())

    def _to_local(self, pos: QPointF) -> tuple[float, float]:
        origin = self._origin_widget()
        dx = pos.x() - origin.x()
        dy = pos.y() - origin.y()
        cos_a = math.cos(self.angle)
        sin_a = math.sin(self.angle)
        return dx * cos_a + dy * sin_a, -dx * sin_a + dy * cos_a

    def _hit(self, lx: float, ly: float) -> str | None:
        length = self._length_px()
        if math.hypot(lx - length, ly - THICK / 2) <= KNOB_R + 6:
            return "knob"
        if math.hypot(lx - CLOSE_X, ly - THICK / 2) <= CLOSE_R + 6:
            return "close"
        if math.hypot(lx - GROW_X, ly - THICK / 2) <= GROW_R + 6:
            return "grow"
        if -8 <= lx <= length + 8 and -8 <= ly <= THICK + 8:
            return "body"
        return None

    def _shape(self) -> QPainterPath:
        length = self._length_px()
        path = QPainterPath()
        path.addRoundedRect(QRectF(-8, -8, length + 16, THICK + 16), 8, 8)
        path.addEllipse(QPointF(length, THICK / 2), KNOB_R + 6, KNOB_R + 6)
        path.addEllipse(QPointF(CLOSE_X, THICK / 2), CLOSE_R + 6, CLOSE_R + 6)
        path.addEllipse(QPointF(GROW_X, THICK / 2), GROW_R + 6, GROW_R + 6)
        origin = self._origin_widget()
        transform = QTransform()
        transform.translate(origin.x(), origin.y())
        transform.rotate(math.degrees(self.angle))
        return transform.map(path)

    def _apply_mask(self) -> None:
        if self._drag:
            return
        screen = self._screen_for_anchor()
        geo = screen.geometry() if screen is not None else None
        if geo is None or self.width() < geo.width() * 0.8 or self.height() < geo.height() * 0.8:
            return
        polygon = self._shape().toFillPolygon().toPolygon()
        if polygon.isEmpty():
            self.clearMask()
            return
        self.setMask(QRegion(polygon))

    def _cover_screen(self) -> None:
        screen = self._screen_for_anchor()
        if screen is None:
            return
        handle = self.windowHandle()
        if handle is not None and handle.screen() != screen:
            handle.setScreen(screen)
        if self._wayland():
            if not self.isFullScreen():
                self.showFullScreen()
            return
        if self.isFullScreen():
            self.showNormal()
        geo = screen.geometry()
        if self.geometry() != geo:
            self.setGeometry(geo)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._cover_screen()
        self.activateWindow()
        self.setFocus()
        handle = self.windowHandle()
        if handle is not None:
            handle.screenChanged.connect(self._on_screen, Qt.ConnectionType.UniqueConnection)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._ready:
            self._present()

    def _on_screen(self, _screen) -> None:
        self._update_dpi()
        self._present()

    def _present(self) -> None:
        if self._in_present or not self.isVisible():
            return
        self._in_present = True
        try:
            self.clearMask()
            self.repaint(self.rect())
        finally:
            self._in_present = False
        if self._drag is None and self._ready:
            QTimer.singleShot(0, self._apply_mask)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        origin = self._origin_widget()
        painter.translate(origin)
        painter.rotate(math.degrees(self.angle))
        self._paint_ruler(painter)
        painter.end()

    def _paint_ruler(self, painter: QPainter) -> None:
        length = self._length_px()
        body = QRectF(0, 0, length, THICK)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(8, 10, 14, 230))
        painter.drawRoundedRect(body.adjusted(0, 3, 0, 3), 4, 4)
        painter.setBrush(QColor(28, 30, 34, 250))
        painter.setPen(QPen(QColor(255, 255, 255, 90), 1))
        painter.drawRoundedRect(body, 4, 4)
        painter.setPen(QPen(QColor(255, 176, 32), 4))
        painter.drawLine(QPointF(0, 2), QPointF(length, 2))

        mm = self.px_per_cm() / 10.0
        step = 1 if mm >= 3.2 else 5
        count = int(math.floor(self.length_cm * 10 + 1e-6))
        font = QFont()
        font.setPixelSize(14)
        font.setBold(True)
        painter.setFont(font)
        upright = 90 < (math.degrees(self.angle) % 360) < 270

        for index in range(count + 1):
            if step == 5 and index % 5:
                continue
            x = index * mm
            if index % 10 == 0:
                height, color, pen_width = 26, QColor(255, 255, 255), 2
                if index == 0:
                    color = QColor(255, 176, 32)
            elif index % 5 == 0:
                height, color, pen_width = 16, QColor(220, 220, 220), 1
            else:
                height, color, pen_width = 9, QColor(170, 170, 170), 1
            painter.setPen(QPen(color, pen_width))
            painter.drawLine(QPointF(x, 0), QPointF(x, height))
            if index % 10 == 0 and x < length - 22:
                self._paint_label(painter, x, 40, str(index // 10), upright)

        unit = QFont()
        unit.setPixelSize(12)
        unit.setBold(True)
        painter.setFont(unit)
        painter.setPen(QColor(255, 176, 32))
        self._paint_label(painter, 34, 22, "cm", upright)

        if self._hint:
            hint_font = QFont()
            hint_font.setPixelSize(12)
            painter.setFont(hint_font)
            painter.setPen(QColor(255, 220, 140))
            self._paint_label(painter, length / 2, THICK - 10, self._hint, upright)

        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.setBrush(QColor(56, 132, 255))
        painter.drawEllipse(QPointF(length, THICK / 2), KNOB_R, KNOB_R)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(220, 70, 60))
        center = QPointF(CLOSE_X, THICK / 2)
        painter.drawEllipse(center, CLOSE_R, CLOSE_R)
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.drawLine(QPointF(center.x() - 4, center.y() - 4), QPointF(center.x() + 4, center.y() + 4))
        painter.drawLine(QPointF(center.x() + 4, center.y() - 4), QPointF(center.x() - 4, center.y() + 4))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(46, 160, 90))
        grow = QPointF(GROW_X, THICK / 2)
        painter.drawEllipse(grow, GROW_R, GROW_R)
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.drawLine(QPointF(grow.x() - 5, grow.y()), QPointF(grow.x() + 5, grow.y()))
        painter.drawLine(QPointF(grow.x(), grow.y() - 5), QPointF(grow.x(), grow.y() + 5))

    def _paint_label(self, painter: QPainter, x: float, y: float, text: str, upright: bool) -> None:
        painter.save()
        painter.translate(x, y)
        if upright:
            painter.rotate(180)
        painter.drawText(QRectF(-40, -10, 80, 20), Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def _set_cursor_for(self, kind: str | None) -> None:
        shapes = {
            "body": Qt.CursorShape.SizeAllCursor,
            "knob": Qt.CursorShape.CrossCursor,
            "close": Qt.CursorShape.PointingHandCursor,
            "grow": Qt.CursorShape.PointingHandCursor,
        }
        self.setCursor(shapes.get(kind, Qt.CursorShape.ArrowCursor))

    def _maybe_snap(self, angle: float, modifiers) -> float:
        if modifiers & Qt.KeyboardModifier.AltModifier:
            return angle
        step = math.pi / 4
        target = round(angle / step) * step
        if abs(angle - target) <= math.radians(4):
            return target
        return angle

    def _flash(self, text: str) -> None:
        self._hint = text
        self._hint_timer.start(1200)

    def _clear_hint(self) -> None:
        self._hint = ""
        self._present()

    def _redraw(self) -> None:
        self._update_dpi()
        self._present()
        self._schedule_save()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        kind = self._hit(*self._to_local(event.position()))
        if kind == "close":
            self.close()
            return
        if kind == "grow":
            self._set_length(self.length_cm + GROW_STEP_CM)
            return
        if kind == "knob":
            self._drag = "rotate"
        elif kind == "body":
            self._drag = "move"
            self._press_global = event.globalPosition()
            self._press_anchor = QPoint(self.anchor)
        else:
            return

    def mouseMoveEvent(self, event) -> None:
        if self._drag == "move":
            delta = event.globalPosition() - self._press_global
            self.anchor = self._press_anchor + QPoint(round(delta.x()), round(delta.y()))
            self._cover_screen()
            self._present()
            return
        if self._drag == "rotate":
            point = event.globalPosition()
            pivot = QPointF(self.anchor.x(), self.anchor.y() + THICK / 2)
            angle = math.atan2(point.y() - pivot.y(), point.x() - pivot.x())
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                distance = math.hypot(point.x() - pivot.x(), point.y() - pivot.y())
                self.length_cm = min(MAX_CM, max(MIN_CM, distance / self.px_per_cm()))
            self.angle = self._maybe_snap(angle, event.modifiers())
            degrees = math.degrees(self.angle) % 360
            self._hint = f"{degrees:.0f}° · {self._fmt_cm(self.length_cm)}"
            self._present()
            return
        self._set_cursor_for(self._hit(*self._to_local(event.position())))

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._drag:
            return
        self._drag = None
        self._hint_timer.start(900)
        self._redraw()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._hit(*self._to_local(event.position())) == "body":
            self.angle = self._maybe_snap(self.angle, Qt.KeyboardModifier.NoModifier)
            self._flash(f"{math.degrees(self.angle) % 360:.0f}°")
            self._redraw()

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            return
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            self.user_scale = min(2.0, max(0.5, self.user_scale * (1.02 ** steps)))
            self._flash(self._fmt_scale())
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            self.length_cm = min(MAX_CM, max(MIN_CM, self.length_cm + 0.5 * steps))
            self._flash(self._fmt_cm(self.length_cm))
        else:
            fine = 0.2 if mods & Qt.KeyboardModifier.AltModifier else 1.0
            self.angle += math.radians(fine) * steps
            self._flash(f"{math.degrees(self.angle) % 360:.0f}°")
        self._redraw()
        event.accept()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mods = event.modifiers()
        step = 10 if mods & Qt.KeyboardModifier.ShiftModifier else 1
        if key == Qt.Key.Key_Escape or (
            key == Qt.Key.Key_W and mods & Qt.KeyboardModifier.ControlModifier
        ):
            self.close()
            return
        if key == Qt.Key.Key_Left:
            self.anchor += QPoint(-step, 0)
        elif key == Qt.Key.Key_Right:
            self.anchor += QPoint(step, 0)
        elif key == Qt.Key.Key_Up:
            self.anchor += QPoint(0, -step)
        elif key == Qt.Key.Key_Down:
            self.anchor += QPoint(0, step)
        elif key == Qt.Key.Key_C:
            self._center_anchor()
            self._flash(self._tr("center"))
        elif key == Qt.Key.Key_H:
            self.angle = 0.0
            self._flash("0°")
        elif key == Qt.Key.Key_V:
            self.angle = math.pi / 2
            self._flash("90°")
        elif key == Qt.Key.Key_BracketLeft:
            self.length_cm = max(MIN_CM, self.length_cm - 1)
            self._flash(self._fmt_cm(self.length_cm))
        elif key == Qt.Key.Key_BracketRight:
            self.length_cm = min(MAX_CM, self.length_cm + 1)
            self._flash(self._fmt_cm(self.length_cm))
        elif mods & Qt.KeyboardModifier.ControlModifier and key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.user_scale = min(2.0, self.user_scale * 1.02)
            self._flash(self._fmt_scale())
        elif mods & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_Minus:
            self.user_scale = max(0.5, self.user_scale / 1.02)
            self._flash(self._fmt_scale())
        elif mods & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_0:
            self.user_scale = 1.0
            self._flash(self._fmt_scale())
        else:
            super().keyPressEvent(event)
            return
        self._redraw()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        motto = menu.addAction(LEMMA)
        motto.setEnabled(False)
        info = menu.addAction(f"{self._fmt_cm(self.length_cm)} · {math.degrees(self.angle) % 360:.0f}°")
        info.setEnabled(False)
        menu.addSeparator()
        menu.addAction(self._tr("center_cmd"), self._center_and_save)
        menu.addAction(self._tr("horizontal"), lambda: self._set_angle(0.0))
        menu.addAction(self._tr("vertical"), lambda: self._set_angle(math.pi / 2))
        menu.addAction(self._tr("rotate45"), lambda: self._set_angle(self.angle + math.pi / 4))
        menu.addSeparator()
        menu.addAction(self._tr("longer"), lambda: self._set_length(self.length_cm + 1))
        menu.addAction(self._tr("shorter"), lambda: self._set_length(self.length_cm - 1))
        menu.addAction(self._tr("reset_scale"), self._reset_scale)
        lang_menu = menu.addMenu(self._tr("language"))
        group = QActionGroup(menu)
        group.setExclusive(True)
        for code in ("pt", "en", "es"):
            action = lang_menu.addAction(LANGS[code]["name"])
            action.setCheckable(True)
            action.setChecked(code == self.lang)
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, chosen=code: self._set_lang(chosen))
        menu.addSeparator()
        menu.addAction(self._tr("close"), self.close)
        menu.exec(event.globalPos())

    def _set_angle(self, angle: float) -> None:
        self.angle = angle
        self._flash(f"{math.degrees(self.angle) % 360:.0f}°")
        self._redraw()

    def _set_length(self, length_cm: float) -> None:
        self.length_cm = min(MAX_CM, max(MIN_CM, length_cm))
        self._flash(self._fmt_cm(self.length_cm))
        self._redraw()

    def _reset_scale(self) -> None:
        self.user_scale = 1.0
        self._flash(self._fmt_scale())
        self._redraw()

    def _center_and_save(self) -> None:
        self._center_anchor()
        self._cover_screen()
        self._flash(self._tr("center"))
        self._redraw()

    def _set_lang(self, code: str) -> None:
        if code not in LANGS or code == self.lang:
            return
        self.lang = code
        self._apply_lang()
        self._flash(self._tr("name"))
        self._redraw()

    def _config_path(self) -> str:
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "state.json")

    def _load(self) -> None:
        self._update_dpi()
        path = self._config_path()
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            self._center_anchor()
            self._apply_lang()
            self._hint = self._tr("hint")
            self._hint_timer.start(5000)
            return
        saved = data.get("lang")
        if saved in LANGS:
            self.lang = saved
        self._apply_lang()
        if int(data.get("v", 0)) != STATE_VERSION:
            self._center_anchor()
            return
        self.anchor = QPoint(int(data.get("x", 0)), int(data.get("y", 0)))
        self.angle = float(data.get("angle", 0.0))
        self.length_cm = min(MAX_CM, max(MIN_CM, float(data.get("length_cm", 15))))
        self.user_scale = min(2.0, max(0.5, float(data.get("scale", 1.0))))
        if not any(screen.geometry().contains(self.anchor) for screen in QGuiApplication.screens()):
            self._center_anchor()

    def _schedule_save(self) -> None:
        if self._ready:
            self._save_timer.start(400)

    def _save(self) -> None:
        if not self._ready:
            return
        payload = {
            "v": STATE_VERSION,
            "x": self.anchor.x(),
            "y": self.anchor.y(),
            "angle": self.angle,
            "length_cm": self.length_cm,
            "scale": self.user_scale,
            "lang": self.lang,
        }
        path = self._config_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)

    def closeEvent(self, event) -> None:
        self._save()
        super().closeEvent(event)


def main() -> int:
    fmt = QSurfaceFormat()
    fmt.setAlphaBufferSize(8)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication(sys.argv)
    app.setOrganizationName("JARBB")
    app.setApplicationName("Ruler")
    window = Ruler()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
