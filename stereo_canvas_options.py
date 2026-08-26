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

from qgis.core import QgsProject, QgsLayerTreeGroup, QgsLayerTreeLayer, Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QSize, Qt, QSignalBlocker, QRect, QEvent, QTimer, QSettings, QT_VERSION_STR
from qgis.PyQt.QtGui import QIcon, QColor, QDoubleValidator
from qgis.PyQt.QtWidgets import QLabel, QLineEdit, QHBoxLayout, QMenu, QAction, QWidgetAction, QToolButton, QWidget, QSizePolicy, QStyledItemDelegate, QStyleOptionViewItem, QStyle, QStyleOptionButton, QFrame, QToolBar

from .utils import is_sgd_swm_layer


class SwmLayerHighlightDelegate(QStyledItemDelegate):
    """Paints a yellow background for SWM layers in the QGIS layer panel."""

    # RETURN KEY (known-good baseline):
    # SWM_STEREO_WMS_LEFT_CHECKBOX_BASELINE_2026_05_29
    # Core idea:
    # - left_checkbox rendering
    # - viewport eventFilter capturing press/release on stereo checkbox hitbox
    # - no geometry shifting to the right

    def __init__(
        self,
        layer_state_resolver: Optional[Callable[[str], Optional[Tuple[bool, bool, bool]]]] = None,
        layer_toggle_handler: Optional[Callable[[str, bool], bool]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._layer_state_resolver = layer_state_resolver
        self._layer_toggle_handler = layer_toggle_handler
        self._highlight_color = QColor(255, 255, 128, 160)
        self._stereo_tint_color = QColor(0, 0, 255)
        self._stereo_checkbox_size = 12
        self._stereo_checkbox_gap = 3
        self._layer_tree_view = None
        self._pressed_on_stereo_checkbox = False
        # Stereo text marker mode:
        # - "left_checkbox": stereo toggle at left of native checkbox (layers only)
        # - "strikeout": stable mode (recommended)
        # - "tint": color text when stereo is visible
        # - "box": experimental mode
        # To revert quickly, change only this value to "strikeout".
        # Working baseline key: SWM_STEREO_WMS_LEFT_CHECKBOX_BASELINE_2026_05_29
        self._stereo_text_marker_mode = "left_checkbox"

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        stereo_visible = False
        has_layer_row = False
        is_swm = False
        try:
            layer_name = str(index.data(Qt.ItemDataRole.DisplayRole) or "").strip()

            layer_state = self._resolve_layer_state(layer_name)
            if layer_state is not None:
                is_swm, _main_visible, stereo_visible = layer_state
                has_layer_row = bool(layer_name)
            else:
                is_swm = bool(layer_name and layer_name in self._get_swm_layer_names())

            if is_swm:
                painter.save()
                painter.fillRect(option.rect, self._highlight_color)
                painter.restore()

        except Exception:
            pass

        if stereo_visible and self._stereo_text_marker_mode == "strikeout":
            font = opt.font
            font.setStrikeOut(True)
            opt.font = font

        if stereo_visible and self._stereo_text_marker_mode == "tint":
            # Some QGIS styles ignore delegate palette overrides for certain rows.
            # Keep default painting and force a deterministic blue text repaint below.
            pass

        super().paint(painter, opt, index)

        if has_layer_row and self._stereo_text_marker_mode == "left_checkbox":
            self._draw_left_stereo_checkbox(painter, opt, index, bool(stereo_visible))

        if stereo_visible and self._stereo_text_marker_mode == "tint":
            self._draw_stereo_tinted_text(painter, opt, index, is_swm)

        if stereo_visible and self._stereo_text_marker_mode == "box":
            self._draw_stereo_text_box(painter, opt, index)

    def editorEvent(self, event, model, option, index):
        try:
            if self._stereo_text_marker_mode != "left_checkbox":
                return super().editorEvent(event, model, option, index)
            if event.type() not in (event.Type.MouseButtonPress, event.Type.MouseButtonRelease):
                return super().editorEvent(event, model, option, index)
            if not hasattr(event, "button") or event.button() != Qt.MouseButton.LeftButton:
                return super().editorEvent(event, model, option, index)

            layer_name = str(index.data(Qt.ItemDataRole.DisplayRole) or "").strip()
            layer_state = self._resolve_layer_state(layer_name)
            if not layer_state:
                return super().editorEvent(event, model, option, index)

            _is_swm, _main_visible, stereo_visible = layer_state
            box_rect = self._left_stereo_checkbox_rect(option, index)
            hit_rect = box_rect.adjusted(-2, -2, 2, 2)

            if hasattr(event, "position"):
                click_pos = event.position().toPoint()
            elif hasattr(event, "pos"):
                click_pos = event.pos()
            else:
                return super().editorEvent(event, model, option, index)

            if not hit_rect.contains(click_pos):
                return super().editorEvent(event, model, option, index)

            # Consume press so the tree view does not interpret this click as
            # a disclosure-triangle expand/collapse action.
            if event.type() == event.Type.MouseButtonPress:
                return True

            if self._layer_toggle_handler:
                changed = self._layer_toggle_handler(layer_name, not bool(stereo_visible))
                if changed and option.widget:
                    option.widget.update()
            return True
        except Exception:
            return super().editorEvent(event, model, option, index)

    def attach_to_layer_tree(self, layer_tree_view):
        """Installs viewport-level click handling for stereo checkbox interaction."""
        self.detach_from_layer_tree()
        self._layer_tree_view = layer_tree_view
        try:
            viewport = layer_tree_view.viewport() if layer_tree_view else None
            if viewport is not None:
                viewport.installEventFilter(self)
        except Exception:
            pass

    def detach_from_layer_tree(self):
        try:
            if self._layer_tree_view is not None:
                viewport = self._layer_tree_view.viewport()
                if viewport is not None:
                    viewport.removeEventFilter(self)
        except Exception:
            pass
        self._layer_tree_view = None
        self._pressed_on_stereo_checkbox = False

    def eventFilter(self, watched, event):
        try:
            if self._stereo_text_marker_mode != "left_checkbox":
                return super().eventFilter(watched, event)
            if self._layer_tree_view is None:
                return super().eventFilter(watched, event)
            viewport = self._layer_tree_view.viewport()
            if watched is not viewport:
                return super().eventFilter(watched, event)

            if event.type() not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
                return super().eventFilter(watched, event)
            if not hasattr(event, "button") or event.button() != Qt.MouseButton.LeftButton:
                return super().eventFilter(watched, event)

            if hasattr(event, "position"):
                pos = event.position().toPoint()
            elif hasattr(event, "pos"):
                pos = event.pos()
            else:
                return super().eventFilter(watched, event)

            index = self._layer_tree_view.indexAt(pos)
            if not index.isValid():
                self._pressed_on_stereo_checkbox = False
                return super().eventFilter(watched, event)

            layer_name = str(index.data(Qt.ItemDataRole.DisplayRole) or "").strip()
            layer_state = self._resolve_layer_state(layer_name)
            if not layer_state:
                self._pressed_on_stereo_checkbox = False
                return super().eventFilter(watched, event)

            _is_swm, _main_visible, stereo_visible = layer_state
            opt = QStyleOptionViewItem()
            opt.rect = self._layer_tree_view.visualRect(index)
            opt.widget = self._layer_tree_view
            box_rect = self._left_stereo_checkbox_rect(opt, index)
            hit_rect = box_rect.adjusted(-2, -2, 2, 2)
            if not hit_rect.contains(pos):
                self._pressed_on_stereo_checkbox = False
                return super().eventFilter(watched, event)

            if event.type() == QEvent.Type.MouseButtonPress:
                self._pressed_on_stereo_checkbox = True
                return True

            if event.type() == QEvent.Type.MouseButtonRelease:
                if not self._pressed_on_stereo_checkbox:
                    return True
                self._pressed_on_stereo_checkbox = False
                if self._layer_toggle_handler:
                    changed = self._layer_toggle_handler(layer_name, not bool(stereo_visible))
                    if changed:
                        viewport.update(opt.rect)
                return True
        except Exception:
            self._pressed_on_stereo_checkbox = False

        return super().eventFilter(watched, event)

    def _left_stereo_checkbox_rect(self, option, index) -> QRect:
        widget = option.widget
        style = widget.style() if widget else None
        y = int(option.rect.center().y() - (self._stereo_checkbox_size // 2))
        if style is None:
            x = int(option.rect.left() + 2)
            return QRect(x, y, self._stereo_checkbox_size, self._stereo_checkbox_size)

        # Force a style option with check-indicator feature so geometry is reliable.
        native_opt = QStyleOptionViewItem(option)
        native_opt.features = native_opt.features | QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        check_state = index.data(Qt.ItemDataRole.CheckStateRole)
        if isinstance(check_state, int):
            native_opt.checkState = Qt.CheckState(check_state)
        elif check_state in (Qt.CheckState.Checked, Qt.CheckState.Unchecked, Qt.CheckState.PartiallyChecked):
            native_opt.checkState = check_state

        check_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, native_opt, widget)
        if check_rect.isValid() and check_rect.width() > 0:
            x = int(check_rect.left() - self._stereo_checkbox_size - self._stereo_checkbox_gap)
        else:
            x = int(option.rect.left() + 2)
        return QRect(x, y, self._stereo_checkbox_size, self._stereo_checkbox_size)

    def _draw_left_stereo_checkbox(self, painter, option, index, stereo_visible: bool, disabled_by_parent: bool = False):
        box_rect = self._left_stereo_checkbox_rect(option, index)
        painter.save()
        # Allow painting into the left tree margin so the stereo checkbox can stay
        # strictly at the left of the native checkbox without overlap.
        painter.setClipping(False)
        widget = option.widget
        style = widget.style() if widget else None
        if style is not None:
            checkbox_opt = QStyleOptionButton()
            checkbox_opt.rect = box_rect
            checkbox_opt.state = option.state
            if disabled_by_parent:
                checkbox_opt.state &= ~QStyle.StateFlag.State_Enabled
            if option.state & QStyle.StateFlag.State_MouseOver:
                checkbox_opt.state |= QStyle.StateFlag.State_MouseOver
            checkbox_opt.state |= QStyle.StateFlag.State_On if stereo_visible else QStyle.StateFlag.State_Off
            style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorCheckBox, checkbox_opt, painter, widget)
        else:
            painter.setRenderHint(painter.RenderHint.Antialiasing, False)
            painter.setPen(QColor(30, 30, 30))
            painter.setBrush(QColor(255, 255, 255, 255))
            painter.drawRect(box_rect)

            if stereo_visible:
                painter.setPen(QColor(0, 70, 0))
                painter.drawLine(box_rect.left() + 3, box_rect.center().y(), box_rect.left() + 6, box_rect.bottom() - 3)
                painter.drawLine(box_rect.left() + 6, box_rect.bottom() - 3, box_rect.right() - 2, box_rect.top() + 3)

        painter.restore()

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
        self._highlight_delegate_installed = False
        self._original_item_delegate = None
        self._swm_highlight_delegate = None
        self._tree_root_visibility_hooked = False
        self._hooked_tree_root = None
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
            layer = self._resolve_single_layer_from_group_name(name)
            if layer is None:
                return None
        else:
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
            layer = self._resolve_single_layer_from_group_name(name)
            if layer is None:
                return False
        else:
            layer = layers[0]

        before_signature = self.signature_fragment()
        self._on_toggle_layer_stereo_visibility(layer, bool(stereo_visible))
        return before_signature != self.signature_fragment()

    def setup_context_menu(self):
        """Connect layer-tree hooks once."""
        layer_tree_view = self.iface.layerTreeView() if self.iface else None
        if not layer_tree_view:
            self._install_tree_root_visibility_hook()
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
            self._swm_highlight_delegate.attach_to_layer_tree(layer_tree_view)
            layer_tree_view.setItemDelegate(self._swm_highlight_delegate)
            self._highlight_delegate_installed = True
            try:
                viewport = layer_tree_view.viewport()
                if viewport is not None:
                    viewport.update()
            except Exception:
                pass

        self._install_tree_root_visibility_hook()

    def cleanup(self):
        """Disconnect hooks and clear local state."""
        layer_tree_view = self.iface.layerTreeView() if self.iface else None
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
                    self._swm_highlight_delegate.detach_from_layer_tree()
                    self._swm_highlight_delegate.deleteLater()
            except Exception:
                pass

        self._highlight_delegate_installed = False
        self._original_item_delegate = None
        self._swm_highlight_delegate = None
        self._remove_tree_root_visibility_hook()
        self._visibility_overrides.clear()

    def _install_tree_root_visibility_hook(self):
        """Hooks project layer-tree visibility changes to mirror group checks into stereo overrides."""
        try:
            project = QgsProject.instance()
            root = project.layerTreeRoot() if project else None
            if root is None or not hasattr(root, "visibilityChanged"):
                self._remove_tree_root_visibility_hook()
                return

            if self._tree_root_visibility_hooked and self._hooked_tree_root is root:
                return

            self._remove_tree_root_visibility_hook()
            root.visibilityChanged.connect(self._on_tree_root_visibility_changed)
            self._tree_root_visibility_hooked = True
            self._hooked_tree_root = root
        except Exception:
            self._tree_root_visibility_hooked = False
            self._hooked_tree_root = None

    def _remove_tree_root_visibility_hook(self):
        try:
            root = self._hooked_tree_root
            if root is not None and hasattr(root, "visibilityChanged"):
                root.visibilityChanged.disconnect(self._on_tree_root_visibility_changed)
        except Exception:
            pass
        self._tree_root_visibility_hooked = False
        self._hooked_tree_root = None

    def _on_tree_root_visibility_changed(self, node):
        """Mirror recursive group check/uncheck actions into stereo child overrides only."""
        try:
            if not isinstance(node, QgsLayerTreeGroup):
                return
            # Defer evaluation one event loop step so QGIS has already applied
            # recursive child check-state updates (if any).
            QTimer.singleShot(0, lambda grp=node: self._apply_recursive_group_action_if_needed(grp))
        except Exception:
            pass

    def _apply_recursive_group_action_if_needed(self, group):
        if not isinstance(group, QgsLayerTreeGroup):
            return

        layer_nodes = list(self._iter_group_layer_nodes(group))
        if not layer_nodes:
            return

        group_state = self._node_checkbox_checked(group)

        # Heuristic: recursive actions set every child checkbox to the same
        # value as the group; plain group visibility toggle preserves child
        # own check states and should not alter stereo overrides.
        all_children_match_group = all(
            self._node_checkbox_checked(layer_node) == group_state
            for layer_node in layer_nodes
        )
        if not all_children_match_group:
            return

        self._apply_group_visibility_to_stereo_children(group)

    def _iter_group_layer_nodes(self, group):
        if not isinstance(group, QgsLayerTreeGroup):
            return

        for child in group.children():
            if isinstance(child, QgsLayerTreeLayer):
                yield child
            elif isinstance(child, QgsLayerTreeGroup):
                yield from self._iter_group_layer_nodes(child)

    @staticmethod
    def _node_checkbox_checked(node) -> bool:
        """Returns the node own checkbox state (independent from parent/group visibility)."""
        try:
            getter = getattr(node, "itemVisibilityChecked", None)
            if callable(getter):
                return bool(getter())
        except Exception:
            pass

        try:
            return bool(node.isVisible())
        except Exception:
            return True

    def _apply_group_visibility_to_stereo_children(self, group):
        """Copies each child own checkbox state into stereo overrides for a group."""
        changed_layer_id = None
        changed_layer_visible = False
        changed = False

        for layer_node in self._iter_group_layer_nodes(group):
            layer = layer_node.layer()
            if not layer or not hasattr(layer, "id"):
                continue

            layer_id = str(layer.id())
            stereo_visible = self._node_checkbox_checked(layer_node)
            previous = self._visibility_overrides.get(layer_id)
            if previous is not None and bool(previous) == stereo_visible:
                continue

            self._visibility_overrides[layer_id] = stereo_visible
            if changed_layer_id is None:
                changed_layer_id = layer_id
                changed_layer_visible = stereo_visible
            changed = True

        if not changed:
            return

        self._save_to_project()

        layer_tree_view = self.iface.layerTreeView() if self.iface else None
        if layer_tree_view and hasattr(layer_tree_view, "viewport"):
            try:
                viewport = layer_tree_view.viewport()
                if viewport is not None:
                    viewport.update()
            except Exception:
                pass

        if changed_layer_id and self._on_visibility_changed:
            self._on_visibility_changed(changed_layer_id, changed_layer_visible)

    def clear_overrides(self):
        """Removes all persisted stereo visibility states and persists the empty state."""
        if not self._visibility_overrides:
            return False

        self._visibility_overrides.clear()
        self._save_to_project()
        return True

    def has_overrides(self) -> bool:
        return bool(self._visibility_overrides)

    def is_layer_visible_in_stereo(self, layer, main_visible: bool) -> bool:
        """Return persisted stereo visibility for a layer, independent from main canvas toggles."""
        if not layer or not hasattr(layer, "id"):
            return bool(main_visible)

        layer_id = str(layer.id())
        stored = self._visibility_overrides.get(layer_id)
        if stored is not None:
            return bool(stored)

        # First time a layer is seen, seed stereo state from main visibility.
        # From this point on, stereo remains independent.
        self._visibility_overrides[layer_id] = bool(main_visible)
        return bool(main_visible)

    def signature_fragment(self) -> Tuple[Tuple[str, bool], ...]:
        """Stable, hashable fragment to include in layer-sync dedupe signature."""
        items = sorted((str(layer_id), bool(visible)) for layer_id, visible in self._visibility_overrides.items())
        return tuple(items)

    def _on_toggle_layer_stereo_visibility(self, layer, stereo_visible: bool):
        if not layer or not hasattr(layer, "id"):
            return

        layer_id = str(layer.id())
        previous = self._visibility_overrides.get(layer_id)
        changed = previous is None or bool(previous) != bool(stereo_visible)
        if changed:
            self._visibility_overrides[layer_id] = bool(stereo_visible)

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
        if root is None:
            return True

        node = root.findLayer(layer.id())
        if node is None:
            return True

        try:
            return bool(node.isVisible())
        except Exception:
            return True

    def _resolve_single_layer_from_group_name(self, group_name: str):
        """If a tree group has a single layer child, return that layer; otherwise None."""
        name = str(group_name or "").strip()
        if not name:
            return None

        project = QgsProject.instance()
        if not project:
            return None

        root = project.layerTreeRoot()
        if root is None:
            return None

        group = root.findGroup(name)
        if group is None:
            return None

        layer = self._first_layer_in_group(group)
        return layer

    def _first_layer_in_group(self, group):
        """Recursively returns the first layer child found under a group if it is unique."""
        if not isinstance(group, QgsLayerTreeGroup):
            return None

        layer_children = []
        for child in group.children():
            if isinstance(child, QgsLayerTreeLayer):
                layer_children.append(child.layer())
            elif isinstance(child, QgsLayerTreeGroup):
                nested_layer = self._first_layer_in_group(child)
                if nested_layer is not None:
                    layer_children.append(nested_layer)

            if len(layer_children) > 1:
                return None

        if len(layer_children) == 1:
            return layer_children[0]
        return None


class StereoCanvasToolbar:
    """Compact control strip hosted in the main QGIS window."""

    _CONTAINER_MODE_AUTO = "auto"
    _CONTAINER_MODE_FLOATING = "floating"
    _CONTAINER_MODE_DOCK = "dock"

    def __init__(self, window):
        self.window = window
        self.toolbar = None
        self._installed = False
        self._icon_label = None
        self._activation_scale_spin = None
        self._rotation_threshold_spin = None
        self._current_rotation_label = None
        self._z_status_label = None
        self._params_button = None
        self._options_button = None
        self._action_show_z_text = None
        self._action_z_project_plain = None
        self._action_move_zlabel_in_3D = None
        self._action_prevent_cursor_from_stereo_display = None
        self._action_restore_defaults = None
        self._build()

    @staticmethod
    def _settings_key_container_mode() -> str:
        return "SigridSWM/controls_container_mode"

    def _qgis_version_int(self) -> int:
        try:
            return int(getattr(Qgis, "QGIS_VERSION_INT", 0) or 0)
        except Exception:
            return 0

    def _resolve_container_mode(self) -> str:
        return "qgis-main-toolbar"

    def _log_container_mode(self):
        try:
            QgsMessageLog.logMessage(
                (
                    f"Controls container mode=qgis-main-toolbar; "
                    f"QGIS_INT={self._qgis_version_int()}; Qt={QT_VERSION_STR}; "
                    f"setting={QSettings().value(self._settings_key_container_mode(), self._CONTAINER_MODE_AUTO, type=str)}"
                ),
                "SWM-3D",
                Qgis.Info,
            )
        except Exception:
            pass

    def _build(self):
        main_window = self.window.iface.mainWindow() if self.window and self.window.iface else None
        if not main_window:
            return

        toolbar = QToolBar("SWM-3D Controls", main_window)
        toolbar.setObjectName("SWM3DControlsMainToolbar")
        toolbar.setMovable(True)
        toolbar.setFloatable(False)
        toolbar.setIconSize(QSize(24, 24))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.addWidget(self._build_control_content(toolbar))
        self.toolbar = toolbar

        self._log_container_mode()

    def _build_control_content(self, parent):
        content_widget = QWidget(parent)
        content_layout = QHBoxLayout(content_widget)
        content_layout.setContentsMargins(6, 4, 6, 4)
        content_layout.setSpacing(6)

        icon_label = QLabel()
        icon = QIcon(self._icon_path())
        if not icon.isNull():
            icon_label.setPixmap(icon.pixmap(24, 24))
        icon_label.setToolTip("SWM-3D stereo controls")
        content_layout.addWidget(icon_label)
        self._add_separator(content_layout, content_widget)

        current_rotation_label = QLabel("Current rot: 0.0°")
        current_rotation_label.setMinimumWidth(120)
        content_layout.addWidget(current_rotation_label)

        self._add_separator(content_layout, content_widget)
        z_status_label = QLabel("Zbase=---- Zcurs=----")
        z_status_label.setMinimumWidth(170)
        content_layout.addWidget(z_status_label)

        spacer = QWidget(content_widget)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        content_layout.addWidget(spacer)

        params_menu = QMenu(content_widget)

        activation_param_widget = QWidget(content_widget)
        activation_param_layout = QHBoxLayout(activation_param_widget)
        activation_param_layout.setContentsMargins(8, 4, 8, 4)
        activation_param_layout.setSpacing(8)
        activation_lbl = QLabel("Stereo active scale")
        activation_lbl.setMinimumWidth(160)
        activation_param_layout.addWidget(activation_lbl)
        activation_edit = QLineEdit()
        activation_edit.setValidator(QDoubleValidator(1.0, 1000000000.0, 0, activation_edit))
        activation_edit.setFixedWidth(120)
        activation_edit.editingFinished.connect(
            lambda edit=activation_edit: self._on_activation_scale_changed(edit)
        )
        activation_param_layout.addWidget(activation_edit)
        activation_param_action = QWidgetAction(params_menu)
        activation_param_action.setDefaultWidget(activation_param_widget)
        params_menu.addAction(activation_param_action)

        rotation_param_widget = QWidget(content_widget)
        rotation_param_layout = QHBoxLayout(rotation_param_widget)
        rotation_param_layout.setContentsMargins(8, 4, 8, 4)
        rotation_param_layout.setSpacing(8)
        rotation_lbl = QLabel("Rotate canvas level (deg)")
        rotation_lbl.setMinimumWidth(160)
        rotation_param_layout.addWidget(rotation_lbl)
        rotation_edit = QLineEdit()
        rotation_edit.setValidator(QDoubleValidator(0.0, 180.0, 0, rotation_edit))
        rotation_edit.setFixedWidth(100)
        rotation_edit.editingFinished.connect(
            lambda edit=rotation_edit: self._on_rotation_threshold_changed(edit)
        )
        rotation_param_layout.addWidget(rotation_edit)
        rotation_param_action = QWidgetAction(params_menu)
        rotation_param_action.setDefaultWidget(rotation_param_widget)
        params_menu.addAction(rotation_param_action)

        params_menu.addSeparator()
        action_restore_defaults_params = QAction("Restore parameter defaults", params_menu)
        action_restore_defaults_params.triggered.connect(self._on_restore_default_params)
        params_menu.addAction(action_restore_defaults_params)

        params_button = QToolButton(content_widget)
        params_button.setText("Configuration parameters")
        params_button.setMenu(params_menu)
        params_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        params_button.setToolTip("Configurable parameters")
        content_layout.addWidget(params_button)

        options_menu = QMenu(content_widget)
        action_show_z_text = QAction("Show Z label", options_menu)
        action_show_z_text.setCheckable(True)
        action_show_z_text.setChecked(True)
        action_show_z_text.toggled.connect(self._on_show_z_text_toggled)
        options_menu.addAction(action_show_z_text)

        action_z_project_plain = QAction("Z cursor to projection plane", options_menu)
        action_z_project_plain.setCheckable(True)
        action_z_project_plain.setChecked(True)
        action_z_project_plain.toggled.connect(self._on_z_project_plain_toggled)
        options_menu.addAction(action_z_project_plain)

        action_move_zlabel_in_3D = QAction("Move Z label in 3D", options_menu)
        action_move_zlabel_in_3D.setCheckable(True)
        action_move_zlabel_in_3D.setChecked(True)
        action_move_zlabel_in_3D.toggled.connect(self._on_move_zlabel_in_3D_toggled)
        options_menu.addAction(action_move_zlabel_in_3D)

        action_prevent_cursor_from_stereo_display = QAction("Prevent cursor from stereo display", options_menu)
        action_prevent_cursor_from_stereo_display.setCheckable(True)
        action_prevent_cursor_from_stereo_display.setChecked(True)
        action_prevent_cursor_from_stereo_display.toggled.connect(self._on_prevent_cursor_from_stereo_display_toggled)
        options_menu.addAction(action_prevent_cursor_from_stereo_display)

        options_menu.addSeparator()
        action_restore_defaults = QAction("Restore default options", options_menu)
        action_restore_defaults.triggered.connect(self._on_restore_default_options)
        options_menu.addAction(action_restore_defaults)

        options_button = QToolButton(content_widget)
        options_button.setText("Configuration options")
        options_button.setMenu(options_menu)
        options_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        options_button.setToolTip("Display options")
        content_layout.addWidget(options_button)
        self._add_separator(content_layout, content_widget)

        close_button = QToolButton(content_widget)
        close_icon = content_widget.style().standardIcon(content_widget.style().StandardPixmap.SP_DockWidgetCloseButton)
        close_button.setIcon(close_icon)
        close_button.setText("Close")
        close_button.setToolTip("Close the SWM-3D plugin window")
        close_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        close_button.clicked.connect(self._close_plugin)
        content_layout.addWidget(close_button)

        self._icon_label = icon_label
        self._activation_scale_spin = activation_edit
        self._rotation_threshold_spin = rotation_edit
        self._current_rotation_label = current_rotation_label
        self._z_status_label = z_status_label
        self._params_button = params_button
        self._options_button = options_button
        self._action_show_z_text = action_show_z_text
        self._action_z_project_plain = action_z_project_plain
        self._action_move_zlabel_in_3D = action_move_zlabel_in_3D
        self._action_prevent_cursor_from_stereo_display = action_prevent_cursor_from_stereo_display
        self._action_restore_defaults = action_restore_defaults

        return content_widget

    @staticmethod
    def _settings_key_pos_x() -> str:
        return "SigridSWM/controls_toolbar_pos_x"

    @staticmethod
    def _settings_key_pos_y() -> str:
        return "SigridSWM/controls_toolbar_pos_y"

    def _save_toolbar_position(self):
        if not self.toolbar:
            return
        try:
            pos = self.toolbar.pos()
            settings = QSettings()
            settings.setValue(self._settings_key_pos_x(), int(pos.x()))
            settings.setValue(self._settings_key_pos_y(), int(pos.y()))
        except Exception:
            pass

    def _load_toolbar_position(self):
        try:
            settings = QSettings()
            px = settings.value(self._settings_key_pos_x(), None)
            py = settings.value(self._settings_key_pos_y(), None)
            if px is None or py is None:
                return None
            return int(px), int(py)
        except Exception:
            return None

    def install(self):
        if self._installed:
            if self.toolbar is not None:
                self.toolbar.setVisible(True)
            return

        if not self.toolbar:
            self._build()

        if not self.toolbar or not self.window or not self.window.iface:
            return

        main_window = self.window.iface.mainWindow()
        if not main_window:
            return

        was_animated = bool(main_window.isAnimated()) if hasattr(main_window, 'isAnimated') else False
        if hasattr(main_window, 'setAnimated'):
            main_window.setAnimated(False)
        try:
            if main_window.toolBarArea(self.toolbar) == Qt.ToolBarArea.NoToolBarArea:
                main_window.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)
            self.toolbar.setVisible(True)
        finally:
            if hasattr(main_window, 'setAnimated'):
                main_window.setAnimated(was_animated)

        self._installed = True

    def cleanup(self):
        if not self.toolbar:
            self._icon_label = None
            self._activation_scale_spin = None
            self._rotation_threshold_spin = None
            self._current_rotation_label = None
            self._z_status_label = None
            self._params_button = None
            self._options_button = None
            self._action_show_z_text = None
            self._action_z_project_plain = None
            self._action_move_zlabel_in_3D = None
            self._action_prevent_cursor_from_stereo_display = None
            self._action_restore_defaults = None
            return

        try:
            if self.window and self.window.iface:
                main_window = self.window.iface.mainWindow()
                if main_window:
                    was_animated = bool(main_window.isAnimated()) if hasattr(main_window, 'isAnimated') else False
                    if hasattr(main_window, 'setAnimated'):
                        main_window.setAnimated(False)
                    try:
                        main_window.removeToolBar(self.toolbar)
                    finally:
                        if hasattr(main_window, 'setAnimated'):
                            main_window.setAnimated(was_animated)
            self.toolbar.deleteLater()
        except Exception:
            pass
        self.toolbar = None
        self._installed = False
        self._icon_label = None
        self._activation_scale_spin = None
        self._rotation_threshold_spin = None
        self._current_rotation_label = None
        self._z_status_label = None
        self._params_button = None
        self._options_button = None
        self._action_show_z_text = None
        self._action_z_project_plain = None
        self._action_move_zlabel_in_3D = None
        self._action_prevent_cursor_from_stereo_display = None
        self._action_restore_defaults = None

    @staticmethod
    def _add_separator(layout, parent_widget):
        separator = QFrame(parent_widget)
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)

    def refresh(self):
        if not self.window or self.toolbar is None:
            return

        if self._activation_scale_spin is not None:
            self._activation_scale_spin.setText(str(int(self.window._stereo_activation_scale_threshold)))

        if self._rotation_threshold_spin is not None:
            self._rotation_threshold_spin.setText(str(int(self.window._flight_rotation_threshold_deg)))

        if self._current_rotation_label is not None:
            self._current_rotation_label.setText(
                f"Current rot: {float(self.window._flight_rotation_current_deg):.1f}°"
            )

        if self._action_show_z_text is not None:
            blocker = QSignalBlocker(self._action_show_z_text)
            self._action_show_z_text.setChecked(bool(self.window.canvas_left and getattr(self.window.canvas_left, 'show_z_text_overlay', True)))
            del blocker

        if self._action_z_project_plain is not None:
            blocker = QSignalBlocker(self._action_z_project_plain)
            self._action_z_project_plain.setChecked(bool(self.window.z_project_plain))
            del blocker

        if self._action_move_zlabel_in_3D is not None:
            blocker = QSignalBlocker(self._action_move_zlabel_in_3D)
            self._action_move_zlabel_in_3D.setChecked(bool(self.window.move_zlabel_in_3D))
            del blocker

        if self._action_prevent_cursor_from_stereo_display is not None:
            blocker = QSignalBlocker(self._action_prevent_cursor_from_stereo_display)
            self._action_prevent_cursor_from_stereo_display.setChecked(bool(self.window.prevent_cursor_from_stereo_display))
            del blocker

        self.set_z_status(float(self.window._z_proj_plane), float(self.window._z_cursor))

    def set_z_status(self, z_base: float, z_cursor: float):
        if self._z_status_label is not None:
            self._z_status_label.setText(f"Zbase={float(z_base):.1f} Zcurs={float(z_cursor):.1f}")

    def _on_activation_scale_changed(self, edit):
        try:
            value = float(edit.text())
        except ValueError:
            self.refresh()
            return
        if self.window:
            self.window._set_stereo_activation_scale_threshold(value)
        self._clear_parameter_focus(edit)

    def _on_rotation_threshold_changed(self, edit):
        try:
            value = float(edit.text())
        except ValueError:
            self.refresh()
            return
        if self.window:
            self.window._set_flight_rotation_threshold_deg(value)
        self._clear_parameter_focus(edit)

    def _clear_parameter_focus(self, spin):
        """Return keyboard focus to the toolbar after a parameter edit."""
        if spin is not None and hasattr(spin, 'clearFocus'):
            spin.clearFocus()

    def _on_show_z_text_toggled(self, checked: bool):
        if self.window:
            self.window.set_show_z_text(checked)

    def _on_z_project_plain_toggled(self, checked: bool):
        if self.window:
            self.window.set_z_project_plain(checked)

    def _on_move_zlabel_in_3D_toggled(self, checked: bool):
        if self.window:
            self.window.set_move_zlabel_in_3D(checked)

    def _on_prevent_cursor_from_stereo_display_toggled(self, checked: bool):
        if self.window:
            self.window.set_prevent_cursor_from_stereo_display(checked)

    def _on_restore_default_options(self):
        if self.window:
            self.window.reset_ui_option_states()

    def _on_restore_default_params(self):
        if self.window:
            self.window.reset_parameter_defaults()

    def _close_plugin(self):
        if self.window:
            self.window.close()

    @staticmethod
    def _icon_path() -> str:
        return os.path.join(os.path.dirname(__file__), "icons", "anaglyph_glasses.svg")
