"""
stereo_canvas_options.py

Handles per-layer visibility overrides for stereo canvases.

Default behavior follows the main canvas visibility. Users can override it
from the layer-tree context menu action "Visible in stereo canvases".
"""

import json
from typing import Callable, Dict, Optional, Tuple

from qgis.core import QgsProject
from qgis.PyQt.QtGui import QAction


class StereoCanvasOptions:
    """Keeps stereo-visibility overrides and injects context-menu action."""

    _PROJECT_SCOPE = "SWM-3D"
    _PROJECT_KEY_OVERRIDES = "stereo_visibility_overrides"

    def __init__(self, iface, on_visibility_changed: Optional[Callable[[str, bool], None]] = None):
        self.iface = iface
        self._on_visibility_changed = on_visibility_changed
        self._visibility_overrides: Dict[str, bool] = {}
        self._context_menu_connected = False
        self._load_from_project()

    def setup_context_menu(self):
        """Connect layer-tree context-menu hook once."""
        if self._context_menu_connected:
            return

        layer_tree_view = self.iface.layerTreeView() if self.iface else None
        if not layer_tree_view or not hasattr(layer_tree_view, "contextMenuAboutToShow"):
            return

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

        self._context_menu_connected = False
        self._visibility_overrides.clear()

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
