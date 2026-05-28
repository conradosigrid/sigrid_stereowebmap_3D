"""
stereo_canvas_options.py

Handles per-layer visibility overrides for stereo canvases.

Default behavior follows the main canvas visibility. Users can override it
from the layer-tree context menu action "Visible in stereo canvases".
"""

import json
import os
import re
from typing import Callable, Dict, Optional, Tuple

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QSize, Qt, QSignalBlocker
from qgis.PyQt.QtGui import QAction, QIcon, QColor
from qgis.PyQt.QtWidgets import QLabel, QDoubleSpinBox, QHBoxLayout, QToolBar, QToolButton, QWidget, QSizePolicy, QStyledItemDelegate, QStyleOptionViewItem, QStyle

from .utils import is_sgd_swm_layer


class SwmLayerHighlightDelegate(QStyledItemDelegate):
    """Paints a yellow background for SWM layers in the QGIS layer panel."""

    def __init__(
        self,
        layer_state_resolver: Optional[Callable[[str], Optional[Tuple[bool, bool, bool]]]] = None,
        layer_toggle_handler: Optional[Callable[[str, bool], bool]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._layer_state_resolver = layer_state_resolver
        self._highlight_color = QColor(255, 255, 128, 160)
        self._stereo_tint_color = QColor(0, 0, 255)
        # Stereo text marker mode:
        # - "strikeout": stable mode (recommended)
        # - "tint": color text when stereo is visible
        # - "box": experimental mode
        # To revert quickly, change only this value to "strikeout".
        self._stereo_text_marker_mode = "strikeout"

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        stereo_visible = False
        draw_stereo_text_box = False
        is_swm = False
        try:
            layer_name = str(index.data(Qt.ItemDataRole.DisplayRole) or "").strip()

            layer_state = self._resolve_layer_state(layer_name)
            if layer_state is not None:
                is_swm, _main_visible, stereo_visible = layer_state
            else:
                is_swm = bool(layer_name and layer_name in self._get_swm_layer_names())

            if is_swm:
                painter.save()
                painter.fillRect(option.rect, self._highlight_color)
                painter.restore()

            if layer_state is not None:
                draw_stereo_text_box = bool(stereo_visible and layer_name)
        except Exception:
            pass

        if draw_stereo_text_box and self._stereo_text_marker_mode == "strikeout":
            font = opt.font
            font.setStrikeOut(True)
            opt.font = font

        if draw_stereo_text_box and self._stereo_text_marker_mode == "tint":
            # Some QGIS styles ignore delegate palette overrides for certain rows.
            # Keep default painting and force a deterministic blue text repaint below.
            pass

        super().paint(painter, opt, index)

        if draw_stereo_text_box and self._stereo_text_marker_mode == "tint":
            self._draw_stereo_tinted_text(painter, opt, index, is_swm)

        if draw_stereo_text_box and self._stereo_text_marker_mode == "box":
            self._draw_stereo_text_box(painter, opt, index)

    def _draw_stereo_tinted_text(self, painter, option, index, is_swm: bool):
        try:
            widget = option.widget
            style = widget.style() if widget else None
            if style is None:
                return

            text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, option, widget)
            if not text_rect.isValid() or text_rect.width() <= 0:
                return

            text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            if not text:
                return

            # Clear only the text area, preserving checkbox/icon layout.
            painter.save()
            if option.state & QStyle.StateFlag.State_Selected:
                bg_color = option.palette.color(option.palette.ColorRole.Highlight)
            elif is_swm:
                bg_color = self._highlight_color
            else:
                bg_color = option.palette.color(option.palette.ColorRole.Base)
            painter.fillRect(text_rect, bg_color)

            painter.setFont(option.font)
            painter.setPen(self._stereo_tint_color)
            elided = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), elided)
            painter.restore()
        except Exception:
            pass

    def _draw_stereo_text_box(self, painter, option, index):
        try:
            widget = option.widget
            style = widget.style() if widget else None
            if style is None:
                return

            text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, option, widget)
            if not text_rect.isValid() or text_rect.width() <= 0:
                return
            deco_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemDecoration, option, widget)
            check_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, widget)

            text = str(index.data(Qt.ItemDataRole.DisplayRole) or "").strip()
            if not text:
                return

            painter.save()
            fm = painter.fontMetrics()
            # Force box start after checkbox/icon, so only the label is enclosed.
            text_left = int(text_rect.left())
            if check_rect.isValid() and check_rect.width() > 0:
                text_left = max(text_left, int(check_rect.right() + 4))
            if deco_rect.isValid() and deco_rect.width() > 0:
                text_left = max(text_left, int(deco_rect.right() + 4))

            available_text_width = max(6, int(text_rect.right() - text_left))
            elided = fm.elidedText(text, Qt.TextElideMode.ElideRight, available_text_width)
            text_width = max(1, fm.horizontalAdvance(elided))

            # Build a geometry tied to text metrics so the box encloses only text,
            # not checkbox/icon. Keep a small inner padding for readability.
            pad_x = 3
            pad_y = 1
            desired_width = int(text_width + (pad_x * 2))
            max_width = max(6, available_text_width)
            box_width = max(6, min(desired_width, max_width))

            box_height = max(6, int(fm.height() + (pad_y * 2)))
            center_y = int(option.rect.center().y())
            y = int(center_y - (box_height // 2))
            y = max(option.rect.top() + 1, min(y, option.rect.bottom() - box_height))

            x = int(text_left - 1)
            right_limit = int(text_rect.right() - 1)
            if x + box_width > right_limit:
                box_width = max(6, right_limit - x)

            box_rect = option.rect.adjusted(0, 0, 0, 0)
            box_rect.setLeft(x)
            box_rect.setTop(y)
            box_rect.setWidth(box_width)
            box_rect.setHeight(box_height)

            if option.state & QStyle.StateFlag.State_Selected:
                pen_color = option.palette.color(option.palette.ColorRole.HighlightedText)
            else:
                pen_color = QColor(50, 50, 50)
            painter.setPen(pen_color)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # drawRect includes the right/bottom edges, so shrink by 1 to avoid clipping.
            painter.drawRect(box_rect.adjusted(0, 0, -1, -1))
            painter.restore()
        except Exception:
            pass

    def _resolve_layer_state(self, layer_name: str) -> Optional[Tuple[bool, bool, bool]]:
        if not self._layer_state_resolver or not layer_name:
            return None
        try:
            return self._layer_state_resolver(layer_name)
        except Exception:
            return None

    def _get_swm_layer_names(self) -> set[str]:
        """Returns names of SWM layers present in the current project."""
        names: set[str] = set()
        try:
            project = QgsProject.instance()
            if project:
                for layer in project.mapLayers().values():
                    if layer and is_sgd_swm_layer(layer):
                        layer_name = str(layer.name()).strip()
                        if layer_name:
                            names.add(layer_name)
        except Exception:
            pass
        return names

    def _get_project_layer_names(self) -> set[str]:
        names: set[str] = set()
        try:
            project = QgsProject.instance()
            if project:
                for layer in project.mapLayers().values():
                    if layer:
                        layer_name = str(layer.name()).strip()
                        if layer_name:
                            names.add(layer_name)
        except Exception:
            pass
        return names


class StereoCanvasOptions:
    """Keeps stereo-visibility overrides and injects context-menu action."""

    _PROJECT_SCOPE = "SWM-3D"
    _PROJECT_KEY_OVERRIDES = "stereo_visibility_overrides"

    def __init__(self, iface, on_visibility_changed: Optional[Callable[[str, bool], None]] = None):
        self.iface = iface
        self._on_visibility_changed = on_visibility_changed
        self._visibility_overrides: Dict[str, bool] = {}
        self._context_menu_connected = False
        self._highlight_delegate_installed = False
        self._original_item_delegate = None
        self._swm_highlight_delegate = None
        self._load_from_project()

    def _resolve_layer_state_by_name(self, layer_name: str) -> Optional[Tuple[bool, bool, bool]]:
        """Returns (is_swm, main_visible, stereo_visible) for one layer-tree display name."""
        name = str(layer_name or "").strip()
        if not name:
            return None

        project = QgsProject.instance()
        if not project:
            return None

        layers = project.mapLayersByName(name)
        if not layers:
            # Layer tree can append suffixes like "[7]" to duplicated names.
            base_name = re.sub(r"\s*\[\d+\]\s*$", "", name).strip()
            if base_name and base_name != name:
                layers = project.mapLayersByName(base_name)
        if not layers:
            return None

        layer = layers[0]
        main_visible = self._is_layer_visible_in_main_tree(layer)
        stereo_visible = self.is_layer_visible_in_stereo(layer, main_visible)
        return (bool(is_sgd_swm_layer(layer)), bool(main_visible), bool(stereo_visible))

    def _toggle_layer_stereo_visibility_by_name(self, layer_name: str, stereo_visible: bool) -> bool:
        """Applies a stereo-visibility change for a layer row name. Returns True if changed."""
        layer_state = self._resolve_layer_state_by_name(layer_name)
        if layer_state is None:
            return False

        name = str(layer_name or "").strip()
        project = QgsProject.instance()
        if not project:
            return False

        layers = project.mapLayersByName(name)
        if not layers:
            base_name = re.sub(r"\s*\[\d+\]\s*$", "", name).strip()
            if base_name and base_name != name:
                layers = project.mapLayersByName(base_name)
        if not layers:
            return False

        before_signature = self.signature_fragment()
        self._on_toggle_layer_stereo_visibility(layers[0], bool(stereo_visible))
        return before_signature != self.signature_fragment()

    def setup_context_menu(self):
        """Connect layer-tree hooks once."""
        layer_tree_view = self.iface.layerTreeView() if self.iface else None
        if not layer_tree_view:
            return

        if not self._highlight_delegate_installed:
            try:
                self._original_item_delegate = layer_tree_view.itemDelegate()
            except Exception:
                self._original_item_delegate = None
            self._swm_highlight_delegate = SwmLayerHighlightDelegate(
                self._resolve_layer_state_by_name,
                self._toggle_layer_stereo_visibility_by_name,
                layer_tree_view,
            )
            layer_tree_view.setItemDelegate(self._swm_highlight_delegate)
            self._highlight_delegate_installed = True
            try:
                viewport = layer_tree_view.viewport()
                if viewport is not None:
                    viewport.update()
            except Exception:
                pass

        if not self._context_menu_connected and hasattr(layer_tree_view, "contextMenuAboutToShow"):
            layer_tree_view.contextMenuAboutToShow.connect(self._on_context_menu_about_to_show)
            self._context_menu_connected = True

    def cleanup(self):
        """Disconnect hooks and clear local state."""
        layer_tree_view = self.iface.layerTreeView() if self.iface else None
        if layer_tree_view and self._context_menu_connected and hasattr(layer_tree_view, "contextMenuAboutToShow"):
            try:
                layer_tree_view.contextMenuAboutToShow.disconnect(self._on_context_menu_about_to_show)
            except (RuntimeError, TypeError):
                pass

        if layer_tree_view and self._highlight_delegate_installed:
            try:
                if self._original_item_delegate is not None:
                    layer_tree_view.setItemDelegate(self._original_item_delegate)
                viewport = layer_tree_view.viewport()
                if viewport is not None:
                    viewport.update()
            except Exception:
                pass
            try:
                if self._swm_highlight_delegate is not None:
                    self._swm_highlight_delegate.deleteLater()
            except Exception:
                pass

        self._context_menu_connected = False
        self._highlight_delegate_installed = False
        self._original_item_delegate = None
        self._swm_highlight_delegate = None
        self._visibility_overrides.clear()

    def clear_overrides(self):
        """Removes all stereo visibility overrides and persists the empty state."""
        if not self._visibility_overrides:
            return False

        self._visibility_overrides.clear()
        self._save_to_project()
        return True

    def has_overrides(self) -> bool:
        return bool(self._visibility_overrides)

    def is_layer_visible_in_stereo(self, layer, main_visible: bool) -> bool:
        """Return effective stereo visibility for a layer."""
        if not layer or not hasattr(layer, "id"):
            return bool(main_visible)

        layer_id = str(layer.id())
        if layer_id in self._visibility_overrides:
            return bool(self._visibility_overrides[layer_id])

        return bool(main_visible)

    def signature_fragment(self) -> Tuple[Tuple[str, bool], ...]:
        """Stable, hashable fragment to include in layer-sync dedupe signature."""
        items = sorted((str(layer_id), bool(visible)) for layer_id, visible in self._visibility_overrides.items())
        return tuple(items)

    def _on_context_menu_about_to_show(self, menu):
        """Injects the stereo visibility toggle into layer context menu."""
        if menu is None:
            return

        layer = self._get_current_layer()
        if not layer:
            return

        for existing_action in menu.actions():
            if existing_action and existing_action.text() == "Visible in stereo canvases":
                return

        main_visible = self._is_layer_visible_in_main_tree(layer)
        current_visible = self.is_layer_visible_in_stereo(layer, main_visible)

        action = QAction("Visible in stereo canvases", menu)
        action.setCheckable(True)
        action.setChecked(bool(current_visible))
        action.toggled.connect(lambda checked, lyr=layer: self._on_toggle_layer_stereo_visibility(lyr, bool(checked)))

        existing_actions = menu.actions()
        if existing_actions:
            menu.insertAction(existing_actions[0], action)
        else:
            menu.addAction(action)

    def _on_toggle_layer_stereo_visibility(self, layer, stereo_visible: bool):
        if not layer or not hasattr(layer, "id"):
            return

        layer_id = str(layer.id())
        main_visible = self._is_layer_visible_in_main_tree(layer)

        # If user sets the same state as main visibility, clear override and go back to default behavior.
        changed = False
        if bool(stereo_visible) == bool(main_visible):
            if layer_id in self._visibility_overrides:
                self._visibility_overrides.pop(layer_id, None)
                changed = True
        else:
            previous = self._visibility_overrides.get(layer_id)
            if previous is None or bool(previous) != bool(stereo_visible):
                self._visibility_overrides[layer_id] = bool(stereo_visible)
                changed = True

        if changed:
            self._save_to_project()
            layer_tree_view = self.iface.layerTreeView() if self.iface else None
            if layer_tree_view and hasattr(layer_tree_view, "viewport"):
                try:
                    viewport = layer_tree_view.viewport()
                    if viewport is not None:
                        viewport.update()
                except Exception:
                    pass

        if changed and self._on_visibility_changed:
            self._on_visibility_changed(layer_id, bool(stereo_visible))

    def _save_to_project(self):
        """Persists overrides in current QGIS project."""
        try:
            project = QgsProject.instance()
            if not project:
                return

            data = {str(layer_id): bool(visible) for layer_id, visible in self._visibility_overrides.items()}
            txt = json.dumps(data, separators=(",", ":"), sort_keys=True)
            project.writeEntry(self._PROJECT_SCOPE, self._PROJECT_KEY_OVERRIDES, txt)
        except Exception:
            pass

    def _load_from_project(self):
        """Loads persisted overrides from current QGIS project."""
        try:
            project = QgsProject.instance()
            if not project:
                return

            txt, ok = project.readEntry(self._PROJECT_SCOPE, self._PROJECT_KEY_OVERRIDES, "")
            if not ok or not txt:
                return

            parsed = json.loads(txt)
            if not isinstance(parsed, dict):
                return

            restored: Dict[str, bool] = {}
            for layer_id, visible in parsed.items():
                if not isinstance(layer_id, str):
                    continue
                restored[layer_id] = bool(visible)

            self._visibility_overrides = restored
        except Exception:
            # Corrupted value or incompatible format: ignore and keep defaults.
            self._visibility_overrides = {}

    def _get_current_layer(self):
        layer_tree_view = self.iface.layerTreeView() if self.iface else None
        if layer_tree_view and hasattr(layer_tree_view, "currentLayer"):
            try:
                layer = layer_tree_view.currentLayer()
                if layer:
                    return layer
            except Exception:
                pass

        if self.iface and hasattr(self.iface, "activeLayer"):
            try:
                return self.iface.activeLayer()
            except Exception:
                return None
        return None

    @staticmethod
    def _is_layer_visible_in_main_tree(layer) -> bool:
        if not layer or not hasattr(layer, "id"):
            return False

        project = QgsProject.instance()
        if not project:
            return True

        root = project.layerTreeRoot()
        if not root:
            return True

        node = root.findLayer(layer.id())
        if node is None:
            return True

        try:
            return bool(node.isVisible())
        except Exception:
            return True


class StereoCanvasToolbar:
    """Compact toolbar row to control the stereo plugin."""

    def __init__(self, window):
        self.window = window
        self.toolbar = None
        self._installed = False
        self._icon_label = None
        self._activation_scale_spin = None
        self._rotation_threshold_spin = None
        self._current_rotation_label = None
        self._z_status_label = None
        self._build()

    def _build(self):
        main_window = self.window.iface.mainWindow() if self.window and self.window.iface else None
        if not main_window:
            return

        toolbar = QToolBar("SWM-3D Controls", main_window)
        toolbar.setObjectName("SWM3DControlsToolbar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.setIconSize(QSize(24, 24))

        icon_label = QLabel()
        icon = QIcon(self._icon_path())
        if not icon.isNull():
            icon_label.setPixmap(icon.pixmap(24, 24))
        icon_label.setToolTip("SWM-3D stereo controls")
        toolbar.addWidget(icon_label)
        toolbar.addSeparator()

        activation_widget = QWidget()
        activation_layout = QHBoxLayout(activation_widget)
        activation_layout.setContentsMargins(0, 0, 0, 0)
        activation_layout.setSpacing(4)
        activation_layout.addWidget(QLabel("Stereo active scale"))

        activation_spin = QDoubleSpinBox()
        activation_spin.setDecimals(0)
        activation_spin.setRange(1.0, 1000000000.0)
        activation_spin.setSingleStep(1000.0)
        activation_spin.setFixedWidth(120)
        activation_spin.valueChanged.connect(self._on_activation_scale_changed)
        activation_layout.addWidget(activation_spin)
        toolbar.addWidget(activation_widget)

        toolbar.addSeparator()

        rotation_widget = QWidget()
        rotation_layout = QHBoxLayout(rotation_widget)
        rotation_layout.setContentsMargins(0, 0, 0, 0)
        rotation_layout.setSpacing(4)
        rotation_layout.addWidget(QLabel("Rotate canvas level"))

        rotation_spin = QDoubleSpinBox()
        rotation_spin.setDecimals(1)
        rotation_spin.setRange(0.0, 180.0)
        rotation_spin.setSingleStep(0.5)
        rotation_spin.setSuffix(" deg")
        rotation_spin.setFixedWidth(100)
        rotation_spin.valueChanged.connect(self._on_rotation_threshold_changed)
        rotation_layout.addWidget(rotation_spin)

        current_rotation_label = QLabel("Current rot: 0.0°")
        current_rotation_label.setMinimumWidth(120)
        rotation_layout.addWidget(current_rotation_label)
        toolbar.addWidget(rotation_widget)

        toolbar.addSeparator()
        z_status_label = QLabel("Zbase=---- Zcurs=----")
        z_status_label.setMinimumWidth(170)
        toolbar.addWidget(z_status_label)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        close_button = QToolButton()
        close_icon = toolbar.style().standardIcon(toolbar.style().StandardPixmap.SP_DockWidgetCloseButton)
        close_button.setIcon(close_icon)
        close_button.setText("Close")
        close_button.setToolTip("Close the SWM-3D plugin window")
        close_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        close_button.clicked.connect(self._close_plugin)
        toolbar.addWidget(close_button)

        self.toolbar = toolbar
        self._icon_label = icon_label
        self._activation_scale_spin = activation_spin
        self._rotation_threshold_spin = rotation_spin
        self._current_rotation_label = current_rotation_label
        self._z_status_label = z_status_label
        self.refresh()

    def install(self):
        if self._installed:
            return

        if not self.toolbar:
            self._build()

        if not self.toolbar or not self.window or not self.window.iface:
            return

        main_window = self.window.iface.mainWindow()
        if not main_window:
            return

        try:
            main_window.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)
        except TypeError:
            main_window.addToolBar(self.toolbar)
        self._installed = True

    def cleanup(self):
        if not self.toolbar or not self.window or not self.window.iface:
            self._icon_label = None
            self._activation_scale_spin = None
            self._rotation_threshold_spin = None
            self._current_rotation_label = None
            self._z_status_label = None
            return

        main_window = self.window.iface.mainWindow()
        if not main_window:
            return

        try:
            main_window.removeToolBar(self.toolbar)
        except Exception:
            pass
        self.toolbar.deleteLater()
        self.toolbar = None
        self._installed = False
        self._icon_label = None
        self._activation_scale_spin = None
        self._rotation_threshold_spin = None
        self._current_rotation_label = None
        self._z_status_label = None

    def refresh(self):
        if not self.window or self.toolbar is None:
            return

        if self._activation_scale_spin is not None:
            blocker = QSignalBlocker(self._activation_scale_spin)
            self._activation_scale_spin.setValue(float(self.window._stereo_activation_scale_threshold))
            del blocker

        if self._rotation_threshold_spin is not None:
            blocker = QSignalBlocker(self._rotation_threshold_spin)
            self._rotation_threshold_spin.setValue(float(self.window._flight_rotation_threshold_deg))
            del blocker

        if self._current_rotation_label is not None:
            self._current_rotation_label.setText(
                f"Current rot: {float(self.window._flight_rotation_current_deg):.1f}°"
            )

        self.set_z_status(float(self.window._z_proj_plane), float(self.window._z_cursor))

    def set_z_status(self, z_base: float, z_cursor: float):
        if self._z_status_label is not None:
            self._z_status_label.setText(f"Zbase={float(z_base):.1f} Zcurs={float(z_cursor):.1f}")

    def _on_activation_scale_changed(self, value):
        if self.window:
            self.window._set_stereo_activation_scale_threshold(float(value))

    def _on_rotation_threshold_changed(self, value):
        if self.window:
            self.window._set_flight_rotation_threshold_deg(float(value))

    def _close_plugin(self):
        if self.window:
            self.window.close()

    @staticmethod
    def _icon_path() -> str:
        return os.path.join(os.path.dirname(__file__), "icons", "anaglyph_glasses.svg")
