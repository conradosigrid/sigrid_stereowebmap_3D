"""
canvas.py

Custom QGIS map canvas for the Sigrid SWM plugin.

This module implements QgsSgdSwmCanvas, a specialized map canvas used for
stereoscopic visualization. The canvas mirrors the main QGIS canvas and adds:

- Support for left/right stereoscopic views
- Stereo rendering filters (anaglyph, interlaced, mirror, etc.)
- Synchronization of extent, layers and cursor with the main canvas
- Application of perspective transformations to Z-enabled layers
  using QGIS Geometry Generator expressions

The canvas does not handle network requests, WMS headers, or mathematical
parsing of transformations. Those responsibilities belong to the window
controller and expression functions.

QGIS Main Canvas
  ├── ratón (fuente)
  ├── zoom
  ├── capas
  └── señales
        ↓
QgsSgdSwmCanvas (plugin)
  ├── Z (estado interno de vista)
  ├── transformación
  ├── cursor proyectado
  └── render
"""
from qgis.core import QgsMessageLog, Qgis  # para mensajes de depuración.
from qgis.gui import QgsMapCanvas, QgsVertexMarker, QgsRubberBand
from qgis.core import QgsWkbTypes, QgsGeometry, QgsRasterLayer, QgsVectorLayer, QgsPoint
from qgis.core import QgsSymbol, QgsSingleSymbolRenderer, QgsGeometryGeneratorSymbolLayer
from qgis.PyQt.QtGui import QColor, QWheelEvent, QImage, QPainter
from qgis.PyQt.QtCore import QEvent, Qt, QObject
from typing import Optional, Any

import re
import numpy as np
# librerías SWM
from .transform import TrfWldToPrjPln
from .utils import is_sgd_swm_layer, is_z_layer


