"""
window.py

Main window and controller for the Sigrid SWM plugin.

This module defines QSgdSwmWindow, which acts as the central controller
of the plugin. It is responsible for:

- Creating and managing the left and right SWM canvases
- Selecting the stereo mode and screen layout
- Handling WMS network replies and reading transformation headers
- Managing the global Z tool and projection plane parameters
- Coordinating synchronization between both canvases

This module does not perform rendering or geometric calculations.
It orchestrates the plugin workflow and delegates rendering and
transformations to the canvas and expression layers.
"""

from qgis.core import QgsMessageLog, Qgis  # for debug messages.
from qgis.core import QgsNetworkAccessManager
from qgis.core import QgsGeometry
from qgis.core import QgsVectorLayer
from qgis.core import QgsCoordinateTransform, QgsProject
from qgis.PyQt.QtWidgets import QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QStackedLayout
from qgis.PyQt.QtWidgets import QInputDialog, QApplication, QLabel
from qgis.PyQt.QtGui import QTransform, QGuiApplication, QCursor, QPixmap
from qgis.PyQt.QtCore import Qt, QEvent, QTimer
import os
import re
import math
import time
from typing import Dict, Any, Set, Tuple, Optional, List, cast

# SWM libraries
from .canvas import QgsSgdSwmCanvas
from .stereo_canvas_options import StereoCanvasOptions, StereoCanvasToolbar
from .utils import is_z_layer, is_sgd_swm_layer
try:
    from . import debug as _debug_module
except ImportError:
    _debug_module = None


