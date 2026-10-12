#!/usr/bin/env python3
"""Régua de tela em centímetros.

Fica por cima das outras janelas. Arraste o corpo para mover e a ponta
para girar. A escala segue o tamanho físico do monitor.

Instalar (uma vez):
    python -m pip install -r requirements.txt

Abrir:
    python regua.py
"""

from __future__ import annotations

import json
import math
import os
import signal
import sys

from PySide6.QtCore import QPoint, QPointF, QRectF, QStandardPaths, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QTransform,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

THICK = 52
KNOB_R = 13
CLOSE_R = 11
CLOSE_X = -24
PAD = 10
MIN_CM = 3.0
MAX_CM = 200.0


def _fmt_cm(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{round(value)} cm"
    return f"{value:.1f} cm".replace(".", ",")


class Ruler(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.angle = 0.0
        self.length_cm = 15.0
        self.user_scale = 1.0
        self.anchor = QPoint(200, 160)
        self._zero = QPointF(PAD, PAD)
        self._px = 38.0
        self._drag: str | None = None
        self._press_global = QPointF()
        self._press_anchor = QPoint()
        self._applying = False
        self._ready = False
        self._hint = ""
        self._loaded = False
        self._win_origin = QPoint(0, 0)

        self.setWindowTitle("Régua")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save)
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.timeout.connect(self._clear_hint)

        self._load()
        self._ready = True
        self._sync()
        if not self._loaded:
            self._set_hint("Arraste para mover. A ponta gira. A roda afina o ângulo.")
            self._hint_timer.start(7000)

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
            logical = screen.logicalDotsPerInch() or 96.0
            px = logical / 2.54
        self._px = px

    def _compute_layout(self) -> tuple[QPointF, int, int]:
        length = self._length_px()
        samples: list[tuple[float, float]] = []

        def add_circle(cx: float, cy: float, radius: float) -> None:
            for step in range(20):
                theta = 2 * math.pi * step / 20
                samples.append((cx + radius * math.cos(theta), cy + radius * math.sin(theta)))

        for x, y in ((-4, -6), (length + 4, -6), (length + 4, THICK + 4), (-4, THICK + 4)):
            samples.append((x, y))
        add_circle(length, THICK / 2, KNOB_R + 2)
        add_circle(CLOSE_X, THICK / 2, CLOSE_R + 2)

        cos_a = math.cos(self.angle)
        sin_a = math.sin(self.angle)
        rotated = [(x * cos_a - y * sin_a, x * sin_a + y * cos_a) for x, y in samples]
        min_x = min(p[0] for p in rotated) - PAD
        min_y = min(p[1] for p in rotated) - PAD
        max_x = max(p[0] for p in rotated) + PAD
        max_y = max(p[1] for p in rotated) + PAD
        zero = QPointF(-min_x, -min_y)
        return zero, max(1, math.ceil(max_x - min_x)), max(1, math.ceil(max_y - min_y))

    def _pivot_widget(self) -> tuple[float, float]:
        return (
            self.anchor.x() - self._win_origin.x() + self._zero.x(),
            self.anchor.y() - self._win_origin.y() + self._zero.y(),
        )

    def _center_anchor(self, screen=None) -> None:
        screen = screen or QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self._update_dpi()
        length = round(self._length_px())
        self.anchor = QPoint(
            geo.x() + max(0, (geo.width() - length) // 2),
            geo.y() + max(0, (geo.height() - THICK) // 2),
        )

    def _sync(self) -> None:
        if self._applying:
            return
        self._update_dpi()
        zero, _, _ = self._compute_layout()
        self._zero = zero
        screen = self._screen_for_anchor()
        if screen is None:
            self.update()
            return
        self._win_origin = screen.geometry().topLeft()
        if not self.isVisible():
            self.update()
            return
        self._applying = True
        try:
            handle = self.windowHandle()
            if handle is not None:
                if handle.screen() != screen:
                    handle.setScreen(screen)
                if not self.isFullScreen():
                    self.showFullScreen()
            self._apply_mask()
        finally:
            self._applying = False
        self.update()

    def _apply_mask(self) -> None:
        length = self._length_px()
        local = QPainterPath()
        local.addRect(QRectF(-4, -6, length + 8, THICK + 10))
        local.addEllipse(QPointF(length, THICK / 2), KNOB_R + 2, KNOB_R + 2)
        local.addEllipse(QPointF(CLOSE_X, THICK / 2), CLOSE_R + 2, CLOSE_R + 2)
        ox, oy = self._pivot_widget()
        transform = QTransform()
        transform.translate(ox, oy)
        transform.rotate(math.degrees(self.angle))
        polygon = transform.map(local).toFillPolygon().toPolygon()
        self.setMask(polygon)

    def _clamp_anchor(self) -> None:
        if any(screen.geometry().contains(self.anchor) for screen in QGuiApplication.screens()):
            return
        self._center_anchor()

    def _to_local(self, pos: QPointF) -> tuple[float, float]:
        ox, oy = self._pivot_widget()
        dx = pos.x() - ox
        dy = pos.y() - oy
        cos_a = math.cos(self.angle)
        sin_a = math.sin(self.angle)
        return dx * cos_a + dy * sin_a, -dx * sin_a + dy * cos_a

    def _hit(self, lx: float, ly: float) -> str | None:
        length = self._length_px()
        if math.hypot(lx - length, ly - THICK / 2) <= KNOB_R + 4:
            return "knob"
        if math.hypot(lx - CLOSE_X, ly - THICK / 2) <= CLOSE_R + 4:
            return "close"
        if -4 <= lx <= length + 4 and -6 <= ly <= THICK + 4:
            return "body"
        return None

    def _set_cursor_for(self, kind: str | None) -> None:
        shapes = {
            "body": Qt.CursorShape.SizeAllCursor,
            "knob": Qt.CursorShape.CrossCursor,
            "close": Qt.CursorShape.PointingHandCursor,
        }
        self.setCursor(shapes.get(kind, Qt.CursorShape.ArrowCursor))

    def _maybe_snap(self, angle: float, modifiers) -> float:
        if modifiers & Qt.KeyboardModifier.AltModifier:
            return angle
        step = math.pi / 4
        target = round(angle / step) * step
        if abs(angle - target) <= math.radians(3):
            return target
        return angle

    def _set_angle(self, angle: float, snap: bool = False) -> None:
        if snap:
            angle = self._maybe_snap(angle, Qt.KeyboardModifier.NoModifier)
        self.angle = angle
        self._sync()
        self._schedule_save()

    def _set_length(self, length_cm: float) -> None:
        self.length_cm = min(MAX_CM, max(MIN_CM, length_cm))
        self._sync()
        self._schedule_save()

    def _set_hint(self, text: str) -> None:
        self._hint = text
        self.update()

    def _clear_hint(self) -> None:
        self._hint = ""
        self.update()

    def _flash(self, text: str) -> None:
        self._set_hint(text)
        self._hint_timer.start(1200)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._clamp_anchor()
        self._sync()
        self.raise_()
        self.activateWindow()
        handle = self.windowHandle()
        if handle is not None:
            handle.screenChanged.connect(self._on_screen, Qt.ConnectionType.UniqueConnection)

    def _on_screen(self, _screen) -> None:
        self._sync()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        ox, oy = self._pivot_widget()
        painter.translate(ox, oy)
        painter.rotate(math.degrees(self.angle))
        self._paint_ruler(painter)
        painter.resetTransform()
        if self._hint:
            self._paint_hint(painter)
        painter.end()

    def _paint_ruler(self, painter: QPainter) -> None:
        length = self._length_px()
        body = QRectF(0, 0, length, THICK)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(12, 14, 18, 210))
        painter.drawRoundedRect(body.adjusted(0, 2, 0, 2), 3, 3)
        painter.setBrush(QColor(22, 24, 28, 236))
        painter.setPen(QPen(QColor(255, 255, 255, 70), 1))
        painter.drawRoundedRect(body, 3, 3)
        painter.setPen(QPen(QColor(255, 176, 32), 3))
        painter.drawLine(QPointF(0, 1.5), QPointF(length, 1.5))

        mm = self.px_per_cm() / 10.0
        step = 1 if mm >= 3.4 else 5
        count = int(math.floor(self.length_cm * 10 + 1e-6))
        font = QFont()
        font.setPixelSize(13)
        painter.setFont(font)
        upright = 90 < (math.degrees(self.angle) % 360) < 270

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for index in range(count + 1):
            if step == 5 and index % 5:
                continue
            x = round(index * mm)
            if index % 10 == 0:
                height = 24
                color = QColor(255, 176, 32) if index == 0 else QColor(245, 245, 245)
                pen_width = 2
            elif index % 5 == 0:
                height = 16
                color = QColor(220, 220, 220)
                pen_width = 1
            else:
                height = 8
                color = QColor(190, 190, 190)
                pen_width = 1
            painter.setPen(QPen(color, pen_width))
            painter.drawLine(x, 0, x, height)
            if index % 10 == 0 and x < length - 18:
                self._paint_label(painter, x, 36, str(index // 10), upright)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        unit_font = QFont()
        unit_font.setPixelSize(11)
        painter.setFont(unit_font)
        painter.setPen(QColor(255, 176, 32))
        self._paint_label(painter, 28, 22, "cm", upright)

        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.setBrush(QColor(76, 141, 255))
        painter.drawEllipse(QPointF(length, THICK / 2), KNOB_R, KNOB_R)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(232, 93, 76))
        painter.drawEllipse(QPointF(CLOSE_X, THICK / 2), CLOSE_R, CLOSE_R)
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        c = QPointF(CLOSE_X, THICK / 2)
        painter.drawLine(QPointF(c.x() - 4, c.y() - 4), QPointF(c.x() + 4, c.y() + 4))
        painter.drawLine(QPointF(c.x() + 4, c.y() - 4), QPointF(c.x() - 4, c.y() + 4))

    def _paint_label(self, painter: QPainter, x: float, y: float, text: str, upright: bool) -> None:
        painter.save()
        painter.translate(x, y)
        if upright:
            painter.rotate(180)
        painter.drawText(QRectF(-18, -9, 36, 18), Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def _paint_hint(self, painter: QPainter) -> None:
        font = QFont()
        font.setPixelSize(12)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(self._hint) + 16
        height = 22
        rect = QRectF(8, self.height() - height - 6, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(12, 14, 18, 220))
        painter.drawRoundedRect(rect, 6, 6)
        painter.setPen(QColor(245, 245, 245))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._hint)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        kind = self._hit(*self._to_local(event.position()))
        if kind == "close":
            self.close()
            return
        if kind == "knob":
            self._drag = "rotate"
            self.grabMouse()
            return
        if kind == "body":
            self._press_global = event.globalPosition()
            self._press_anchor = QPoint(self.anchor)
            self._drag = "move"
            self.grabMouse()

    def mouseMoveEvent(self, event) -> None:
        if self._drag == "move":
            delta = event.globalPosition() - self._press_global
            self.anchor = self._press_anchor + QPoint(round(delta.x()), round(delta.y()))
            self._sync()
            return
        if self._drag == "rotate":
            point = event.globalPosition()
            angle = math.atan2(point.y() - self.anchor.y(), point.x() - self.anchor.x())
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                distance = math.hypot(point.x() - self.anchor.x(), point.y() - self.anchor.y())
                self.length_cm = min(MAX_CM, max(MIN_CM, distance / self.px_per_cm()))
            self.angle = self._maybe_snap(angle, event.modifiers())
            degrees = math.degrees(self.angle) % 360
            self._set_hint(f"{degrees:.0f}° · {_fmt_cm(self.length_cm)}")
            self._sync()
            return
        if self._drag is None:
            self._set_cursor_for(self._hit(*self._to_local(event.position())))

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag in {"move", "rotate"}:
            self.releaseMouse()
        if self._drag:
            self._schedule_save()
        self._drag = None

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._hit(*self._to_local(event.position())) == "body":
            self._set_angle(self._maybe_snap(self.angle, Qt.KeyboardModifier.NoModifier), snap=False)
            degrees = math.degrees(self.angle) % 360
            self._flash(f"{degrees:.0f}°")

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            return
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            self.user_scale = min(2.0, max(0.5, self.user_scale * (1.02 ** steps)))
            self._flash(f"escala {self.user_scale * 100:.0f}%")
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            self._set_length(self.length_cm + 0.5 * steps)
            self._flash(_fmt_cm(self.length_cm))
        else:
            fine = 0.2 if mods & Qt.KeyboardModifier.AltModifier else 1.0
            self.angle += math.radians(fine) * steps
            degrees = math.degrees(self.angle) % 360
            self._flash(f"{degrees:.0f}°")
            self._sync()
            self._schedule_save()
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
            self._center_and_sync()
            self._flash("centro")
            return
        elif key == Qt.Key.Key_H:
            self._set_angle(0)
            self._flash("0°")
            return
        elif key == Qt.Key.Key_V:
            self._set_angle(math.pi / 2)
            self._flash("90°")
            return
        elif key == Qt.Key.Key_BracketLeft:
            self._set_length(self.length_cm - 1)
            self._flash(_fmt_cm(self.length_cm))
            return
        elif key == Qt.Key.Key_BracketRight:
            self._set_length(self.length_cm + 1)
            self._flash(_fmt_cm(self.length_cm))
            return
        elif mods & Qt.KeyboardModifier.ControlModifier and key in (
            Qt.Key.Key_Plus,
            Qt.Key.Key_Equal,
        ):
            self.user_scale = min(2.0, self.user_scale * 1.02)
            self._flash(f"escala {self.user_scale * 100:.0f}%")
            self._sync()
            self._schedule_save()
            return
        elif mods & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_Minus:
            self.user_scale = max(0.5, self.user_scale / 1.02)
            self._flash(f"escala {self.user_scale * 100:.0f}%")
            self._sync()
            self._schedule_save()
            return
        elif mods & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_0:
            self.user_scale = 1.0
            self._flash("escala 100%")
            self._sync()
            self._schedule_save()
            return
        else:
            super().keyPressEvent(event)
            return
        self._sync()
        self._schedule_save()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        info = menu.addAction(f"{_fmt_cm(self.length_cm)} · {math.degrees(self.angle) % 360:.0f}°")
        info.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Horizontal", lambda: self._set_angle(0))
        menu.addAction("Vertical", lambda: self._set_angle(math.pi / 2))
        menu.addAction("Girar 45°", lambda: self._set_angle(self.angle + math.pi / 4))
        menu.addAction("Centralizar", self._center_and_sync)
        menu.addSeparator()
        menu.addAction("Mais longa", lambda: self._set_length(self.length_cm + 1))
        menu.addAction("Mais curta", lambda: self._set_length(self.length_cm - 1))
        menu.addAction("Resetar escala", self._reset_scale)
        menu.addSeparator()
        menu.addAction("Fechar", self.close)
        menu.exec(event.globalPos())

    def _reset_scale(self) -> None:
        self.user_scale = 1.0
        self._flash("escala 100%")
        self._sync()
        self._schedule_save()

    def _center_and_sync(self) -> None:
        self._center_anchor()
        self._sync()
        self._schedule_save()

    def _config_path(self) -> str:
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "state.json")

    def _load(self) -> None:
        path = self._config_path()
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            self._center_anchor()
            return
        self.anchor = QPoint(int(data.get("x", self.anchor.x())), int(data.get("y", self.anchor.y())))
        self.angle = float(data.get("angle", 0.0))
        self.length_cm = min(MAX_CM, max(MIN_CM, float(data.get("length_cm", 15))))
        self.user_scale = min(2.0, max(0.5, float(data.get("scale", 1.0))))
        self._loaded = True

    def _schedule_save(self) -> None:
        if self._ready:
            self._save_timer.start(400)

    def _save(self) -> None:
        if not self._ready:
            return
        payload = {
            "x": self.anchor.x(),
            "y": self.anchor.y(),
            "angle": self.angle,
            "length_cm": self.length_cm,
            "scale": self.user_scale,
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
    # Permite Ctrl+C no terminal enquanto o Qt segura o processo.
    signal.signal(signal.SIGINT, lambda *_: QApplication.quit())
    app = QApplication(sys.argv)
    app.setOrganizationName("regua")
    app.setApplicationName("regua")
    app.setApplicationDisplayName("Régua")
    wake = QTimer()
    wake.start(200)
    wake.timeout.connect(lambda: None)
    Ruler().show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