# Class Sigrid Swm Canvas esclavo (espejo) transformado del canvas principal de QGIS
class QgsSgdSwmCanvas(QgsMapCanvas):
    FILTER_NONE = 0
    FILTER_RED = 1
    FILTER_CYAN = 2
    FILTER_EVEN = 3
    FILTER_ODD = 4

    def __init__(self, is_left: bool, qgis_main_canvas, filter: int = FILTER_NONE, parent: Optional[Any] = None):
        super(QgsSgdSwmCanvas, self).__init__(parent)
 
        self.parent = parent
        self.qgis_main_canvas = qgis_main_canvas
        self.is_left = is_left
        self.filter = filter

        # Transformation world to projection plane
        self.trf_wld2prp = None
        # VER_0.5
        # self.filtered_image = None  # Aquí guardaremos la imagen filtrada
        # VER_1.0
        # nada

        # Cursor marker
        self.cursor_marker = QgsVertexMarker(self)
        self.cursor_marker.setColor(QColor(Qt.GlobalColor.black))
        self.cursor_marker.setIconSize(10)
        self.cursor_marker.setIconType(QgsVertexMarker.ICON_CROSS)
        self.cursor_marker.setPenWidth(3)
        # Pruebas con cursor bitmap. 
        # # Cursor marker - synchronized with main canvas using actual cursor bitmap
        # self.cursor_marker = None  # Will be created dynamically
        # self.cursor_pixmap_item = None  # QGraphicsPixmapItem for custom cursor
        # self.current_cursor_shape = None  # Cache current cursor
        # self._init_cursor_marker()

        self.layer_swm = None
        self.layers_z = []
        self.limits = None

        self.setCanvasColor(QColor(0, 0, 0, 0))  # QColor(Qt.GlobalColor.transparent)
        # Conectar señales que deberían triggerear repintado
        # self.qgis_main_canvas.extentsChanged.connect(self.force_repaint)
        # self.qgis_main_canvas.layersChanged.connect(self.force_repaint)
        
        # Synchronize events
        # Para cursor bitmap. 
        # self.qgis_main_canvas.mapToolSet.connect(self.sync_cursor_style)
        # self.qgis_main_canvas.xyCoordinates.connect(self.sync_cursor) en window.py ahora
        # self.qgis_main_canvas.layersChanged.connect(self.sync_layers) 
        # self.qgis_main_canvas.extentsChanged.connect(self.sync_zoom)
        # self.mapCanvasRefreshed.connect(self.refresh_finnished)
        # self.renderComplete.connect(self.render_complete)

    # ============================================================================
    # == Cursor en el canvas estéreo ==
    # ============================================================================
    def update_cursor(self):
        """
        Reprojects cursor using current XYZ value and updates its position.
        """
        if not self.isVisible():
            return

        # Get cursor position in main canvas coordinates and current Z
        pos = self.qgis_main_canvas.mouseLastXY()
        z = self.parent.z_cursor if self.parent else 0
        point_xy = self.qgis_main_canvas.getCoordinateTransform().toMapCoordinates(pos)
        
        # Calculate projected position
        if self.trf_wld2prp:
            pnt_wrl = QgsPoint(point_xy.x(), point_xy.y(), z)
            pnt_prj = self.trf_wld2prp.execute_wrl2prp(pnt_wrl)
            if pnt_prj:
                self.cursor_marker.setCenter(pnt_prj)
                self.cursor_marker.show()
                return

        self.cursor_marker.setCenter(point_xy)
        self.cursor_marker.show()

    def wheelEvent(self, event: QWheelEvent):  # type: ignore[override]
        """
        Ignore mouse wheel events on the stereo canvas. Wheel interaction is handled globally by the main window.
        """
        event.accept()   # consumir el evento
        return           # no llamar a super()

    # ============================================================================
    # == Fin del cursor en el canvas estéreo ==
    # ============================================================================

    def paintEvent(self, e):

        if self.filter == self.FILTER_NONE:  # ver_0.5:  or self.filtered_image is None:
            super().paintEvent(e)
        else:
            # VER_0.5
            # painter = QPainter(self.viewport())
            # if not self.is_left:
            #     painter.setCompositionMode(QPainter.CompositionMode_Plus)
            # painter.drawImage(0, 0, self.filtered_image)
            # painter.end()
            # VER_1.0
            # Render base content
            buffer = QImage(self.size(), QImage.Format_ARGB32)
            buffer.fill(QColor(0, 0, 0, 0))  # QColor(Qt.GlobalColor.transparent)
            super().render(QPainter(buffer))
            filtered = self.apply_filter(buffer)  # Apply filter to the buffer
            painter = QPainter(self.viewport())  # Paint viewport
            if self.parent and self.parent.stereo_id < 3 and self.is_left:  
                # Overlayer stereoscopic mode and right canvas. Add pixels filteres images
                # left always last?
                # &&&& painter.setCompositionMode(QPainter.CompositionMode_Plus)
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)               
            painter.drawImage(0, 0, filtered)
            painter.end()        
            self.viewport().update()

    def force_repaint(self):
        """
        Force a lightweight repaint of the viewport.
        Used only for visual filters (stereo, anaglyph, interlaced),
        without triggering a full map refresh.
        """
        if self.filter != self.FILTER_NONE:
            self.viewport().update()

    def apply_filter(self, image):
        """Ultra-fast version with precise results"""
        if self.filter == self.FILTER_NONE:
            return image

        # Convert to ARGB32 (ensures alpha channel even if the source is RGB32)
        # image = image.convertToFormat(QImage.Format_ARGB32)
        result = QImage(image.size(), QImage.Format_ARGB32)
        
        # Direct access to bits (fast)
        ptr = image.bits()
        ptr.setsize(image.byteCount())
        arr = np.frombuffer(ptr, np.uint8).reshape(image.height(), image.width(), 4)

        if self.filter == self.FILTER_RED:
            # Correct red filter: R = original, G=B=0, Alpha intact
            arr[:, :, 0] = 0  # G channel to 0
            arr[:, :, 1] = 0  # B channel to 0
        elif self.filter == self.FILTER_CYAN:
            # Correct cyan filter: G+B = original, R=0, Alpha intact
            arr[:, :, 2] = 0  # R channel to 0
        elif self.filter in [self.FILTER_EVEN, self.FILTER_ODD]:
            # Alternate lines (optimized version)
            mask = np.zeros_like(arr)
            lines = range(0, image.height(), 2) if self.filter == self.FILTER_EVEN else range(1, image.height(), 2)
            mask[lines, :, :] = arr[lines, :, :]
            arr = mask
 
        # Convert back to QImage
        result = QImage(arr.data, image.width(), image.height(), image.bytesPerLine(), QImage.Format_ARGB32)
        return result.copy()  # Important: .copy() to avoid memory issues 
    
    def refresh_finnished(self):
        # Draws a rectangle corresponding to the main canvas extent in this canvas
        # TODO: Fails. Rubber band?
        if self.parent and not self.parent.isVisible():
            return
        # print("refresh_finnished", self.is_left)

    def render_complete(self):
        # Draws a rectangle corresponding to the main canvas extent in this canvas
        # TODO: Fails. Rubber band?

        if self.parent and not self.parent.isVisible():
            return

        extent = self.qgis_main_canvas.extent()  # Get the extent of the main canvas
        if self.limits:
            self.scene().removeItem(self.limits)
        else:
            self.limits = QgsRubberBand(self, QgsWkbTypes.PolygonGeometry)
            border_color = QColor(200, 200, 200)  # Gray color for the border
            self.limits.setColor(border_color)
            self.limits.setWidth(1)
            self.limits.setFillColor(QColor(0, 0, 0, 0))  # QColor(Qt.GlobalColor.transparent)
        self.limits.setToGeometry(QgsGeometry.fromRect(extent), None)
        self.limits.show()

        # VER_0.5
        # Aplica el filtro solo cuando termina el renderizado
        # if self.filter != self.FILTER_NONE:
        #     # Captura el contenido del canvas como imagen
        #    buffer = QImage(self.size(), QImage.Format_ARGB32)
        #    buffer.fill(Qt.transparent)
        #    super().render(QPainter(buffer))
        #    self.filtered_image = self.apply_filter(buffer)
        #    self.viewport().update()
        # else:
        #    self.filtered_image = None
        # VER_1.0
        # movido a def paintEvent(self, event):

    def sync_cursor(self, point_xy):
        """
        Evento de movimiento del cursor enviado por el padre
        Updates the position of the cursor marker in the canvas (based on the cursor position AND Z in the main canvas).
        Coge los valores XYZ más actuales que guarda el padre
        """
        # Just update position, don't sync properties on every mouse move (too expensive)
        self.update_cursor()

    def sync_layers(self):
        # Get the layers from the main QGIS canvas
        layers_main = self.qgis_main_canvas.layers()
        layers_self = []  # Get the layers from this canvas
        self.layer_swm = None 
        self.layers_z = []

        # Loop through the layers to short them properly in ONE SWM layer, several vector layers with Z (with geometry generator) 
        # and other layers without Z (copied as they are)
        for layer_main in layers_main:
            if is_sgd_swm_layer(layer_main):
                # TODO: Assign to layer default CRS service
                # Set own styles for canvas URL
                # Styles in uppercase <==> problems :-(
                if self.layer_swm:
                    # Only first swm layer is used
                    continue
                sigrid_layer_main_url = layer_main.source()
                style_value = 'PHOTOLEFT' if self.is_left else 'PHOTORIGHT'
                sigrid_layer_self_url = re.sub(r'styles(=[^&]*)?', f'styles={style_value}', sigrid_layer_main_url, flags=re.IGNORECASE)
                # https://gis.stackexchange.com/questions/467847/creating-qgsrasterlayer-from-wms-layer-using-pyqgis-in-qgis-3-28
                # Aquí se provoca una llamada inicial al servidor con (GETCAPABILITIES)
                self.layer_swm = QgsRasterLayer(sigrid_layer_self_url, style_value, 'wms')            
                layers_self.append(self.layer_swm)  
            elif is_z_layer(layer_main):
                # Layer has Z values. Must apply Geometry Generator
                # Copy layer_main to apply Geometry Generator. Ensure the CRS and other properties are the same
                # 1) Crear una vista lógica independiente, perfecta para el canvas secundario.
                layer_copy = QgsVectorLayer(layer_main.source(), layer_main.name(), layer_main.providerType())
                # Update (only once: is_left). No sé si es ecesario. Desactivado de momento
                # if self.is_left:
                #     layer_main.rendererChanged.connect(lambda: self.parent.trigger_sync_renderer_layerz(layer_copy.name()))
                # 2) copiamos todos los estilos de la capa original
                symbol = layer_main.renderer().symbol().clone()
                if symbol is None:
                    QgsMessageLog.logMessage(f"SYNC_LAYER Capa: {layer_main.name()}-{'LEFT' if self.is_left else 'RIGHT'}. "
                                             f"NO SE PUEDE interpretar el estilo.", "SWM-3D", Qgis.Error)
                    continue
                # 3) Creamos un Geometry Generator inicial inútil, porque la perspectiva y proyeccion no la conocemos aún
                # Crearemos luego una nueva expresión cuando tengamos la transformación accesible.
                # Esta es una dummy expression, que pinta la capa en 2D sin transformar (también PINTA los PUNTOS, pero sin Z)
                symbol_layer = QgsGeometryGeneratorSymbolLayer.create({'geometryModifier': '$geometry'})
                if symbol_layer is None:
                    continue
                symbol_layer.setSubSymbol(symbol)
                # 4) Sustituir el symbol layer (capa 0)
                final_symbol = QgsSymbol.defaultSymbol(layer_main.geometryType())
                if final_symbol is None:
                    continue
                final_symbol.changeSymbolLayer(0, symbol_layer) 
                # 5) Asignar el renderer
                renderer = QgsSingleSymbolRenderer(final_symbol)
                layer_copy.setRenderer(renderer) 
                if layer_main.hasScaleBasedVisibility():
                    # TODO: With glasses is not working. Real scale in glasses?
                    layer_copy.setScaleBasedVisibility(True)
                    layer_copy.setMinimumScale(layer_main.minimumScale())
                    layer_copy.setMaximumScale(layer_main.maximumScale())
                self.layers_z.append(layer_copy)
                layers_self.append(layer_copy) 
                QgsMessageLog.logMessage(f"SYNC_LAYER Capa: {layer_main.name()}-{'LEFT' if self.is_left else 'RIGHT'}.", 
                                         "SWM-3D", Qgis.Info)
            else:
                # ¿Qué hacemos con otra capa que no es SWM ni tiene Z?
                layers_self.append(layer_main)

        self.setLayers(layers_self)
        # self.refresh()  # Parece innecesario

    # Lo desactivo porque no sé si es necesario
    # def sync_renderer_layerz_changed(self, layer_name):
    #     """Synchronize the renderer when symbology of layer with Z changes."""
    #     layer_self = next((layer for layer in self.layers() if layer.name() == layer_name), None) 
    #     layer_main = next((layer for layer in self.qgis_main_canvas.layers() if layer.name() == layer_name), None)        
    #     if (not layer_self) or (not layer_main): return False
    #     try:
    #         symbol_new = layer_main.renderer().symbol()
    #         layer_self.renderer().symbol().symbolLayer(0).setSubSymbol(symbol_new.clone())
    #     except Exception as e:
    #         QgsMessageLog.logMessage(f"Error syncing renderer for layer {layer_name}: {e}", "SWM-3D", Qgis.Warning)
    #         return False
    #     layer_self.triggerRepaint()
    #     return True

    def update_data_from_wms_header(self, reply):
        """
        Update photogrammetric transformation parameters from a SWM WMS reply
        and store them as layer custom properties so they can be consumed
        by Geometry Generator expressions.
        Esta función es fundamental porque es la que deja el Geometry Generator de las capas con Z 
        preparado para aplicar la transformación fotogramétrica.
        IMPORTANTE: si se llama a sync_layers sin pasar luego por aquí, El Geometry Generator se 
        queda con la transformación vacía y no pintará nada en el canvas secundario estereoscópico.
        """
        # TODO: Get rotation from the reply headers
        # Init transformation
        self.trf_wld2prp = TrfWldToPrjPln()
            
        # Get transform perspective point Z to photo from the reply headers
        txt_trf_wrl2pht = reply.rawHeader(b'SIGRID_PhtTransWorld3DToPhoto').data().decode('utf-8')
        self.trf_wld2prp.read_perspective(txt_trf_wrl2pht)

        # Get transform photo to projection plane from the reply headers
        txt_trf_pht2prp = reply.rawHeader(b'SIGRID_PhtTransPhotoToCanvas').data().decode('utf-8')
        self.trf_wld2prp.read_projective(txt_trf_pht2prp)

        # Update Geometry Generator for Z layers. Ahora por fin sabemos la transformación, así que ya 
        # podemos actualizar las capas con Z (si las hay) para que apliquen la transformación en sus geometrías.
        for layer in self.layers():
            if is_z_layer(layer):
                layer.setCustomProperty("swm_trf_wrl2pht", txt_trf_wrl2pht)
                layer.setCustomProperty("swm_trf_pht2prp", txt_trf_pht2prp)
                # Hay que cambiar el GeometryGenerator ahora que ya tenemos la transformación 
                side = 'left' if self.is_left else 'right'
                expression = (f"perspective_swm_transform($geometry,'{side}','{self.trf_wld2prp.txt_perspective}','{self.trf_wld2prp.txt_projective}')")
                symbol_layer = layer.renderer().symbol().symbolLayer(0)
                if not isinstance(symbol_layer, QgsGeometryGeneratorSymbolLayer):
                    QgsMessageLog.logMessage(f"Tipo SymbolLayer inesperado en capa {layer.name()}: {type(symbol_layer)}", "SWM-3D", Qgis.Warning)
                    continue
                symbol_layer.setGeometryExpression(expression)

                QgsMessageLog.logMessage(f"UPDATE_SWM_HEADER Capa: {layer.name()}-{'LEFT' if self.is_left else 'RIGHT'}.", 
                                         "SWM-3D", Qgis.Info)   
        self.render_complete()
        # self.refresh()  # crea bucle infinito
