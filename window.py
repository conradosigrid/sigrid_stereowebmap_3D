
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
from qgis.PyQt.QtWidgets import QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QStackedLayout
from qgis.PyQt.QtWidgets import QInputDialog, QMessageBox, QLabel, QApplication
from qgis.PyQt.QtGui import QTransform, QGuiApplication, QCursor
from qgis.PyQt.QtCore import Qt, QEvent, QTimer
import re

# SWM libraries
from .canvas import QgsSgdSwmCanvas
from .utils import is_z_layer
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

        # Set the screen for the Swm Window
        self.init_error = self.set_screen()
        if self.init_error:
            return

        # Mount plugin window
        self.init_error = self.configure_canvases()
        if self.init_error:
            self.iface.messageBar().pushCritical("SWM-3D", self.init_error)
            return

        self.z_label = QLabel("Z=----")
        # self.z_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.z_label.setMinimumWidth(60)
        self.iface.mainWindow().statusBar().addPermanentWidget(self.z_label)

        # Shared global Z; always assign through @z_cursor.setter to update everything
        self._z_proj_plane = 0.0
        self.z_cursor = 0.0
        # Install event filter only for wheel-based Z control
        QApplication.instance().installEventFilter(self)

        # Capturar eventos del canvas principal qgis
        self.iface.mapCanvas().xyCoordinates.connect(self._sync_canvases_cursor)      # mouse
        self.iface.mapCanvas().layersChanged.connect(self._sync_canvases_layers)      # new layers
        self.iface.mapCanvas().extentsChanged.connect(self._sync_canvases_repaint)    # zoom and pan

        # Network Manager WMS
        # https://chat.deepseek.com/a/chat/s/5dc872fa-208d-458c-836b-9199dcc3a37c
        self.network_manager = QgsNetworkAccessManager.instance()      
        if self.network_manager:
            self.network_manager.finished.connect(self.network_reply_handle)

    def showEvent(self, event):
        """
        Virtual method inherited from QWidget / QMainWindow.
        """
        super().showEvent(event)
        # Initial sync after the window is shown
        if self.canvas_left:
            self.canvas_left.setExtent(self.qgis_main_canvas.extent())
            self.canvas_left.sync_layers()
        if self.canvas_right:
            self.canvas_right.setExtent(self.qgis_main_canvas.extent())
            self.canvas_right.sync_layers()
        # Keep for now; may be removed later if unnecessary
        if self.canvas_left:
            self.canvas_left.refresh()
        if self.canvas_right:
            self.canvas_right.refresh()

    def enterEvent(self, event):
        self._keep_cursor_in_main_qgis_window()
        super().enterEvent(event)

    def closeEvent(self, event):
        try:
            # Clean up event filter
            QApplication.instance().removeEventFilter(self)
            
            # Clean up synchronization in secondary canvases
            if self.canvas_left:
                self.canvas_left.cleanup_canvas_items_sync()
            if self.canvas_right:
                self.canvas_right.cleanup_canvas_items_sync()
            
            # Remove Z label from the status bar
            try:
                self.iface.mainWindow().statusBar().removeWidget(self.z_label)
            except:
                pass
                
        except Exception as e:
            QgsMessageLog.logMessage(f"Cleanup error: {str(e)}", "SWM-3D", QgsMessageLog.MessageType.Critical)
        
        super().closeEvent(event)

    def _keep_cursor_in_main_qgis_window(self):
        """Prevents the cursor from staying over the stereo window (cross-platform)."""
        if _debug_module is not None and _debug_module.DEBUG:
            return

        try:
            main_rect = self.iface.mainWindow().frameGeometry()
            p = QCursor.pos()
            x = min(max(p.x(), main_rect.left() + 1), main_rect.right() - 1)
            y = min(max(p.y(), main_rect.top() + 1), main_rect.bottom() - 1)
            QCursor.setPos(x, y)
        except Exception:
            pass

    def configure_canvases(self):
        # Select stereo mode
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
        stereo_name = stereo_choice.split()[1]

        filter_letf = QgsSgdSwmCanvas.FILTER_NONE
        filter_right = QgsSgdSwmCanvas.FILTER_NONE
        if self.stereo_id == 1:
            filter_letf = QgsSgdSwmCanvas.FILTER_RED
            filter_right = QgsSgdSwmCanvas.FILTER_CYAN
        elif self.stereo_id == 2:
            filter_letf = QgsSgdSwmCanvas.FILTER_EVEN
            filter_right = QgsSgdSwmCanvas.FILTER_ODD
        elif self.stereo_id == 3:
            filter_letf = QgsSgdSwmCanvas.FILTER_ODD
            filter_right = QgsSgdSwmCanvas.FILTER_EVEN
        # FALTA
        # elif self.stereo_id == 4:
        #     filter_letf = QgsSgdSwmCanvas.FILTER_RED
        #     filter_right = QgsSgdSwmCanvas.FILTER_CYAN
        # elif self.stereo_id == 5:
        #     filter_letf = QgsSgdSwmCanvas.FILTER_ODD
        #     filter_right = QgsSgdSwmCanvas.FILTER_EVEN

        self.canvas_left = QgsSgdSwmCanvas(True, self.qgis_main_canvas, filter_letf, self)
        self.canvas_right = QgsSgdSwmCanvas(False, self.qgis_main_canvas, filter_right, self)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        if self.stereo_id <= 3:
            # Overlayed canvas
            layout = QStackedLayout()
            layout.setStackingMode(QStackedLayout.StackAll)
            layout.setCurrentIndex(1)  # Ensure the right canvas stays on top
        elif self.stereo_id <= 5:
            # Horizontally disposed canvas
            layout = QHBoxLayout()
            if self.stereo_id == 5:
                self.canvas_right.setTransform(QTransform().scale(-1, 1))  # Mirror right canvas horizontally
        elif self.stereo_id == 6:
            # Vertically disposed canvas
            layout = QVBoxLayout()
            self.canvas_right.setTransform(QTransform().scale(-1, 1))  # Mirror right canvas vertically        
        central_widget.setLayout(layout)

        layout.addWidget(self.canvas_left)
        layout.addWidget(self.canvas_right)

        return None

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
        """Replicates canvas Z changes in the QGIS status bar."""
        self.z_label.setText(f"Zbase={self._z_proj_plane:.1f} Zcurs={self._z_cursor:.1f}")
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
        if self.canvas_left:
            # QgsMessageLog.logMessage(f"[DEBUG] <sync_canvases_repaint> Refreshing LEFT canvas", "SWM-3D", Qgis.Info)
            self.canvas_left.setExtent(self.qgis_main_canvas.extent())
            # self.canvas_left.sync_layers()
            self.canvas_left.refresh()
        if self.canvas_right:
            # QgsMessageLog.logMessage(f"[DEBUG] <sync_canvases_repaint> Refreshing RIGHT canvas", "SWM-3D", Qgis.Info)
            self.canvas_right.setExtent(self.qgis_main_canvas.extent())
            # self.canvas_right.sync_layers()
            self.canvas_right.refresh()

    def _sync_canvases_layers(self):
        """
        Propagate layer changes (add/remove/reorder) to plugin canvases.
        """
        if self.canvas_left:
            self.canvas_left.sync_layers()
        if self.canvas_right:
            self.canvas_right.sync_layers()

    def eventFilter(self, obj, event):
        """
         Cursor entry control.
         Mouse wheel Z control (ALT + wheel).
        """
        try:
            # Cursor entry control in the photogrammetric window.
            is_stereo_object = False
            if isinstance(obj, QWidget):
                is_stereo_object = (obj is self) or (obj.window() is self)

            if is_stereo_object and event.type() in (QEvent.Type.Enter, QEvent.Type.MouseMove, QEvent.Type.HoverMove):
                self._keep_cursor_in_main_qgis_window()
                return True

            # Wheel-based Z control
            if event.type() == QEvent.Type.Wheel:
                modifiers = QApplication.keyboardModifiers()
                if not (modifiers & Qt.KeyboardModifier.AltModifier):
                    return False
                    
                delta = -event.angleDelta().x() / 120.
                if modifiers & Qt.KeyboardModifier.ControlModifier:
                    delta /= 10.
                elif modifiers & Qt.KeyboardModifier.ShiftModifier:
                    delta *= 10.
                    
                old_z = self.z_cursor
                self.z_cursor = round(self.z_cursor + delta, 1)
                
                QgsMessageLog.logMessage(f"Z changed: {old_z} → {self._z_cursor}", "SWM-3D", Qgis.Info)
                return True
                
            return False
        except:
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
        if not self.isVisible():
            return
        request_url = reply.request().url().toString()

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
            if not self._has_received_swm_reply:
                self._has_received_swm_reply = True
                # This timer waits for the next Qt event-loop cycle
                QTimer.singleShot(0, self.showFullScreen)  # Restores with ESC in keyPressEvent
        else:
            if self.canvas_right:
                self.canvas_right.update_data_from_wms_header(reply)

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