# Class Sigrid Swm Window
class QSgdSwmWindow(QMainWindow):
    def __init__(self, iface):
        super().__init__(None)

        # Initialize
        self.qgis_main_canvas = iface.mapCanvas()
        self.stereo_id = 0
        self.swm_screen_geometry = None
        self.canvas_left = None
        self.canvas_right = None
        self.init_error = None
        self.iface = iface
        # Used to maximize only after a valid SWM reply is received
        self._has_received_swm_reply = False
        # Cached fixed CRS of SWM service (from capabilities/layer metadata)
        self._swm_service_crs = None
        # Stereo projection is only active when the main canvas is zoomed in to this scale or more.
        self._stereo_activation_scale_threshold = 100000.0
        # Auto strip-rotation enabled: compensates non E-W flight headings.
        self._auto_flight_rotation_enabled = True
        self._flight_rotation_threshold_deg = 10.0
        self._flight_rotation_current_deg = 0.0
        self._photo_center_left = None   # (x0, y0) world coords of left photo
        self._photo_center_right = None  # (x0, y0) world coords of right photo
        self._last_photo_bbox_left: Optional[str] = None
        self._last_photo_bbox_right: Optional[str] = None
        self._last_rotation_bbox_applied: Optional[str] = None
        self._stereo_refresh_revision = 0
        self._last_applied_stereo_refresh_signature: Optional[Tuple[Any, ...]] = None
        self._last_main_repaint_signature: Optional[Tuple[Any, ...]] = None
        self._last_layers_sync_signature: Optional[Tuple[Any, ...]] = None
        self._last_main_scale_for_layer_sync: Optional[float] = None
        self._trace_wms_debug = False
        self._trace_seq = 0
        self._stereo_canvas_options = None
        self._stereo_controls_toolbar = None
        self._main_north_indicator_label = None
        self._main_north_indicator_base_pixmap = QPixmap(
            os.path.join(os.path.dirname(__file__), "icons", "north_arrow.svg")
        )
        self._main_canvas_rotation_at_start = 0.0
        if self.qgis_main_canvas and hasattr(self.qgis_main_canvas, 'rotation'):
            self._main_canvas_rotation_at_start = float(self.qgis_main_canvas.rotation())

        # Set the screen for the Swm Window
        self.init_error = self.set_screen()
        if self.init_error:
            return

        # Mount plugin window
        self.init_error = self.configure_canvases()
        if self.init_error:
            self.iface.messageBar().pushCritical("SWM-3D", self.init_error)
            return

        # Shared global Z; always assign through @z_cursor.setter to update everything
        self._z_proj_plane = 0.0
        self.z_cursor = 0.0
        # Global filter can be removed on close and must be reinstalled on reopen.
        self._global_event_filter_installed = False
        self._install_global_event_filter()
        self._setup_main_canvas_north_indicator()

        # Capturar eventos del canvas principal qgis
        self.iface.mapCanvas().xyCoordinates.connect(self._sync_canvases_cursor)      # mouse
        self._layer_sync_timer = QTimer(self)
        self._layer_sync_timer.setSingleShot(True)
        self._layer_sync_timer.setInterval(50)
        self._layer_sync_timer.timeout.connect(self._sync_canvases_layers)
        self._canvas_refresh_timer = QTimer(self)
        self._canvas_refresh_timer.setSingleShot(True)
        self._canvas_refresh_timer.setInterval(0)
        self._canvas_refresh_timer.timeout.connect(self._run_canvas_refresh)
        self.iface.mapCanvas().layersChanged.connect(self._schedule_layers_sync)      # new layers / ordering
        project = QgsProject.instance()
        if project:
            root = project.layerTreeRoot()
            if root and hasattr(root, 'visibilityChanged'):
                root.visibilityChanged.connect(self._schedule_layers_sync)  # visibility toggles
        try:
            layer_tree_view = self.iface.layerTreeView()
            if layer_tree_view and hasattr(layer_tree_view, 'layerTreeModel'):
                model = layer_tree_view.layerTreeModel()
                self._layer_tree_model = model
        except Exception:
            self._layer_tree_model = None
        self.iface.mapCanvas().extentsChanged.connect(self._sync_canvases_repaint)    # zoom and pan
        if hasattr(self.iface.mapCanvas(), 'scaleChanged'):
            self.iface.mapCanvas().scaleChanged.connect(self._on_main_canvas_scale_changed)
        if hasattr(self.iface.mapCanvas(), 'destinationCrsChanged'):
            self.iface.mapCanvas().destinationCrsChanged.connect(self._on_main_canvas_crs_changed)

        # Persist cursor Z into edited features on Z-enabled layers.
        self._layer_edit_hooks: Dict[str, Dict[str, Any]] = {}
        self._geometry_update_guard: Set[Tuple[str, int]] = set()
        self._feature_vertex_z_history: Dict[Tuple[str, int], List[float]] = {}
        self._pending_digitize_z_clicks: Dict[str, List[float]] = {}
        self._last_click_capture: Dict[str, Tuple[float, float, str]] = {}
        self._layers_rolling_back: Set[str] = set()
        self._layer_style_hooks: Dict[str, Dict[str, Any]] = {}
        self._pending_style_sync = False
        self._style_sync_timer = QTimer(self)
        self._style_sync_timer.setSingleShot(True)
        self._style_sync_timer.setInterval(100)
        self._style_sync_timer.timeout.connect(self._run_pending_style_sync)
        self._stereo_canvas_options = StereoCanvasOptions(self.iface, self._on_stereo_layer_visibility_changed)
        self._stereo_canvas_options.setup_context_menu()
        self._stereo_controls_toolbar = StereoCanvasToolbar(self)
        self._stereo_controls_toolbar.install()
        self._update_digitizing_layer_hooks()
        self._update_style_layer_hooks()

        # Network Manager WMS
        # https://chat.deepseek.com/a/chat/s/5dc872fa-208d-458c-836b-9199dcc3a37c
        self.network_manager = QgsNetworkAccessManager.instance()      
        if self.network_manager:
            self.network_manager.finished.connect(self.network_reply_handle)

    def showEvent(self, event):
        """
        Virtual method inherited from QWidget / QMainWindow.
        """
        self._install_global_event_filter()
        super().showEvent(event)
        if self._stereo_canvas_options:
            self._stereo_canvas_options.setup_context_menu()
        if self._stereo_controls_toolbar:
            self._stereo_controls_toolbar.install()
        self._setup_main_canvas_north_indicator()
        # Initial sync after the window is shown
        self._sync_canvases_destination_crs()
        if self.canvas_left:
            extent_left = self._reproject_extent_to_stereo_crs(self.qgis_main_canvas.extent(), self.canvas_left)
            self.canvas_left.setExtent(extent_left)
        if self.canvas_right:
            extent_right = self._reproject_extent_to_stereo_crs(self.qgis_main_canvas.extent(), self.canvas_right)
            self.canvas_right.setExtent(extent_right)
        # Route initial layer setup through the deduplicated sync path so later
        # startup layer-tree signals do not repeat the same SWM requests.
        self._sync_canvases_layers()
        self._update_control_toolbar_state()
        self._update_main_canvas_north_indicator()

    def enterEvent(self, event):
        self._keep_cursor_in_main_qgis_window()
        super().enterEvent(event)

    def _install_global_event_filter(self):
        """Installs the QApplication-level event filter exactly once."""
        if self._global_event_filter_installed:
            return
        app = QApplication.instance()
        if not app:
            return
        app.installEventFilter(self)
        self._global_event_filter_installed = True

    def _remove_global_event_filter(self):
        """Removes the QApplication-level event filter when currently installed."""
        if not self._global_event_filter_installed:
            return
        app = QApplication.instance()
        if app:
            try:
                app.removeEventFilter(self)
            except (RuntimeError, TypeError):
                pass
        self._global_event_filter_installed = False

    def closeEvent(self, event):
        try:
            # Clean up event filter
            self._remove_global_event_filter()

            if self.network_manager:
                try:
                    self.network_manager.finished.disconnect(self.network_reply_handle)
                except (RuntimeError, TypeError):
                    pass

            if self._stereo_canvas_options:
                self._stereo_canvas_options.cleanup()

            if self._stereo_controls_toolbar:
                self._stereo_controls_toolbar.cleanup()

            self._cleanup_main_canvas_north_indicator()

            # Restore original main-canvas rotation from before stereo session.
            self._restore_main_canvas_rotation()

            # Clean up digitizing interceptors
            self._disconnect_all_digitizing_hooks()
            self._disconnect_all_style_hooks()
            if self._style_sync_timer and self._style_sync_timer.isActive():
                self._style_sync_timer.stop()
            if self._layer_sync_timer and self._layer_sync_timer.isActive():
                self._layer_sync_timer.stop()

            project = QgsProject.instance()
            if project:
                root = project.layerTreeRoot()
                if root and hasattr(root, 'visibilityChanged'):
                    try:
                        root.visibilityChanged.disconnect(self._schedule_layers_sync)
                    except (RuntimeError, TypeError):
                        pass

            if hasattr(self.iface.mapCanvas(), 'scaleChanged'):
                try:
                    self.iface.mapCanvas().scaleChanged.disconnect(self._on_main_canvas_scale_changed)
                except (RuntimeError, TypeError):
                    pass
            
            # Clean up synchronization in secondary canvases
            if self.canvas_left:
                self.canvas_left.cleanup_canvas_items_sync()
            if self.canvas_right:
                self.canvas_right.cleanup_canvas_items_sync()
            
        except Exception as e:
            QgsMessageLog.logMessage(f"Cleanup error: {str(e)}", "SWM-3D")
        
        super().closeEvent(event)

    @staticmethod
    def _normalize_signed_angle_deg(angle_deg: float) -> float:
        """Normalize angle to [-180, 180)."""
        return ((float(angle_deg) + 180.0) % 360.0) - 180.0

    def _signed_delta_to_nearest_horizontal(self, angle_deg: float) -> float:
        """
        Returns signed angular offset to nearest horizontal reference (0 or +/-180).
        """
        a = self._normalize_signed_angle_deg(angle_deg)
        candidates = (0.0, 180.0, -180.0)
        best_delta = None
        for target in candidates:
            delta = self._normalize_signed_angle_deg(a - target)
            if best_delta is None or abs(delta) < abs(best_delta):
                best_delta = delta
        return float(best_delta) if best_delta is not None else 0.0

    def _apply_rotation_to_all_canvases(self, rotation_deg: float, apply_main_canvas: bool = False):
        """
        Apply map rotation to stereo canvases and optionally to main canvas.
        Stereo freeze/unfreeze is handled by the caller when batching updates.
        """
        try:
            if apply_main_canvas and self.qgis_main_canvas and hasattr(self.qgis_main_canvas, 'setRotation'):
                self.qgis_main_canvas.setRotation(float(rotation_deg))
            if self.canvas_left and hasattr(self.canvas_left, 'setRotation'):
                self.canvas_left.setRotation(float(rotation_deg))
            if self.canvas_right and hasattr(self.canvas_right, 'setRotation'):
                self.canvas_right.setRotation(float(rotation_deg))
        except Exception as e:
            QgsMessageLog.logMessage(f"ROT: Error applying canvas rotation: {str(e)}", "SWM-3D", Qgis.Warning)
        self._update_main_canvas_north_indicator()
        self._update_control_toolbar_state()

    def _restore_main_canvas_rotation(self):
        """Restore the main QGIS canvas rotation that existed before opening SWM window."""
        try:
            if self.qgis_main_canvas and hasattr(self.qgis_main_canvas, 'setRotation'):
                self.qgis_main_canvas.setRotation(float(self._main_canvas_rotation_at_start))
        except Exception:
            pass
        self._update_main_canvas_north_indicator()

    def _setup_main_canvas_north_indicator(self):
        """Creates the north-indicator overlay on the main QGIS canvas viewport."""
        if self._main_north_indicator_label is not None:
            return
        if not self.qgis_main_canvas or self._main_north_indicator_base_pixmap.isNull():
            return

        try:
            viewport = self.qgis_main_canvas.viewport() if hasattr(self.qgis_main_canvas, 'viewport') else None
            if viewport is None:
                return

            label = QLabel(viewport)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            label.setStyleSheet("background: transparent;")
            label.hide()
            self._main_north_indicator_label = label
            self._update_main_canvas_north_indicator()
        except Exception:
            self._main_north_indicator_label = None

    def _cleanup_main_canvas_north_indicator(self):
        """Removes the north-indicator overlay from the main canvas viewport."""
        label = self._main_north_indicator_label
        if label is not None:
            try:
                label.hide()
                label.deleteLater()
            except Exception:
                pass
        self._main_north_indicator_label = None

    def _update_main_canvas_north_indicator(self):
        """Refreshes visibility, orientation and position of the main north indicator."""
        label = self._main_north_indicator_label
        if label is None or self._main_north_indicator_base_pixmap.isNull() or not self.qgis_main_canvas:
            return

        try:
            rotation = float(self.qgis_main_canvas.rotation()) if hasattr(self.qgis_main_canvas, 'rotation') else 0.0
            if abs(rotation) < 0.05:
                label.hide()
                return

            viewport = self.qgis_main_canvas.viewport() if hasattr(self.qgis_main_canvas, 'viewport') else None
            if viewport is None:
                label.hide()
                return

            pix = self._main_north_indicator_base_pixmap.transformed(
                QTransform().rotate(-rotation),
                Qt.TransformationMode.SmoothTransformation,
            )
            pix = pix.scaled(
                44,
                44,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            label.setPixmap(pix)

            margin = 12
            x = max(0, viewport.width() - label.width() - margin)
            y = margin
            label.move(x, y)
            label.show()
            label.raise_()
        except Exception:
            pass

    def _is_stereo_projection_active(self) -> bool:
        """Returns True when the main canvas scale is within the stereo activation range."""
        try:
            if not self.qgis_main_canvas:
                return True

            scale_getter = getattr(self.qgis_main_canvas, 'scale', None)
            if callable(scale_getter):
                main_scale = float(cast(Any, scale_getter()))
            else:
                map_settings = self.qgis_main_canvas.mapSettings() if hasattr(self.qgis_main_canvas, 'mapSettings') else None
                if not map_settings or not hasattr(map_settings, 'scale'):
                    return True
                main_scale = float(map_settings.scale())

            return main_scale <= float(self._stereo_activation_scale_threshold)
        except Exception:
            return True

    def _update_auto_flight_rotation(self, apply_main_canvas: bool = True) -> bool:
        """
        Computes and applies automatic rotation compensation for tilted flight strips.

        Calculates the angle of the baseline between left and right photo centers
        (x0, y0 from perspective transform) and compensates so the baseline becomes
        horizontal (nearest 0 or 180 degrees).

        Returns True when a new rotation value was applied.
        """
        if not self._auto_flight_rotation_enabled:
            if abs(self._flight_rotation_current_deg) < 0.05:
                self._update_control_toolbar_state()
                return False
            self._flight_rotation_current_deg = 0.0
            self._apply_rotation_to_all_canvases(0.0, apply_main_canvas=apply_main_canvas)
            return True

        if self._photo_center_left is None or self._photo_center_right is None:
            self._update_control_toolbar_state()
            return False

        # Keep flight-strip auto-rotation disabled outside stereo-active scales.
        if not self._is_stereo_projection_active():
            target_rotation = 0.0
            if abs(target_rotation - self._flight_rotation_current_deg) < 0.05:
                self._update_control_toolbar_state()
                return False
            self._flight_rotation_current_deg = target_rotation
            self._apply_rotation_to_all_canvases(target_rotation, apply_main_canvas=apply_main_canvas)
            return True

        x_l, y_l = self._photo_center_left
        x_r, y_r = self._photo_center_right
        dx = x_r - x_l
        dy = y_r - y_l
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return False

        # Angle of baseline left->right photo center against horizontal
        strip_angle = self._normalize_signed_angle_deg(math.degrees(math.atan2(dy, dx)))
        self._trace(
            f"ROT: photo_center_L=({x_l:.1f},{y_l:.1f}) R=({x_r:.1f},{y_r:.1f}) "
            f"strip_angle={strip_angle:.2f}°"
        )

        delta_to_horizontal = self._signed_delta_to_nearest_horizontal(strip_angle)
        if abs(delta_to_horizontal) > float(self._flight_rotation_threshold_deg):
            target_rotation = delta_to_horizontal
        else:
            target_rotation = 0.0

        target_rotation = self._normalize_signed_angle_deg(target_rotation)
        if abs(target_rotation - self._flight_rotation_current_deg) < 0.05:
            self._update_control_toolbar_state()
            return False

        self._flight_rotation_current_deg = target_rotation
        self._apply_rotation_to_all_canvases(target_rotation, apply_main_canvas=apply_main_canvas)
        return True

    def _keep_cursor_in_main_qgis_window(self):
        """Prevents the cursor from staying over the stereo window (cross-platform)."""
        if _debug_module is not None and _debug_module.DEBUG:
        # if True:
            return

        try:
            main_rect = self.iface.mainWindow().frameGeometry()
            p = QCursor.pos()
            x = min(max(p.x(), main_rect.left() + 1), main_rect.right() - 1)
            y = min(max(p.y(), main_rect.top() + 1), main_rect.bottom() - 1)
            QCursor.setPos(x, y)
        except Exception:
            pass

    def _update_control_toolbar_state(self):
        if self._stereo_controls_toolbar:
            self._stereo_controls_toolbar.refresh()

    def _set_stereo_activation_scale_threshold(self, value: float):
        try:
            self._stereo_activation_scale_threshold = max(1.0, float(value))
            self._update_control_toolbar_state()
            self._update_auto_flight_rotation(apply_main_canvas=True)
            self._schedule_layers_sync()
            self._schedule_canvas_refresh()
        except Exception as e:
            QgsMessageLog.logMessage(f"SWM: Error updating stereo activation scale: {str(e)}", "SWM-3D", Qgis.Warning)

    def _set_flight_rotation_threshold_deg(self, value: float):
        try:
            self._flight_rotation_threshold_deg = max(0.0, min(180.0, float(value)))
            self._update_control_toolbar_state()
            self._update_auto_flight_rotation(apply_main_canvas=True)
            self._schedule_canvas_refresh()
        except Exception as e:
            QgsMessageLog.logMessage(f"SWM: Error updating flight rotation threshold: {str(e)}", "SWM-3D", Qgis.Warning)

    def reset_stereo_visibility_overrides(self):
        """Clears all stereo visibility overrides and refreshes stereo canvases."""
        if self._stereo_canvas_options and self._stereo_canvas_options.clear_overrides():
            self._last_layers_sync_signature = None
            self._schedule_layers_sync()

    def configure_canvases(self):
        stereo_options = [
            "1 Anaglyph",
            "2 Interlaced even",
            "3 Interlaced odd",
            "4 Side by side",
            "5 Mirror right",
            "6 Mirror up",
        ]
        stereo_choice, ok = QInputDialog.getItem(self.iface.mainWindow(), "Stereo mode", "Select stereo mode for SWM-3D plugin:", stereo_options, 3, False)
        if not ok:
            return("Canceled")
        self.stereo_id = int(stereo_choice.split()[0])

        filter_left = QgsSgdSwmCanvas.FILTER_NONE
        filter_right = QgsSgdSwmCanvas.FILTER_NONE
        if self.stereo_id == 1:
            filter_left = QgsSgdSwmCanvas.FILTER_RED
            filter_right = QgsSgdSwmCanvas.FILTER_CYAN
        elif self.stereo_id == 2:
            filter_left = QgsSgdSwmCanvas.FILTER_EVEN
            filter_right = QgsSgdSwmCanvas.FILTER_ODD
        elif self.stereo_id == 3:
            filter_left = QgsSgdSwmCanvas.FILTER_ODD
            filter_right = QgsSgdSwmCanvas.FILTER_EVEN

        self.canvas_left = QgsSgdSwmCanvas(True, self.qgis_main_canvas, filter_left, self)
        self.canvas_right = QgsSgdSwmCanvas(False, self.qgis_main_canvas, filter_right, self)

        central_widget = QWidget()
        central_widget.setContentsMargins(0, 0, 0, 0)
        central_widget.setAutoFillBackground(False)
        self.setCentralWidget(central_widget)
        if self.stereo_id <= 3:
            # Overlayed stereo modes share the same viewport.
            central_widget.setStyleSheet("background-color: white;")
            layout = QStackedLayout()
            layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
            layout.setCurrentIndex(1)
            self._configure_overlay_canvases()
        elif self.stereo_id <= 5:
            # Side-by-side mode.
            central_widget.setStyleSheet("")
            layout = QHBoxLayout()
            if self.stereo_id == 5:
                self._apply_horizontal_mirror(self.canvas_right)
        elif self.stereo_id == 6:
            # Mirror-right / mirror-up layout.
            central_widget.setStyleSheet("")
            layout = QVBoxLayout()
            self._apply_horizontal_mirror(self.canvas_right)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        central_widget.setLayout(layout)

        layout.addWidget(self.canvas_left)
        layout.addWidget(self.canvas_right)

        return None

    def _configure_overlay_canvases(self):
        """Makes both stereo canvases transparent when stacked."""
        for canvas in (self.canvas_left, self.canvas_right):
            if canvas is None:
                continue
            canvas.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            canvas.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
            viewport = canvas.viewport()
            if viewport is not None:
                viewport.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
                viewport.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
            canvas.setStyleSheet("background: transparent;")

    def _apply_horizontal_mirror(self, canvas):
        """Applies the horizontal mirror used by mirrored stereo layouts."""
        if canvas:
            canvas.setTransform(QTransform().scale(-1, 1))

    def _schedule_canvas_refresh(self):
        """Coalesces repeated refresh requests into one repaint per event loop."""
        if self._canvas_refresh_timer:
            self._canvas_refresh_timer.start()
            self._trace("REFRESH: scheduled")

    def _trace(self, message: str):
        """Structured troubleshooting log for request-flow diagnostics."""
        if not self._trace_wms_debug:
            return
        self._trace_seq += 1
        QgsMessageLog.logMessage(f"TRACE[{self._trace_seq:05d}] {message}", "SWM-3D", Qgis.Info)

    def _stereo_refresh_signature(self) -> Tuple[Any, ...]:
        """Returns a stable signature of current stereo render state for deduping refreshes."""
        def _canvas_state(canvas):
            if not canvas:
                return None
            try:
                ex = canvas.extent()
                rot = float(canvas.rotation()) if hasattr(canvas, 'rotation') else 0.0
                return (
                    round(float(ex.xMinimum()), 3),
                    round(float(ex.yMinimum()), 3),
                    round(float(ex.xMaximum()), 3),
                    round(float(ex.yMaximum()), 3),
                    round(rot, 3),
                    int(canvas.width()) if hasattr(canvas, 'width') else 0,
                    int(canvas.height()) if hasattr(canvas, 'height') else 0,
                )
            except Exception:
                return None

        return (
            _canvas_state(self.canvas_left),
            _canvas_state(self.canvas_right),
            round(float(self._flight_rotation_current_deg), 3),
            int(self._stereo_refresh_revision),
        )

    def _main_canvas_repaint_signature(self) -> Optional[Tuple[Any, ...]]:
        """Returns a signature of main-canvas state used to ignore repeated repaint events."""
        try:
            if not self.qgis_main_canvas:
                return None

            ex = self.qgis_main_canvas.extent()
            crs = self.qgis_main_canvas.mapSettings().destinationCrs() if hasattr(self.qgis_main_canvas, 'mapSettings') else None
            crs_authid = crs.authid() if crs and crs.isValid() else ""
            rotation = float(self.qgis_main_canvas.rotation()) if hasattr(self.qgis_main_canvas, 'rotation') else 0.0

            return (
                round(float(ex.xMinimum()), 6),
                round(float(ex.yMinimum()), 6),
                round(float(ex.xMaximum()), 6),
                round(float(ex.yMaximum()), 6),
                crs_authid,
                round(rotation, 3),
                int(self.qgis_main_canvas.width()) if hasattr(self.qgis_main_canvas, 'width') else 0,
                int(self.qgis_main_canvas.height()) if hasattr(self.qgis_main_canvas, 'height') else 0,
            )
        except Exception:
            return None

    def _current_layers_sync_signature(self) -> Tuple[Any, ...]:
        """Returns a stable signature for current layer stack relevant to stereo sync."""
        items: List[Tuple[str, bool]] = []
        scale_visibility_fragment: Tuple[Tuple[str, bool], ...] = ()
        try:
            project = QgsProject.instance()
            if project:
                root = project.layerTreeRoot()
                if root:
                    for node in root.findLayers():
                        layer = node.layer()
                        if layer:
                            items.append((str(layer.id()), bool(node.isVisible())))
        except Exception:
            items = []

        try:
            project = QgsProject.instance()
            root = project.layerTreeRoot() if project else None
            main_scale = None
            if self.iface and self.iface.mapCanvas() and hasattr(self.iface.mapCanvas(), 'scale'):
                scale_getter = getattr(self.iface.mapCanvas(), 'scale')
                if callable(scale_getter):
                    main_scale = float(cast(Any, scale_getter()))

            if root and main_scale is not None:
                scale_items: List[Tuple[str, bool]] = []
                for node in root.findLayers():
                    layer = node.layer()
                    if not layer or not bool(node.isVisible()):
                        continue

                    has_scale_visibility = getattr(layer, 'hasScaleBasedVisibility', None)
                    if not (callable(has_scale_visibility) and bool(has_scale_visibility())):
                        continue

                    in_range = True
                    in_range_getter = getattr(layer, 'isInScaleRange', None)
                    if callable(in_range_getter):
                        in_range = bool(in_range_getter(main_scale))
                    else:
                        min_scale_getter = getattr(layer, 'minimumScale', None)
                        max_scale_getter = getattr(layer, 'maximumScale', None)
                        min_scale = float(cast(Any, min_scale_getter())) if callable(min_scale_getter) else 0.0
                        max_scale = float(cast(Any, max_scale_getter())) if callable(max_scale_getter) else 0.0
                        above_min = (min_scale <= 0.0) or (main_scale <= min_scale)
                        below_max = (max_scale <= 0.0) or (main_scale >= max_scale)
                        in_range = bool(above_min and below_max)
                    scale_items.append((str(layer.id()), in_range))

                scale_visibility_fragment = tuple(sorted(scale_items))
        except Exception:
            scale_visibility_fragment = ()

        if not items:
            try:
                for layer in self.iface.mapCanvas().layers():
                    items.append((str(layer.id()), True))
            except Exception:
                pass

        stereo_fragment: Tuple[Any, ...] = ()
        if self._stereo_canvas_options:
            stereo_fragment = self._stereo_canvas_options.signature_fragment()

        return (tuple(items), scale_visibility_fragment, stereo_fragment)

    def is_layer_visible_in_stereo(self, layer, main_visible: bool) -> bool:
        """Returns layer visibility for stereo canvases using current override rules."""
        if not self._stereo_canvas_options:
            return bool(main_visible)
        return self._stereo_canvas_options.is_layer_visible_in_stereo(layer, bool(main_visible))

    def _on_stereo_layer_visibility_changed(self, layer_id: str, stereo_visible: bool):
        """Re-sync stereo canvases after a per-layer visibility override change."""
        self._trace(f"LAYER_SYNC: stereo override changed layer={layer_id} visible={stereo_visible}")
        self._last_layers_sync_signature = None
        self._schedule_layers_sync()
        # Refresh is triggered after _sync_canvases_layers() completes so repaint
        # always reflects the new layer stack and avoids timer-race stale frames.

    def _run_canvas_refresh(self):
        """Refreshes both stereo canvases once after rapid state changes settle."""
        signature = self._stereo_refresh_signature()
        if signature == self._last_applied_stereo_refresh_signature:
            self._trace("REFRESH: skipped (same stereo signature)")
            return
        self._last_applied_stereo_refresh_signature = signature
        self._trace(f"REFRESH: apply signature={signature}")

        if self.canvas_left:
            self.canvas_left.refresh()
        if self.canvas_right:
            self.canvas_right.refresh()

    # Z value updates using the mouse wheel
    @property
    def z_cursor(self):
        return self._z_cursor

    @z_cursor.setter
    def z_cursor(self, value):
        self._z_cursor = value
        if self.canvas_left:
            self.canvas_left.update_cursor()
        if self.canvas_right:
            self.canvas_right.update_cursor()
        self._update_z_label()  # Update Z text on stereo canvases

    def _update_z_label(self):
        """Replicates canvas Z changes in the SWM-3D control toolbar."""
        toolbar = self._stereo_controls_toolbar
        if toolbar is not None:
            toolbar.set_z_status(float(self._z_proj_plane), float(self._z_cursor))
        # Update text in canvases
        if self.canvas_left:
            self.canvas_left.update_z_text(self._z_cursor)
        if self.canvas_right:
            self.canvas_right.update_z_text(self._z_cursor)

    def _set_projection_plane_z(self, z):
        """
        Set the projection plane Z coming from the WMS headers.
        This defines the reference Z used by the canvas.
        """
        self._z_proj_plane = z
        self.z_cursor = z  # @z_cursor.setter already propagates cursor update to canvases

    def _sync_canvases_destination_crs(self):
        """
        Keeps stereo canvas destination CRS aligned to SWM service CRS whenever known.
        Falls back to main canvas CRS only if SWM service CRS is not available yet.
        """
        try:
            self._update_swm_service_crs_cache()

            main_crs = self.qgis_main_canvas.mapSettings().destinationCrs()
            if not main_crs or not main_crs.isValid():
                return

            target_crs = self._swm_service_crs if self._swm_service_crs and self._swm_service_crs.isValid() else main_crs

            if self.canvas_left:
                left_crs = self.canvas_left.mapSettings().destinationCrs()
                if (not left_crs) or (not left_crs.isValid()) or (left_crs != target_crs):
                    self.canvas_left.setDestinationCrs(target_crs)

            if self.canvas_right:
                right_crs = self.canvas_right.mapSettings().destinationCrs()
                if (not right_crs) or (not right_crs.isValid()) or (right_crs != target_crs):
                    self.canvas_right.setDestinationCrs(target_crs)

        except Exception as e:
            QgsMessageLog.logMessage(f"CRS: Error syncing stereo destination CRS: {str(e)}", "SWM-3D", Qgis.Warning)

    def _update_swm_service_crs_cache(self):
        """
        Updates cached SWM service CRS from available SWM layers.
        """
        try:
            for layer in self.qgis_main_canvas.layers():
                if is_sgd_swm_layer(layer):
                    layer_crs = layer.crs()
                    if layer_crs and layer_crs.isValid():
                        self._swm_service_crs = layer_crs
                        return

            if self.canvas_left and self.canvas_left.layer_swm:
                layer_crs = self.canvas_left.layer_swm.crs()
                if layer_crs and layer_crs.isValid():
                    self._swm_service_crs = layer_crs
                    return

            if self.canvas_right and self.canvas_right.layer_swm:
                layer_crs = self.canvas_right.layer_swm.crs()
                if layer_crs and layer_crs.isValid():
                    self._swm_service_crs = layer_crs
                    return
        except Exception:
            pass

    def _on_main_canvas_crs_changed(self, *args):
        """
        Reacts to main canvas destination CRS changes.
        """
        self._sync_canvases_destination_crs()
        self._sync_canvases_repaint()

    def _on_main_canvas_scale_changed(self, *args):
        """Re-evaluates stereo layer visibility immediately after scale changes."""
        scale_changed = False
        try:
            if args:
                current_scale = float(cast(Any, args[0]))
            else:
                scale_getter = getattr(self.iface.mapCanvas(), 'scale', None)
                current_scale = float(cast(Any, scale_getter())) if callable(scale_getter) else None

            if current_scale is not None:
                scale_changed = (
                    self._last_main_scale_for_layer_sync is None
                    or abs(float(current_scale) - float(self._last_main_scale_for_layer_sync)) > 1e-6
                )
                if scale_changed:
                    self._last_main_scale_for_layer_sync = float(current_scale)
                    self._last_layers_sync_signature = None
        except Exception:
            pass

        if scale_changed:
            self._sync_canvases_layers()
        else:
            self._schedule_layers_sync()
        self._schedule_canvas_refresh()

    def _reproject_extent_to_stereo_crs(self, extent, target_canvas):
        """
        Reprojects an extent from main canvas CRS to the provided stereo canvas CRS (if different).
        Returns the original extent if CRS match or if reprojection cannot be performed.
        """
        try:
            if not target_canvas:
                return extent
                
            source_crs = self.qgis_main_canvas.mapSettings().destinationCrs()
            dest_crs = target_canvas.mapSettings().destinationCrs()
            
            if not source_crs or not source_crs.isValid():
                return extent
            if not dest_crs or not dest_crs.isValid():
                return extent
            if source_crs == dest_crs:
                return extent
                
            # Create transform and reproject extent
            trf = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())
            reprojected = trf.transformBoundingBox(extent)
            return reprojected
        except Exception as e:
            QgsMessageLog.logMessage(f"CRS: Error reprojecting extent to stereo CRS: {str(e)}",
                                     "SWM-3D", Qgis.Warning)
            return extent

    def _sync_canvases_cursor(self, point_xy):
        """
        Synchronizes the cursor position (XY) of the main QGIS canvas into the two steresocopic canvas.
        """
        if self.canvas_left:
            self.canvas_left.sync_cursor(point_xy)
        if self.canvas_right:
            self.canvas_right.sync_cursor(point_xy)

    def _sync_canvases_repaint(self):
        """
        On zoom/pan in the main canvas:
        1) SWM should have been called to update imagery and network_reply_handle should run.
        2) New projections should be loaded, updating Geometry Generator on Z layers and SWM layer.
        3) A final refresh is still needed so everything is repainted.
        """
        main_signature = self._main_canvas_repaint_signature()
        self._trace(f"REPAINT: signal main_signature={main_signature}")
        if main_signature is not None and main_signature == self._last_main_repaint_signature:
            self._trace("REPAINT: skipped (same main signature)")
            return
        self._last_main_repaint_signature = main_signature
        self._update_main_canvas_north_indicator()

        # Some environments do not emit a reliable scaleChanged signal during wheel zoom.
        # Detect scale transitions here and force one layer-sync pass when scale changed.
        try:
            current_scale = None
            scale_getter = getattr(self.qgis_main_canvas, 'scale', None)
            if callable(scale_getter):
                current_scale = float(cast(Any, scale_getter()))

            if current_scale is not None:
                changed = (
                    self._last_main_scale_for_layer_sync is None
                    or abs(float(current_scale) - float(self._last_main_scale_for_layer_sync)) > 1e-6
                )
                if changed:
                    self._last_main_scale_for_layer_sync = float(current_scale)
                    self._last_layers_sync_signature = None
                    self._sync_canvases_layers()
        except Exception:
            pass

        self._sync_canvases_destination_crs()
        if self.canvas_left:
            extent_left = self._reproject_extent_to_stereo_crs(self.qgis_main_canvas.extent(), self.canvas_left)
            current_left_extent = self.canvas_left.extent()
            if current_left_extent != extent_left:
                self.canvas_left.setExtent(extent_left)
        if self.canvas_right:
            extent_right = self._reproject_extent_to_stereo_crs(self.qgis_main_canvas.extent(), self.canvas_right)
            current_right_extent = self.canvas_right.extent()
            if current_right_extent != extent_right:
                self.canvas_right.setExtent(extent_right)

        # Re-evaluate strip rotation in the same zoom/pan cycle.
        rotation_changed = self._update_auto_flight_rotation()
        self._trace(f"REPAINT: post-update rotation_changed={rotation_changed}")

        # Always request stereo refresh for zoom/pan. Duplicate calls are
        # filtered in _run_canvas_refresh() using the refresh signature.
        self._schedule_canvas_refresh()

    def _sync_canvases_layers(self):
        """
        Propagate layer changes (visibility, add/remove/reorder) to plugin canvases.
        Reusing SWM layers is handled inside each stereo canvas to avoid
        repeated WMS requests during visibility toggles.
        """
        if self._layers_rolling_back:
            self._trace("LAYER_SYNC: skipped (rollback active)")
            return

        layers_signature = self._current_layers_sync_signature()
        if self._last_layers_sync_signature == layers_signature:
            self._trace("LAYER_SYNC: skipped (same layers signature)")
            return
        self._last_layers_sync_signature = layers_signature
        self._trace(f"LAYER_SYNC: apply signature={layers_signature}")

        # Keep hooks aligned with currently active Z layers.
        self._update_digitizing_layer_hooks()
        self._update_style_layer_hooks()

        if self.canvas_left:
            self.canvas_left.sync_layers()
        if self.canvas_right:
            self.canvas_right.sync_layers()

        # Layer ordering/visibility changes must invalidate refresh dedupe.
        self._stereo_refresh_revision += 1

        # Layer visibility/type changes can update SWM service CRS availability.
        self._update_swm_service_crs_cache()
        self._sync_canvases_destination_crs()
        # Run one debounced refresh after layer sync so visibility toggles apply
        # immediately even when no pan/zoom occurs next.
        self._schedule_canvas_refresh()

    def _schedule_layers_sync(self, *args):
        """
        Coalesces rapid layer/tree events into a single sync pass.
        This prevents duplicate sync_layers() calls for one user action.
        """
        if self._layers_rolling_back:
            self._trace("LAYER_SYNC: schedule skipped (rollback active)")
            return

        if self._layer_sync_timer:
            self._layer_sync_timer.start()
            self._trace("LAYER_SYNC: scheduled")

    @staticmethod
    def _extract_wms_request_brief(request_url: str) -> str:
        """Extracts compact WMS request details from URL for diagnostics."""
        try:
            style_m = re.search(r'(?:[?&])STYLES=([^&]*)', request_url, flags=re.IGNORECASE)
            bbox_m = re.search(r'(?:[?&])BBOX=([^&]+)', request_url, flags=re.IGNORECASE)
            w_m = re.search(r'(?:[?&])WIDTH=(\d+)', request_url, flags=re.IGNORECASE)
            h_m = re.search(r'(?:[?&])HEIGHT=(\d+)', request_url, flags=re.IGNORECASE)
            style = style_m.group(1) if style_m else ""
            bbox = bbox_m.group(1) if bbox_m else ""
            w = w_m.group(1) if w_m else ""
            h = h_m.group(1) if h_m else ""
            return f"style={style} bbox={bbox} size={w}x{h}"
        except Exception:
            return "style=? bbox=? size=?"

    @staticmethod
    def _extract_wms_bbox(request_url: str) -> Optional[str]:
        """Extract raw BBOX value from a WMS URL (encoded string)."""
        try:
            bbox_m = re.search(r'(?:[?&])BBOX=([^&]+)', request_url, flags=re.IGNORECASE)
            if not bbox_m:
                return None
            return bbox_m.group(1)
        except Exception:
            return None

    def _update_auto_rotation_for_matched_stereo_bbox(self) -> bool:
        """
        Applies strip auto-rotation once both LEFT/RIGHT replies are available
        for the same BBOX. This guarantees we do not mix photo centers from
        different requests.
        """
        bbox_left = self._last_photo_bbox_left
        bbox_right = self._last_photo_bbox_right
        if not bbox_left or not bbox_right:
            return False
        if bbox_left != bbox_right:
            return False
        if self._last_rotation_bbox_applied == bbox_left:
            return False

        self._last_rotation_bbox_applied = bbox_left
        rotation_changed = self._update_auto_flight_rotation(apply_main_canvas=True)
        return rotation_changed

    def _update_digitizing_layer_hooks(self):
        """
        Connects editing interceptors for currently visible vector layers.
        """
        try:
            current_vector_layers = {}
            for layer in self.iface.mapCanvas().layers():
                if isinstance(layer, QgsVectorLayer):
                    current_vector_layers[layer.id()] = layer

            # Disconnect removed hooks first.
            for layer_id in list(self._layer_edit_hooks.keys()):
                if layer_id not in current_vector_layers:
                    self._disconnect_digitizing_hooks(layer_id)

            # Connect new hooks.
            for layer_id, layer in current_vector_layers.items():
                if layer_id not in self._layer_edit_hooks:
                    self._connect_digitizing_hooks(layer)

        except Exception as e:
            QgsMessageLog.logMessage(f"INTERCEPTOR: Error updating hooks: {str(e)}", "SWM-3D", Qgis.Warning)

    def _update_style_layer_hooks(self):
        """
        Connects style-change hooks for currently visible Z-enabled vector layers.
        These hooks trigger immediate renderer synchronization in stereo canvases.
        """
        try:
            current_z_layers = {}
            for layer in self.iface.mapCanvas().layers():
                if is_z_layer(layer):
                    current_z_layers[layer.id()] = layer

            for layer_id in list(self._layer_style_hooks.keys()):
                if layer_id not in current_z_layers:
                    self._disconnect_style_hooks(layer_id)

            for layer_id, layer in current_z_layers.items():
                if layer_id not in self._layer_style_hooks:
                    self._connect_style_hooks(layer)

        except Exception as e:
            QgsMessageLog.logMessage(f"STYLE_SYNC: Error updating style hooks: {str(e)}", "SWM-3D", Qgis.Warning)

    def _connect_style_hooks(self, layer):
        """
        Connects renderer/style change signals for one Z-enabled layer.
        """
        try:
            style_slot = lambda *args, lyr=layer: self._on_layer_style_changed(lyr)

            if hasattr(layer, 'rendererChanged'):
                layer.rendererChanged.connect(style_slot)
            if hasattr(layer, 'styleChanged'):
                layer.styleChanged.connect(style_slot)

            self._layer_style_hooks[layer.id()] = {
                "layer": layer,
                "style_slot": style_slot,
            }

        except Exception as e:
            QgsMessageLog.logMessage(f"STYLE_SYNC: Error connecting hooks for {layer.name()}: {str(e)}", "SWM-3D", Qgis.Warning)

    def _disconnect_style_hooks(self, layer_id):
        """
        Disconnects style hooks for one layer id.
        """
        hook = self._layer_style_hooks.get(layer_id)
        if not hook:
            return

        layer = hook.get("layer")
        style_slot = hook.get("style_slot")

        try:
            if layer and style_slot:
                if hasattr(layer, 'rendererChanged'):
                    layer.rendererChanged.disconnect(style_slot)
                if hasattr(layer, 'styleChanged'):
                    layer.styleChanged.disconnect(style_slot)
        except (RuntimeError, TypeError):
            pass

        self._layer_style_hooks.pop(layer_id, None)

    def _disconnect_all_style_hooks(self):
        """
        Disconnects all style hooks.
        """
        for layer_id in list(self._layer_style_hooks.keys()):
            self._disconnect_style_hooks(layer_id)

    def _on_layer_style_changed(self, layer):
        """
        Handles style changes from the main layer and schedules a debounced
        propagation to stereo canvases.
        """
        try:
            if not is_z_layer(layer):
                return

            self._schedule_style_sync()

        except Exception as e:
            QgsMessageLog.logMessage(f"STYLE_SYNC: Error syncing style for {layer.name()}: {str(e)}", "SWM-3D", Qgis.Warning)

    def _schedule_style_sync(self):
        """
        Schedules style synchronization with debounce to avoid repeated
        expensive resyncs while the user is editing symbology.
        """
        self._pending_style_sync = True
        self._style_sync_timer.start()

    def _run_pending_style_sync(self):
        """
        Runs the queued style synchronization once debounce interval expires.
        """
        if not self._pending_style_sync:
            return

        self._pending_style_sync = False
        try:
            if self.canvas_left:
                self.canvas_left.sync_layers()
            if self.canvas_right:
                self.canvas_right.sync_layers()
            self._stereo_refresh_revision += 1
            self._schedule_canvas_refresh()
        except Exception as e:
            QgsMessageLog.logMessage(f"STYLE_SYNC: Error in debounced sync: {str(e)}", "SWM-3D", Qgis.Warning)

    def _connect_digitizing_hooks(self, layer):
        """
        Connect feature and geometry edit signals for one vector layer.
        """
        try:
            feature_slot = lambda fid, lyr=layer: self._on_layer_feature_added(lyr, fid)
            geometry_slot = lambda fid, geom, lyr=layer: self._on_layer_geometry_changed(lyr, fid, geom)
            before_rollback_slot = lambda lyr=layer: self._on_layer_before_rollback(lyr)
            after_rollback_slot = lambda lyr=layer: self._on_layer_after_rollback(lyr)

            layer.featureAdded.connect(feature_slot)
            layer.geometryChanged.connect(geometry_slot)
            connected_before_rollback_signal = None
            connected_after_rollback_signal = None
            for signal_name in ('beforeRollBack', 'beforeRollback'):
                if hasattr(layer, signal_name):
                    getattr(layer, signal_name).connect(before_rollback_slot)
                    connected_before_rollback_signal = signal_name
                    break
            for signal_name in ('afterRollBack', 'afterRollback'):
                if hasattr(layer, signal_name):
                    getattr(layer, signal_name).connect(after_rollback_slot)
                    connected_after_rollback_signal = signal_name
                    break

            self._layer_edit_hooks[layer.id()] = {
                "layer": layer,
                "feature_slot": feature_slot,
                "geometry_slot": geometry_slot,
                "before_rollback_slot": before_rollback_slot,
                "after_rollback_slot": after_rollback_slot,
                "before_rollback_signal": connected_before_rollback_signal,
                "after_rollback_signal": connected_after_rollback_signal,
            }

        except Exception as e:
            QgsMessageLog.logMessage(f"INTERCEPTOR: Error connecting layer hooks: {str(e)}", "SWM-3D", Qgis.Warning)

    def _disconnect_digitizing_hooks(self, layer_id):
        """
        Disconnect feature and geometry edit signals for one layer.
        """
        hook = self._layer_edit_hooks.get(layer_id)
        if not hook:
            return

        layer = hook.get("layer")
        if layer is None:
            self._layer_edit_hooks.pop(layer_id, None)
            return

        try:
            layer.featureAdded.disconnect(hook["feature_slot"])
        except Exception:
            pass

        try:
            layer.geometryChanged.disconnect(hook["geometry_slot"])
        except Exception:
            pass

        try:
            signal_name = hook.get("before_rollback_signal")
            if signal_name and hook.get("before_rollback_slot") and hasattr(layer, signal_name):
                getattr(layer, signal_name).disconnect(hook["before_rollback_slot"])
        except Exception:
            pass

        try:
            signal_name = hook.get("after_rollback_signal")
            if signal_name and hook.get("after_rollback_slot") and hasattr(layer, signal_name):
                getattr(layer, signal_name).disconnect(hook["after_rollback_slot"])
        except Exception:
            pass

        self._layer_edit_hooks.pop(layer_id, None)
        self._layers_rolling_back.discard(layer_id)
        # Drop per-feature Z history for removed layer hooks.
        keys_to_remove = [k for k in self._feature_vertex_z_history.keys() if k[0] == layer_id]
        for k in keys_to_remove:
            self._feature_vertex_z_history.pop(k, None)
        self._pending_digitize_z_clicks.pop(layer_id, None)
        self._last_click_capture.pop(layer_id, None)

    def _disconnect_all_digitizing_hooks(self):
        """
        Disconnect all currently registered layer edit hooks.
        """
        for layer_id in list(self._layer_edit_hooks.keys()):
            self._disconnect_digitizing_hooks(layer_id)
        self._geometry_update_guard.clear()
        self._feature_vertex_z_history.clear()
        self._pending_digitize_z_clicks.clear()
        self._last_click_capture.clear()
        self._layers_rolling_back.clear()

    def _on_layer_before_rollback(self, layer):
        """
        Temporarily disable edit-driven sync while layer rollback is running.
        """
        try:
            self._layers_rolling_back.add(layer.id())
            if self._layer_sync_timer and self._layer_sync_timer.isActive():
                self._layer_sync_timer.stop()
            self._geometry_update_guard.clear()
            keys_to_remove = [k for k in self._feature_vertex_z_history.keys() if k[0] == layer.id()]
            for k in keys_to_remove:
                self._feature_vertex_z_history.pop(k, None)
            self._pending_digitize_z_clicks.pop(layer.id(), None)
            self._last_click_capture.pop(layer.id(), None)
        except Exception:
            pass

    def _is_capture_map_tool_active(self) -> bool:
        """
        Returns True when a QGIS capture map tool (line/polygon digitizing) appears active.
        """
        try:
            tool = self.iface.mapCanvas().mapTool() if self.iface and self.iface.mapCanvas() else None
            if not tool:
                return False
            tool_name = type(tool).__name__
            if "Identify" in tool_name:
                return False
            return (
                "Capture" in tool_name
                or "DigitizeFeature" in tool_name
                or "AddFeature" in tool_name
            )
        except Exception:
            return False

    def _on_layer_after_rollback(self, layer):
        """
        Re-enable edit-driven sync after rollback and refresh stereo layers once.
        """
        try:
            self._layers_rolling_back.discard(layer.id())
            self._schedule_layers_sync()
        except Exception:
            pass

    def _on_layer_feature_added(self, layer, fid: int):
        """
        Triggered when a new feature is digitized.
        """
        if layer.id() in self._layers_rolling_back:
            return

        if is_z_layer(layer):
            # Defer Z write so we do not mutate geometry re-entrantly
            # while QGIS is still finalizing the edit command/undo state.
            QTimer.singleShot(0, lambda lyr=layer, feature_id=fid: self._apply_cursor_z_to_feature(lyr, feature_id, None))
        self._schedule_layers_sync()
        self._schedule_canvas_refresh()

    def _on_layer_geometry_changed(self, layer, fid: int, geom: QgsGeometry):
        """
        Triggered when an edited geometry changes.
        """
        if layer.id() in self._layers_rolling_back:
            return

        if is_z_layer(layer):
            self._capture_feature_vertex_z(layer, fid, geom)

        # Keep this handler read-only for stability during undo/rollback flows.
        self._schedule_layers_sync()
        self._schedule_canvas_refresh()

    def _capture_feature_vertex_z(self, layer, fid: int, geom: QgsGeometry):
        """
        Captures Z per vertex as digitizing progresses for one feature.
        Only appends values when vertex count grows.
        """
        try:
            if not geom or geom.isEmpty():
                return
            const_geom = geom.constGet()
            if const_geom is None:
                return

            key = (layer.id(), int(fid))
            vertex_count = const_geom.vertexCount()
            if vertex_count <= 0:
                self._feature_vertex_z_history.pop(key, None)
                return

            z_history = self._feature_vertex_z_history.setdefault(key, [])

            if len(z_history) > vertex_count:
                z_history[:] = z_history[:vertex_count]

            cursor_z = float(self.z_cursor)
            while len(z_history) < vertex_count:
                src_idx = len(z_history)
                src_v = geom.vertexAt(src_idx)
                src_z = src_v.z()
                if math.isfinite(src_z):
                    z_history.append(float(src_z))
                else:
                    z_history.append(cursor_z)

        except Exception as e:
            QgsMessageLog.logMessage(f"INTERCEPTOR: Error capturing vertex Z for feature {fid}: {str(e)}", "SWM-3D", Qgis.Warning)

    def _apply_cursor_z_to_feature(self, layer, fid: int, geom: Optional[QgsGeometry]):
        """
        Assigns current cursor Z to vertices that still have Z=0 or non-finite Z.
        """
        if layer.id() in self._layers_rolling_back:
            return

        guard_key = (layer.id(), int(fid))
        if guard_key in self._geometry_update_guard:
            return

        try:
            working_geom = QgsGeometry(geom) if geom else QgsGeometry()
            if working_geom.isEmpty():
                feature = layer.getFeature(fid)
                if not feature.isValid():
                    return
                working_geom = feature.geometry()

            if not working_geom or working_geom.isEmpty():
                return


            const_geom = working_geom.constGet()
            if const_geom is None:
                return

            changed = False
            key = (layer.id(), int(fid))
            z_history = self._feature_vertex_z_history.get(key)
            vtx_count = const_geom.vertexCount()

            # Primary fallback for featureAdded timing race: use recorded click Z values.
            if (not z_history) and vtx_count > 0:
                pending = self._pending_digitize_z_clicks.get(layer.id(), [])
                if len(pending) >= vtx_count:
                    z_history = pending[-vtx_count:].copy()
                    del pending[-vtx_count:]
                    if not pending:
                        self._pending_digitize_z_clicks.pop(layer.id(), None)

            if (not z_history) and self.canvas_left and hasattr(self.canvas_left, 'get_tracked_z_for_geometry'):
                try:
                    z_history = self.canvas_left.get_tracked_z_for_geometry(working_geom)
                except Exception:
                    z_history = self._feature_vertex_z_history.get(key)
            if (not z_history) and self.canvas_right and hasattr(self.canvas_right, 'get_tracked_z_for_geometry'):
                try:
                    z_history = self.canvas_right.get_tracked_z_for_geometry(working_geom)
                except Exception:
                    z_history = self._feature_vertex_z_history.get(key)
            for i in range(vtx_count):
                vertex = working_geom.vertexAt(i)
                if z_history and i < len(z_history):
                    z_to_apply = float(z_history[i])
                else:
                    z_to_apply = float(self.z_cursor)
                current_z = vertex.z()
                if (not math.isfinite(current_z)) or abs(current_z) < 1e-9 or abs(current_z - z_to_apply) > 1e-6:
                    vertex.setZ(z_to_apply)
                    if working_geom.moveVertex(vertex, i):
                        changed = True

            if not changed:
                return

            self._geometry_update_guard.add(guard_key)
            ok = layer.changeGeometry(fid, working_geom)
            if ok:
                # Geometry is persisted; tracker is no longer needed for this feature.
                self._feature_vertex_z_history.pop(key, None)
                self._pending_digitize_z_clicks.pop(layer.id(), None)
                self._last_click_capture.pop(layer.id(), None)
            else:
                QgsMessageLog.logMessage(
                    f"INTERCEPTOR: Failed applying Z to feature {fid} in '{layer.name()}'",
                    "SWM-3D", Qgis.Warning
                )

        except Exception as e:
            QgsMessageLog.logMessage(f"INTERCEPTOR: Error applying Z to feature {fid}: {str(e)}", "SWM-3D", Qgis.Warning)
        finally:
            self._geometry_update_guard.discard(guard_key)

    def eventFilter(self, obj, event):
        """
         Cursor entry control.
         Mouse wheel Z control (ALT + wheel).
        """
        try:
            if event.type() == QEvent.Type.Resize:
                try:
                    main_canvas = self.iface.mapCanvas() if self.iface else None
                    vp = main_canvas.viewport() if (main_canvas and hasattr(main_canvas, 'viewport')) else None
                    if obj is main_canvas or (vp is not None and obj is vp):
                        self._update_main_canvas_north_indicator()
                except Exception:
                    pass

            # Cursor entry control in the photogrammetric window.
            is_stereo_object = False
            if isinstance(obj, QWidget):
                is_stereo_object = (obj is self) or (obj.window() is self)

            if is_stereo_object and event.type() in (QEvent.Type.Enter, QEvent.Type.MouseMove, QEvent.Type.HoverMove):
                self._keep_cursor_in_main_qgis_window()
                return True

            # Wheel-based Z control
            if event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    try:
                        main_canvas = self.iface.mapCanvas() if self.iface else None
                        from_main_canvas = False
                        if main_canvas:
                            vp = main_canvas.viewport() if hasattr(main_canvas, 'viewport') else None
                            from_main_canvas = (obj is main_canvas) or (vp is not None and obj is vp)

                        active_layer = self.iface.activeLayer() if self.iface else None
                        if (
                            not is_stereo_object
                            and from_main_canvas
                            and isinstance(active_layer, QgsVectorLayer)
                            and active_layer.isEditable()
                            and is_z_layer(active_layer)
                            and self._is_capture_map_tool_active()
                        ):
                            pending = self._pending_digitize_z_clicks.setdefault(active_layer.id(), [])
                            current_z = float(self.z_cursor)
                            if len(pending) > 512:
                                del pending[:-512]
                            tool_name = ""
                            if main_canvas and hasattr(main_canvas, 'mapTool'):
                                tool = main_canvas.mapTool()
                                tool_name = type(tool).__name__ if tool else ""

                            now = time.monotonic()
                            last = self._last_click_capture.get(active_layer.id())
                            is_duplicate = False
                            if last:
                                last_t, last_z, last_tool = last
                                if (now - last_t) < 0.25 and abs(last_z - current_z) < 1e-6 and last_tool == tool_name:
                                    is_duplicate = True

                            if not is_duplicate:
                                pending.append(current_z)
                                self._last_click_capture[active_layer.id()] = (now, current_z, tool_name)
                    except Exception:
                        pass

            if event.type() == QEvent.Type.Wheel:
                modifiers = QApplication.keyboardModifiers()
                if not (modifiers & Qt.KeyboardModifier.AltModifier):
                    return False
                    
                delta = -event.angleDelta().x() / 120.
                if modifiers & Qt.KeyboardModifier.ControlModifier:
                    delta /= 10.
                elif modifiers & Qt.KeyboardModifier.ShiftModifier:
                    delta *= 10.
                    
                self.z_cursor = round(self.z_cursor + delta, 1)
                return True
                
            return False
        except Exception:
            return False

    def network_reply_handle(self, reply):
        """
        Handler for WMS network replies. Checks if the reply is from a SWM plugin layer by looking for specific headers.
        If it is a SWM reply, reads the projection plane Z and transformation parameters from the headers, 
        updates the canvas state, and triggers a refresh to apply the new transformations.
        Through self.network_manager.finished.connect(self.network_reply_handle),
        this method is connected to QgsNetworkAccessManager's finished() signal,
        which is emitted every time a server response is received.
        """
        # Do not require window visibility here: first SWM headers can arrive
        # during startup before the stereo window is fully shown.
        if not self.canvas_left and not self.canvas_right:
            return
        request_url = reply.request().url().toString()
        self._trace(f"NET: finished {self._extract_wms_request_brief(request_url)}")

        # Check whether the reply belongs to SWM and is valid
        # Requests from the main QGIS screen come as PhotoRight or PhotoLeft.
        # We use that uppercase style tag difference to filter them.
        is_photo_left = re.search(r'STYLES=PHOTOLEFT', request_url) is not None
        is_photo_right = re.search(r'STYLES=PHOTORIGHT', request_url) is not None
        is_swm_reply = is_photo_left or is_photo_right
        if not is_swm_reply:
            return
        is_swm_reply &= reply.hasRawHeader(b'SIGRID_PROJECTIONPLAINZ')
        is_swm_reply &= reply.hasRawHeader(b'SIGRID_PhtTransWorld3DToPhoto')
        is_swm_reply &= reply.hasRawHeader(b'SIGRID_PhtTransPhotoToCanvas')
        if not is_swm_reply:
            return
        self._trace(f"NET: accepted SWM {self._extract_wms_request_brief(request_url)}")
        request_bbox = self._extract_wms_bbox(request_url)
        # Sure it is a SWM plugin WMS request layer
        if is_photo_left:
            # Get projection plane Z value from the reply headers. Only left, need not read twice
            text = reply.rawHeader(b'SIGRID_PROJECTIONPLAINZ').data().decode('utf-8').strip()
            try:
                z_proj_plane = float(text)
            except ValueError:
                QgsMessageLog.logMessage(f"[DEBUG] <network_reply_handler> Invalid SIGRID_PROJECTIONPLAINZ value: ({text})",
                                         "SWM-3D", Qgis.Warning)
                return

            self._set_projection_plane_z(z_proj_plane)
            if self.canvas_left:
                self.canvas_left.update_data_from_wms_header(reply)
                trf_left = self.canvas_left.trf_wld2prp
                if trf_left:
                    self._photo_center_left = (trf_left.x0, trf_left.y0)
            self._last_photo_bbox_left = request_bbox
            self._update_auto_rotation_for_matched_stereo_bbox()
            if not self._has_received_swm_reply:
                self._has_received_swm_reply = True
                # This timer waits for the next Qt event-loop cycle
                QTimer.singleShot(0, self.showFullScreen)  # Restores with ESC in keyPressEvent
        else:
            if self.canvas_right:
                self.canvas_right.update_data_from_wms_header(reply)
                trf_right = self.canvas_right.trf_wld2prp
                if trf_right:
                    self._photo_center_right = (trf_right.x0, trf_right.y0)
            self._last_photo_bbox_right = request_bbox
            self._update_auto_rotation_for_matched_stereo_bbox()

    def keyPressEvent(self, event):
        """Only used to exit FullScreen mode."""
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal()
            event.accept()
            return
        super().keyPressEvent(event)

    def set_screen(self):
        # Get the available screens
        screens = QGuiApplication.screens()
        screen_count = len(screens)
        current_screen = QGuiApplication.screenAt(self.iface.mainWindow().geometry().center())

        if screen_count == 1:
            return "There is only one screen available. Need at least two screens."
        elif screen_count == 2:
            # Automatically select the other screen
            swm_screen = screens[1] if screens[0] == current_screen else screens[0]
        else:
            # Show a dialog to select the screen, excluding the current screen
            screen_options = [
                screens[i].name() for i in range(screen_count) if screens[i] != current_screen
            ]
            widget = self.iface.mainWindow()
            screen_choice, ok = QInputDialog.getItem(
                widget,
                "Select screen",
                "Select screen for SWM window:",
                screen_options,
                0,
                False,
            )
            if not ok:
                return "canceled"
            # Find the selected screen by name
            swm_screen = next(
                (screen for screen in screens if screen.name() == screen_choice), None
            )
        # Set geometry to target screen
        if swm_screen:
            self.swm_screen_geometry = swm_screen.geometry()
            self.setGeometry(self.swm_screen_geometry)
        else:
            return "Error: Selected screen not found."

        return None
