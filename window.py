
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

from qgis.core import QgsMessageLog, Qgis  # para mensajes de depuración.
from qgis.core import QgsNetworkAccessManager
from qgis.PyQt.QtWidgets import QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QStackedLayout
from qgis.PyQt.QtWidgets import QInputDialog, QMessageBox, QLabel, QApplication
from qgis.PyQt.QtGui import QTransform, QGuiApplication
from qgis.PyQt.QtCore import Qt, QEvent, QTimer

import re

# librerías SWM
from .canvas import QgsSgdSwmCanvas


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
        # Para maximizar cuando se haya recibido una respuesta válida
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

        # Z global compartido siempre asignar con @z_cursor.setter para actualizar todo
        self._z_proj_plane = 0.0
        self.z_cursor = 0.0
        # Instalar filtro de eventos GLOBAL
        QApplication.instance().installEventFilter(self)

        # Capturar eventos del canvas principal qgis
        self.iface.mapCanvas().xyCoordinates.connect(self._sync_canvases_cursor)      # ratón
        self.iface.mapCanvas().layersChanged.connect(self._sync_canvases_layers)      # nuevas capas
        self.iface.mapCanvas().extentsChanged.connect(self._sync_canvases_repaint)    # zoom y desplazamiento
        
        # Timer para monitorear cambios en map canvas items
        self.canvas_items_timer = QTimer()
        self.canvas_items_timer.timeout.connect(self._check_canvas_items_changes)
        self.canvas_items_timer.start(200)  # Verificar cada 200ms
        self._last_canvas_items_count = 0

        # Network Manager WMS
        # https://chat.deepseek.com/a/chat/s/5dc872fa-208d-458c-836b-9199dcc3a37c
        # self.network_manager = QNetworkAccessManager()
        self.network_manager = QgsNetworkAccessManager.instance()      
        if self.network_manager:
            self.network_manager.finished.connect(self.network_reply_handle)

    def showEvent(self, event):
        """
        Método virtual heredado de QWidget / QMainWindow
        """
        super().showEvent(event)
        # Sincronización inicial tras mostrarse la ventana
        if self.canvas_left:
            self.canvas_left.setExtent(self.qgis_main_canvas.extent())
            self.canvas_left.sync_layers()
        if self.canvas_right:
            self.canvas_right.setExtent(self.qgis_main_canvas.extent())
            self.canvas_right.sync_layers()
        # Quitar a ver si es necesario
        if self.canvas_left:
            self.canvas_left.refresh()
        if self.canvas_right:
            self.canvas_right.refresh()

    def closeEvent(self, event):
        # Limpiar timer de monitoreo de canvas items
        if hasattr(self, 'canvas_items_timer'):
            self.canvas_items_timer.stop()
            
        # Limpiar sincronización en los canvas secundarios
        if self.canvas_left:
            self.canvas_left.cleanup_canvas_items_sync()
        if self.canvas_right:
            self.canvas_right.cleanup_canvas_items_sync()
            
        self.iface.mainWindow().statusBar().removeWidget(self.z_label)
        super().closeEvent(event)

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
            layout.setStackingMode(QStackedLayout.StackAll) # Stacked layout to overlay canvases    
            # VER_0.5
            # nada
            # VER_1.0
            layout.setCurrentIndex(1)  # Asegura que el derecho esté arriba
        elif self.stereo_id <= 5:
            # Horizontally disposed canvas
            layout = QHBoxLayout()
            if self.stereo_id == 5:
                self.canvas_right.setTransform(QTransform().scale(-1, 1))  # Mirror right canvas horizontally
        elif self.stereo_id == 6:
            # Vertically disposed canvas
            layout = QVBoxLayout()
            # VER_0.5
            # self.canvas_right.setTransform(QTransform().scale(1, -1))  # Mirror right canvas vertically
            # VER1.0
            self.canvas_right.setTransform(QTransform().scale(-1, 1))  # Mirror right canvas vertically        
        central_widget.setLayout(layout)  # ¿quitado en VER_1.0?

        layout.addWidget(self.canvas_left)
        layout.addWidget(self.canvas_right)

        # Para conectar el canvas con el StatusBar
        self.canvas_left.z_changed_callback = self._update_z_label
        self.canvas_right.z_changed_callback = self._update_z_label

        return None

    # La Z, cambios con la rueda del ratón
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
        self._update_z_label()

    def _set_projection_plane_z(self, z):
        """
        Set the projection plane Z coming from the WMS headers.
        This defines the reference Z used by the canvas.
        """
        self._z_proj_plane = z
        self.z_cursor = z  # @z_cursor.setter ya envía al canvas el mensaje de actualizar cursor

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
        Al hacer zoom o pan en el canvas principal, por un lado
        1) se ha debido llamar al SWM para actualizar la imagen y network_reply_handle se ha tenido que llamar
        2) Eso habrá cargado nuevas proyecciones con actualización del Geometry Generator de las capas con Z y la capa SWM
        3) Pero falta el refresco de todo para que se repinte.
        """
        if self.canvas_left:
            QgsMessageLog.logMessage(f"[DEBUG] <sync_canvases_repaint> Refrescando canvas IZDO", "SWM-3D", Qgis.Info)
            self.canvas_left.setExtent(self.qgis_main_canvas.extent())
            # self.canvas_left.sync_layers()
            self.canvas_left.refresh()
        if self.canvas_right:
            QgsMessageLog.logMessage(f"[DEBUG] <sync_canvases_repaint> Refrescando canvas DCHO", "SWM-3D", Qgis.Info)
            self.canvas_right.setExtent(self.qgis_main_canvas.extent())
            # self.canvas_right.sync_layers()
            self.canvas_right.refresh()

    def _sync_canvases_layers(self):
        """
        Propaga cambios en las capas (add/remove/reorder) a los canvases del plugin.
        """
        if self.canvas_left:
            self.canvas_left.sync_layers()
        if self.canvas_right:
            self.canvas_right.sync_layers()

    def _check_canvas_items_changes(self):
        """
        Verifica cambios en los map canvas items del canvas principal y 
        fuerza la sincronización en los canvas secundarios.
        """
        try:
            if not self.iface or not self.iface.mapCanvas():
                return
                
            # Contar items actuales en el canvas principal
            current_count = 0
            main_canvas = self.iface.mapCanvas()
            if hasattr(main_canvas, 'scene') and main_canvas.scene():
                for item in main_canvas.scene().items():
                    if hasattr(item, '__class__') and 'MapCanvasItem' in str(item.__class__):
                        current_count += 1
            
            # Si hay cambios, forzar sincronización
            if current_count != self._last_canvas_items_count:
                self._last_canvas_items_count = current_count
                self._sync_canvases_items()
                
        except Exception as e:
            QgsMessageLog.logMessage(f"Error verificando cambios en canvas items: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)

    def _sync_canvases_items(self):
        """
        Fuerza la sincronización de map canvas items en los canvas secundarios.
        """
        if self.canvas_left:
            self.canvas_left.force_sync_canvas_items()
        if self.canvas_right:
            self.canvas_right.force_sync_canvas_items()

    def set_canvas_items_sync_enabled(self, enabled: bool):
        """
        Habilita o deshabilita la sincronización automática de map canvas items.
        Método público para controlar la funcionalidad desde el exterior.
        """
        if enabled:
            # Reactivar el timer de monitoreo
            if hasattr(self, 'canvas_items_timer'):
                self.canvas_items_timer.start(200)
            # Habilitar sincronización en los canvas
            if self.canvas_left:
                self.canvas_left.set_canvas_items_sync_enabled(True)
            if self.canvas_right:
                self.canvas_right.set_canvas_items_sync_enabled(True)
        else:
            # Detener el timer de monitoreo
            if hasattr(self, 'canvas_items_timer'):
                self.canvas_items_timer.stop()
            # Deshabilitar sincronización en los canvas
            if self.canvas_left:
                self.canvas_left.set_canvas_items_sync_enabled(False)
            if self.canvas_right:
                self.canvas_right.set_canvas_items_sync_enabled(False)

    def eventFilter(self, obj, event):
        # Capture wheel events globally
        if event.type() == QEvent.Type.Wheel:
            # Check if the Alt key is pressed using the keyboard state
            modifiers = QApplication.keyboardModifiers()
            # Use ALT + wheel to control Z
            if not (modifiers & Qt.KeyboardModifier.AltModifier):
                return False
            delta = -event.angleDelta().x() / 120.  # Increment Z by wheel step 1 m. ¿Debería ser y()??
            # Modifiers for precision
            if modifiers & Qt.KeyboardModifier.ControlModifier:
                delta /= 10.    # Increment Z by wheel step 0.1 m.
            elif modifiers & Qt.KeyboardModifier.ShiftModifier:
                delta *= 10.    # Increment Z by wheel step 10 m.
            self.z_cursor += delta  # Adjust Z value
            # QgsMessageLog.logMessage(f"[DEBUG] New Z value: {self.z_cursor}", "SWM_3D", Qgis.Info)

            # Notificar a TODOS los canvases Ya lo hace el @z_cursor.setter
            # self.canvas_left.update_cursor()
            # self.canvas_right.update_cursor()
            # self._update_z_label()
            return True  # Event handled, prevent further propagation
        return False

    def _update_z_label(self):
        """ Para reproducir en StatusBar los cambios en Z (que está en el canvas) """
        self.z_label.setText(f"Zbase={self._z_proj_plane:.1f} Zcurs={self._z_cursor:.1f}")
        # self.z_label.setText(f"Z={z: .1f}")

    def network_reply_handle(self, reply):
        """
        Handler for WMS network replies. Checks if the reply is from a SWM plugin layer by looking for specific headers.
        If it is a SWM reply, reads the projection plane Z and transformation parameters from the headers, 
        updates the canvas state, and triggers a refresh to apply the new transformations.
        A través de self.network_manager.finished.connect(self.network_reply_handle)
        este método se conecta al signal finished() del QgsNetworkAccessManager, que se emite cada vez que:
        se recibe una respuesta del servidor . 
        """
        if not self.isVisible():
            return
        request_url = reply.request().url().toString()
        # QgsMessageLog.logMessage(f"[DEBUG] <network_reply_handler> Lanzando", "SWM-3D", Qgis.Info)

        # Vamos a chequear si la respuesta viene de SWM y es correcta
        # La petición que proviene de la pantalla principal de QGIS vienen como PhotoRight o PhotoLeft.
        # Aprovechamos esa diferencia en MAY/MIN para filtrarla y no procesarla
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
                # Este Timer espera al siguiente ciclo del bucle de eventos Qt
                QTimer.singleShot(0, self.showFullScreen)  # Se minimiza con ESC en keyPressEvent
        else:
            if self.canvas_right:
                self.canvas_right.update_data_from_wms_header(reply)

    def keyPressEvent(self, event):
        """
        Sólo para poder salir del modo FullScreen
        """
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

    # def trigger_sync_renderer_layerz(self, layer_name):
    #     """Trigger the sync_layers function when a layer's renderer changes."""
    #     self.canvas_left.sync_renderer_layerz_changed(layer_name)
    #     self.canvas_right.sync_renderer_layerz_changed(layer_name)
