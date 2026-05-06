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
from qgis.gui import QgsMapCanvas, QgsVertexMarker, QgsRubberBand, QgsMapCanvasItem
from qgis.core import QgsWkbTypes, QgsGeometry, QgsRasterLayer, QgsVectorLayer, QgsPoint
from qgis.core import QgsSymbol, QgsSingleSymbolRenderer, QgsGeometryGeneratorSymbolLayer
from qgis.PyQt.QtGui import QColor, QWheelEvent, QImage, QPainter
from qgis.PyQt.QtCore import QEvent, Qt, QObject, QTimer
from typing import Optional, Any, Dict, List

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

        # Cursor marker (debe crearse antes de la sincronización de items)
        self.cursor_marker = QgsVertexMarker(self)
        self.cursor_marker.setColor(QColor(Qt.GlobalColor.black))
        self.cursor_marker.setIconSize(10)
        self.cursor_marker.setIconType(QgsVertexMarker.ICON_CROSS)
        self.cursor_marker.setPenWidth(3)

        # Map canvas items synchronization (después de crear cursor_marker)
        self.synced_items: Dict[QgsMapCanvasItem, QgsMapCanvasItem] = {}  # main_item -> synced_item
        self.vertex_z_cache: Dict[QgsMapCanvasItem, Dict[str, float]] = {}  # rubber_band -> {xy_key: z_value}
        self.sync_timer = QTimer()
        self.sync_timer.timeout.connect(self._sync_canvas_items)
        self.sync_timer.setSingleShot(True)
        self._setup_canvas_items_sync()

        self.layer_swm = None
        self.layers_z = []
        self.limits = None
        self.z_text = ""  # Texto del cursor Z

        self.setCanvasColor(QColor(0, 0, 0, 0))  # QColor(Qt.GlobalColor.transparent)

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

    # ============================================================================
    # == Sincronización de Map Canvas Items ==
    # ============================================================================

    def _setup_canvas_items_sync(self):
        """
        Configura la sincronización automática de map canvas items del canvas principal.
        """
        # Realizar sincronización inicial
        self._sync_canvas_items()
        
        # Programar sincronización periódica (cada 500ms cuando no hay cambios activos)
        self.sync_timer.start(500)

    def _sync_canvas_items(self):
        """
        Sincroniza todos los map canvas items del canvas principal con este canvas.
        """
        try:
            if not self.qgis_main_canvas:
                return
                
            main_items = self._get_canvas_items(self.qgis_main_canvas)
            current_main_items = set(main_items)
            synced_main_items = set(self.synced_items.keys())
            
            # Eliminar items que ya no existen en el canvas principal
            items_to_remove = synced_main_items - current_main_items
            for main_item in items_to_remove:
                if main_item in self.synced_items:
                    synced_item = self.synced_items[main_item]
                    if hasattr(synced_item, 'hide'):
                        synced_item.hide()
                    # Eliminar del canvas de forma segura
                    self._safe_remove_item(synced_item)
                    del self.synced_items[main_item]
                    
                    # Limpiar cache de Z para rubber bands removidos
                    if main_item in self.vertex_z_cache:
                        del self.vertex_z_cache[main_item]
            
            # Añadir o actualizar items existentes
            for main_item in main_items:
                if main_item not in self.synced_items:
                    # Crear nuevo item sincronizado
                    synced_item = self._create_synced_item(main_item)
                    if synced_item:
                        self.synced_items[main_item] = synced_item
                else:
                    # Actualizar item existente
                    self._update_synced_item(main_item, self.synced_items[main_item])
                    
        except Exception as e:
            QgsMessageLog.logMessage(f"Error sincronizando map canvas items: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)
        finally:
            # Reprogramar próxima sincronización
            if not self.sync_timer.isActive():
                self.sync_timer.start(500)

    def _get_canvas_items(self, canvas) -> List[QgsMapCanvasItem]:
        """
        Obtiene todos los map canvas items de un canvas.
        """
        items = []
        try:
            if hasattr(canvas, 'scene') and canvas.scene():
                for item in canvas.scene().items():
                    # Verificar que sea un QgsMapCanvasItem y excluir nuestro cursor marker si existe
                    if isinstance(item, QgsMapCanvasItem):
                        # Excluir nuestro propio cursor marker para evitar recursión
                        if hasattr(self, 'cursor_marker') and item == self.cursor_marker:
                            continue
                        items.append(item)
        except Exception as e:
            QgsMessageLog.logMessage(f"Error obteniendo canvas items: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)
        return items

    def _create_synced_item(self, main_item: QgsMapCanvasItem) -> Optional[QgsMapCanvasItem]:
        """
        Crea una copia sincronizada de un map canvas item del canvas principal.
        """
        try:
            synced_item = None
            
            if isinstance(main_item, QgsVertexMarker):
                synced_item = QgsVertexMarker(self)
                self._sync_vertex_marker_properties(main_item, synced_item)
                
            elif isinstance(main_item, QgsRubberBand):
                # Obtener tipo de geometría del rubber band original
                geom_type = QgsWkbTypes.PolygonGeometry
                if hasattr(main_item, 'geometryType'):
                    geom_type = main_item.geometryType()
                    
                synced_item = QgsRubberBand(self, geom_type)
                self._sync_rubber_band_properties(main_item, synced_item)
            
            # Añadir más tipos de items según necesidades
            # elif isinstance(main_item, OtherMapCanvasItemType):
            #     synced_item = self._create_other_item_type(main_item)
                
            if synced_item:
                synced_item.show()
                
            return synced_item
            
        except Exception as e:
            QgsMessageLog.logMessage(f"Error creando item sincronizado: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)
            return None

    def _update_synced_item(self, main_item: QgsMapCanvasItem, synced_item: QgsMapCanvasItem):
        """
        Actualiza las propiedades de un item sincronizado basándose en el item principal.
        """
        try:
            if isinstance(main_item, QgsVertexMarker) and isinstance(synced_item, QgsVertexMarker):
                self._sync_vertex_marker_properties(main_item, synced_item)
                
            elif isinstance(main_item, QgsRubberBand) and isinstance(synced_item, QgsRubberBand):
                self._sync_rubber_band_properties(main_item, synced_item)
                
            # Actualizar visibilidad
            if hasattr(main_item, 'isVisible') and hasattr(synced_item, 'setVisible'):
                synced_item.setVisible(main_item.isVisible())
                
        except Exception as e:
            QgsMessageLog.logMessage(f"Error actualizando item sincronizado: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)

    def _sync_vertex_marker_properties(self, source: QgsVertexMarker, target: QgsVertexMarker):
        """
        Sincroniza las propiedades de un QgsVertexMarker.
        """
        try:
            # Copiar propiedades básicas de forma segura
            # Verificar existencia de métodos getter antes de usarlos
            if hasattr(source, 'color'):
                target.setColor(source.color())
            
            # Para iconSize, iconType y penWidth, algunos getters podrían no estar disponibles
            # En caso de no poder obtener el valor, usar valores por defecto razonables
            try:
                if hasattr(source, 'iconSize'):
                    target.setIconSize(source.iconSize())
                else:
                    target.setIconSize(10)  # Valor por defecto
            except AttributeError:
                target.setIconSize(10)
                
            try:
                if hasattr(source, 'iconType'):
                    target.setIconType(source.iconType())
                else:
                    target.setIconType(QgsVertexMarker.ICON_CROSS)  # Valor por defecto
            except AttributeError:
                target.setIconType(QgsVertexMarker.ICON_CROSS)
                
            try:
                if hasattr(source, 'penWidth'):
                    target.setPenWidth(source.penWidth())
                else:
                    target.setPenWidth(3)  # Valor por defecto
            except AttributeError:
                target.setPenWidth(3)
            
            # Copiar y transformar posición
            center = source.center()
            if center and self.trf_wld2prp:
                # Aplicar transformación 3D si está disponible
                z = self.parent.z_cursor if self.parent else 0
                pnt_wrl = QgsPoint(center.x(), center.y(), z)
                pnt_prj = self.trf_wld2prp.execute_wrl2prp(pnt_wrl)
                if pnt_prj:
                    target.setCenter(pnt_prj)
                else:
                    target.setCenter(center)
            else:
                target.setCenter(center)
                
        except Exception as e:
            QgsMessageLog.logMessage(f"Error sincronizando vertex marker: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)

    def _sync_rubber_band_properties(self, source: QgsRubberBand, target: QgsRubberBand):
        """
        Sincroniza las propiedades de un QgsRubberBand.
        """
        try:
            # Copiar propiedades de estilo
            # Usar strokeColor() en lugar de color() para QgsRubberBand
            if hasattr(source, 'strokeColor'):
                target.setColor(source.strokeColor())
            elif hasattr(source, 'color'):
                target.setColor(source.color())
                
            if hasattr(source, 'fillColor'):
                target.setFillColor(source.fillColor())
                
            if hasattr(source, 'width'):
                target.setWidth(source.width())
            
            # Copiar geometría con manejo inteligente de Z
            geom = source.asGeometry()
            if geom and not geom.isEmpty():
                if self.trf_wld2prp:
                    # Aplicar transformación 3D con Z individuales por vértice
                    transformed_geom = self._transform_geometry_with_vertex_z(geom, source)
                    if transformed_geom:
                        target.setToGeometry(transformed_geom, None)
                    else:
                        target.setToGeometry(geom, None)
                else:
                    target.setToGeometry(geom, None)
            
        except Exception as e:
            QgsMessageLog.logMessage(f"Error sincronizando rubber band: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)

    def _transform_geometry_with_vertex_z(self, geom: QgsGeometry, source_rubber_band: QgsRubberBand) -> Optional[QgsGeometry]:
        """
        Transforma una geometría aplicando la proyección 3D con Z individuales por vértice.
        Preserva las Z capturadas durante la digitalización usando coordenadas XY como clave.
        """
        try:
            if not self.trf_wld2prp or not geom:
                return geom
            
            # Obtener o inicializar cache de Z para este rubber band
            if source_rubber_band not in self.vertex_z_cache:
                self.vertex_z_cache[source_rubber_band] = {}
            
            z_cache = self.vertex_z_cache[source_rubber_band]
            current_z = self.parent.z_cursor if self.parent else 0
            
            def get_vertex_key(point) -> str:
                """Genera una clave única para un vértice basada en sus coordenadas XY"""
                return f"{point.x():.6f},{point.y():.6f}"
            
            # Transformar según tipo de geometría
            if geom.type() == QgsWkbTypes.PointGeometry:
                point = geom.asPoint()
                vertex_key = get_vertex_key(point)
                # Si el punto está en cache, usar esa Z; si no, usar Z actual y guardarlo
                if vertex_key in z_cache:
                    vertex_z = z_cache[vertex_key]
                else:
                    vertex_z = current_z
                    z_cache[vertex_key] = vertex_z  # Capturar Z
                
                pnt_wrl = QgsPoint(point.x(), point.y(), vertex_z)
                pnt_prj = self.trf_wld2prp.execute_wrl2prp(pnt_wrl)
                if pnt_prj:
                    return QgsGeometry.fromPointXY(pnt_prj)
                    
            elif geom.type() in [QgsWkbTypes.LineGeometry, QgsWkbTypes.PolygonGeometry]:
                if geom.type() == QgsWkbTypes.LineGeometry:
                    polyline = geom.asPolyline()
                    
                    # Limpiar cache de vértices que ya no existen
                    current_keys = set(get_vertex_key(point) for point in polyline)
                    cached_keys = set(z_cache.keys())
                    for old_key in cached_keys - current_keys:
                        del z_cache[old_key]
                    
                    # Transformar cada punto
                    transformed_points = []
                    for i, point in enumerate(polyline):
                        vertex_key = get_vertex_key(point)
                        
                        if vertex_key in z_cache:
                            # Vértice ya capturado: usar Z del cache
                            vertex_z = z_cache[vertex_key]
                        else:
                            # Vértice nuevo/temporal: usar Z actual del cursor
                            # Solo guardar en cache si no es el último vértice (que puede ser temporal)
                            vertex_z = current_z
                            if i < len(polyline) - 1:  # No es el último vértice
                                z_cache[vertex_key] = vertex_z
                        
                        pnt_wrl = QgsPoint(point.x(), point.y(), vertex_z)
                        pnt_prj = self.trf_wld2prp.execute_wrl2prp(pnt_wrl)
                        if pnt_prj:
                            transformed_points.append(pnt_prj)
                        else:
                            transformed_points.append(point)
                    
                    if transformed_points:
                        return QgsGeometry.fromPolylineXY(transformed_points)
                        
                elif geom.type() == QgsWkbTypes.PolygonGeometry:
                    polygon = geom.asPolygon()
                    if polygon:
                        transformed_rings = []
                        
                        for ring in polygon:
                            transformed_ring = []
                            for i, point in enumerate(ring):
                                vertex_key = get_vertex_key(point)
                                
                                if vertex_key in z_cache:
                                    vertex_z = z_cache[vertex_key]
                                else:
                                    vertex_z = current_z
                                    # Guardar en cache (los polígonos son menos propensos a tener vértices temporales)
                                    z_cache[vertex_key] = vertex_z
                                
                                pnt_wrl = QgsPoint(point.x(), point.y(), vertex_z)
                                pnt_prj = self.trf_wld2prp.execute_wrl2prp(pnt_wrl)
                                if pnt_prj:
                                    transformed_ring.append(pnt_prj)
                                else:
                                    transformed_ring.append(point)
                            
                            if transformed_ring:
                                transformed_rings.append(transformed_ring)
                        
                        if transformed_rings:
                            return QgsGeometry.fromPolygonXY(transformed_rings)
            
            return geom
            
        except Exception as e:
            QgsMessageLog.logMessage(f"Error transformando geometría con Z de vértices: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)
            return geom

    def force_sync_canvas_items(self):
        """
        Fuerza una sincronización inmediata de todos los map canvas items.
        Método público para ser llamado desde el exterior cuando sea necesario.
        """
        self.sync_timer.stop()
        self._sync_canvas_items()

    def set_canvas_items_sync_enabled(self, enabled: bool):
        """
        Habilita o deshabilita la sincronización automática de map canvas items.
        """
        if enabled:
            if not self.sync_timer.isActive():
                self.sync_timer.start(500)
        else:
            self.sync_timer.stop()

    def cleanup_canvas_items_sync(self):
        """
        Limpia todos los recursos relacionados con la sincronización de canvas items.
        Debe ser llamado al cerrar o destruir el canvas.
        """
        self.sync_timer.stop()
        
        # Limpiar todos los items sincronizados
        for synced_item in self.synced_items.values():
            try:
                if hasattr(synced_item, 'hide'):
                    synced_item.hide()
                self._safe_remove_item(synced_item)
            except Exception:
                pass  # Ignorar errores durante la limpieza
        
        self.synced_items.clear()
        self.vertex_z_cache.clear()

    def _safe_remove_item(self, item):
        """
        Remueve un item del canvas de forma segura, evitando errores de Qt.
        """
        try:
            # Verificar que el item existe y tiene una scene válida
            if not item:
                return
                
            item_scene = None
            if hasattr(item, 'scene'):
                item_scene = item.scene()
            
            # Si el item no tiene scene, no hay nada que remover
            if not item_scene:
                return
                
            # Verificar que la scene del item coincide con nuestra scene
            canvas_scene = self.scene() if hasattr(self, 'scene') else None
            if canvas_scene and item_scene == canvas_scene:
                canvas_scene.removeItem(item)
            elif item_scene:
                # Si las scenes son diferentes, remover del item's scene
                item_scene.removeItem(item)
                
        except Exception as e:
            # Silenciar errores de Qt relacionados con scene management
            pass

    # ============================================================================
    # == Fin de sincronización de Map Canvas Items ==
    # ============================================================================

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

        if self.filter == self.FILTER_NONE:
            super().paintEvent(e)
        else:
            # Render base content
            buffer = QImage(self.size(), QImage.Format_ARGB32)
            buffer.fill(QColor(0, 0, 0, 0))  # QColor(Qt.GlobalColor.transparent)
            super().render(QPainter(buffer))
            filtered = self.apply_filter(buffer)  # Apply filter to the buffer
            painter = QPainter(self.viewport())  # Paint viewport
            if self.parent and self.parent.stereo_id < 3 and self.is_left:  
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)               
            painter.drawImage(0, 0, filtered)
            painter.end()        
            self.viewport().update()
        
        # Dibujar texto Z si existe
        if self.z_text:
            from qgis.PyQt.QtGui import QFont
            painter = QPainter(self.viewport())
            font = QFont()
            font.setPointSize(18)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(int(self.width()/2 - painter.fontMetrics().horizontalAdvance(self.z_text)/2), int(self.height() * 3 / 4), self.z_text)
            painter.end()
            

    def force_repaint(self):
        """
        Force a lightweight repaint of the viewport.
        Used only for visual filters (stereo, anaglyph, interlaced),
        without triggering a full map refresh.
        """
        if self.filter != self.FILTER_NONE:
            self.viewport().update()
    
    def update_z_text(self, z_value):
        """Actualiza el texto Z mostrado en el canvas"""
        self.z_text = f"Z={z_value:.1f}"
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

    def sync_cursor(self, point_xy):
        """
        Evento de movimiento del cursor enviado por el padre.
        Updates the position of the cursor marker in the canvas.
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
        
        # Forzar sincronización de map canvas items después de cambiar capas
        self.force_sync_canvas_items()

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
