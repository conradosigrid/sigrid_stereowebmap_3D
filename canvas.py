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
from qgis.core import QgsWkbTypes, QgsGeometry, QgsRasterLayer, QgsVectorLayer, QgsPoint, QgsPointXY
from qgis.core import QgsSymbol, QgsSingleSymbolRenderer, QgsGeometryGeneratorSymbolLayer
from qgis.PyQt.QtGui import QColor, QWheelEvent, QImage, QPainter
from qgis.PyQt.QtCore import Qt
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
        self.geometry_cache: Dict[QgsMapCanvasItem, str] = {}  # rubber_band -> geometry_wkt para evitar duplicados
        self.sync_in_progress = False  # Para evitar sincronizaciones concurrentes
        
        # Sistema de tracking Z paralelo - "objeto gemelo" para captura Z incremental
        self.rubber_band_z_tracker: Dict[QgsRubberBand, List[float]] = {}  # rubber_band -> [z1, z2, z3, ...]
        
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
        
        # Conectar a señales del canvas principal para sincronización reactiva
        if hasattr(self.qgis_main_canvas, 'mapCanvasRefreshed'):
            self.qgis_main_canvas.mapCanvasRefreshed.connect(self._sync_canvas_items)
        
        # Conectar a señales de la escena para detectar cambios en items
        if hasattr(self.qgis_main_canvas, 'scene') and self.qgis_main_canvas.scene():
            scene = self.qgis_main_canvas.scene()
            if hasattr(scene, 'changed'):
                scene.changed.connect(self._on_scene_changed)
        
        QgsMessageLog.logMessage(f"SYNC: Configuración de sincronización por señales completada para canvas {'LEFT' if self.is_left else 'RIGHT'}", "SWM-3D", Qgis.Info)

    def _on_scene_changed(self, regions):
        """
        Maneja cambios en la escena del canvas principal.
        """
        # Solo sincronizar si hay cambios significativos
        if regions:  # Si hay regiones cambiadas
            self._sync_canvas_items()
    
    def _sync_canvas_items(self):
        """
        Sincroniza todos los map canvas items del canvas principal con este canvas.
        """
        # Evitar sincronización concurrente
        if self.sync_in_progress:
            return
            
        self.sync_in_progress = True
        
        # QgsMessageLog.logMessage("SYNC: Iniciando sincronización de canvas items", "SWM-3D", Qgis.Info)
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
                    
                    # Limpiar cache de geometría para rubber bands removidos
                    if main_item in self.geometry_cache:
                        del self.geometry_cache[main_item]
                        
                    # Limpiar Z tracker para rubber bands removidos
                    if isinstance(main_item, QgsRubberBand) and main_item in self.rubber_band_z_tracker:
                        tracked_z_count = len(self.rubber_band_z_tracker[main_item])
                        del self.rubber_band_z_tracker[main_item]
                        QgsMessageLog.logMessage(
                            f"Z-TRACKER: Rubber band removido → limpiadas {tracked_z_count} Z's", 
                            "SWM-3D", Qgis.Info
                        )
            
            # Añadir o actualizar items existentes
            for main_item in main_items:
                if main_item not in self.synced_items:
                    # Crear nuevo item sincronizado
                    synced_item = self._create_synced_item(main_item)
                    if synced_item:
                        self.synced_items[main_item] = synced_item
                else:
                    # Actualizar item existente
                    # QgsMessageLog.logMessage(f"SYNC: Actualizando item existente: {type(main_item).__name__}", "SWM-3D", Qgis.Info)
                    self._update_synced_item(main_item, self.synced_items[main_item])
                    
        except Exception as e:
            QgsMessageLog.logMessage(f"Error sincronizando map canvas items: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)
        finally:
            self.sync_in_progress = False

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

            else:
                # Para otros tipos de items, podríamos implementar lógica de actualización específica
                QgsMessageLog.logMessage(f"Unmannaged item type for synchronization: {type(main_item)}", 
                        "SWM-3D", Qgis.Warning)    
                
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
        Sincroniza las propiedades de un QgsRubberBand con sistema de tracking Z paralelo.
        Captura Z incremental del cursor y la almacena en "objeto gemelo".
        """
        try:
            # Copiar propiedades de estilo
            if hasattr(source, 'strokeColor'):
                target.setColor(source.strokeColor())
            elif hasattr(source, 'color'):
                target.setColor(source.color())
                
            if hasattr(source, 'fillColor'):
                target.setFillColor(source.fillColor())
                
            if hasattr(source, 'width'):
                target.setWidth(source.width())
            
            # Copiar geometría con tracking Z paralelo
            geom = source.asGeometry()
            if geom and not geom.isEmpty():
                # SISTEMA TRACKING Z: Detectar cambios y capturar Z del cursor
                self._track_rubber_band_z_changes(source, geom)
                
                # Aplicar Z del tracker para visualización en canvas estéreo
                geom_with_tracked_z = self._apply_tracked_z_to_geometry(geom, source)
                
                if self.trf_wld2prp:
                    # Aplicar transformación 3D con Z del tracker
                    transformed_geom = self._transform_geometry_with_vertex_z(geom_with_tracked_z, source)
                    if transformed_geom:
                        target.setToGeometry(transformed_geom, None)
                    else:
                        target.setToGeometry(geom_with_tracked_z, None)
                else:
                    target.setToGeometry(geom_with_tracked_z, None)
            
        except Exception as e:
            QgsMessageLog.logMessage(f"Error sincronizando rubber band: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)

    def _track_rubber_band_z_changes(self, rubber_band: QgsRubberBand, current_geom: QgsGeometry):
        """
        Sistema de tracking Z paralelo - "objeto gemelo" para captura incremental.
        Detecta cambios en rubber band y captura Z del cursor por vértice.
        """
        try:
            if not self.parent:
                return
                
            # Obtener número de vértices actual
            const_geom = current_geom.constGet()
            if not const_geom:
                return
                
            current_vertex_count = const_geom.vertexCount()
            
            # Obtener Z del cursor actual
            cursor_z = getattr(self.parent, 'z_cursor', 0.0)
            
            # Inicializar tracker si es nuevo rubber band
            if rubber_band not in self.rubber_band_z_tracker:
                self.rubber_band_z_tracker[rubber_band] = []
                QgsMessageLog.logMessage(
                    f"Z-TRACKER: Nuevo rubber band detectado (Z cursor: {cursor_z})", 
                    "SWM-3D", Qgis.Info
                )
            
            # Obtener Z's almacenadas para este rubber band
            tracked_z_list = self.rubber_band_z_tracker[rubber_band]
            
            # Si hay más vértices que Z's almacenadas → nuevos vértices añadidos
            if current_vertex_count > len(tracked_z_list):
                new_vertices_count = current_vertex_count - len(tracked_z_list)
                
                # Capturar Z actual del cursor para los nuevos vértices
                for i in range(new_vertices_count):
                    tracked_z_list.append(cursor_z)
                
                QgsMessageLog.logMessage(
                    f"Z-TRACKER: {new_vertices_count} nuevos vértices → Z={cursor_z} "
                    f"(total: {len(tracked_z_list)} Z's)", 
                    "SWM-3D", Qgis.Warning
                )
                
            # Si hay menos vértices → rubber band reducido (undo, etc.)
            elif current_vertex_count < len(tracked_z_list):
                # Recortar lista de Z's para coincidir con vértices actuales
                tracked_z_list[:] = tracked_z_list[:current_vertex_count]
                
                QgsMessageLog.logMessage(
                    f"Z-TRACKER: Rubber band reducido a {current_vertex_count} vértices "
                    f"(Z's: {len(tracked_z_list)})", 
                    "SWM-3D", Qgis.Info
                )
                
        except Exception as e:
            QgsMessageLog.logMessage(f"Error tracking Z changes: {str(e)}", "SWM-3D", Qgis.Warning)

    def _apply_tracked_z_to_geometry(self, geom: QgsGeometry, rubber_band: QgsRubberBand) -> QgsGeometry:
        """
        Aplica las Z's del tracker paralelo a la geometría para visualización.
        Usa las Z capturadas incrementalmente, no la Z cursor actual.
        """
        try:
            # Si no hay Z's trackeadas para este rubber band, retornar sin cambios
            if rubber_band not in self.rubber_band_z_tracker:
                return geom
                
            tracked_z_list = self.rubber_band_z_tracker[rubber_band]
            if not tracked_z_list:
                return geom
            
            # Crear nueva geometría con Z's del tracker
            new_geom = QgsGeometry(geom)
            const_geom = new_geom.constGet()
            if not const_geom:
                return geom
            
            vertex_count = const_geom.vertexCount()
            applied_count = 0
            
            # Aplicar Z's del tracker a cada vértice
            for i in range(vertex_count):
                if i < len(tracked_z_list):
                    vertex = new_geom.vertexAt(i)
                    tracked_z = tracked_z_list[i]
                    
                    if vertex.z() != tracked_z:
                        vertex.setZ(tracked_z)
                        new_geom.moveVertex(vertex, i)
                        applied_count += 1
            
            if applied_count > 0:
                z_summary = ", ".join([f"{z:.1f}" for z in tracked_z_list[:3]])  # Primeras 3 Z's
                if len(tracked_z_list) > 3:
                    z_summary += "..."
                    
                QgsMessageLog.logMessage(
                    f"Z-TRACKER: ✅ Aplicadas Z's trackeadas [{z_summary}] a {applied_count} vértices", 
                    "SWM-3D", Qgis.Info
                )
            
            return new_geom
            
        except Exception as e:
            QgsMessageLog.logMessage(f"Error aplicando Z's trackeadas: {str(e)}", "SWM-3D", Qgis.Warning)
            return geom

    def get_rubber_band_tracked_z(self, rubber_band: QgsRubberBand) -> List[float]:
        """
        Método público para obtener las Z's capturadas de un rubber band específico.
        Útil para aplicar Z's al finalizar digitalización en capa vectorial.
        
        Returns:
            List[float]: Lista de Z's capturadas por vértice, o lista vacía si no hay tracking
        """
        if rubber_band in self.rubber_band_z_tracker:
            return self.rubber_band_z_tracker[rubber_band].copy()  # Retornar copia para seguridad
        return []
    
    def clear_rubber_band_tracked_z(self, rubber_band: QgsRubberBand) -> bool:
        """
        Limpia las Z's trackeadas de un rubber band específico.
        Útil después de aplicar las Z's a la geometría final.
        
        Returns:
            bool: True si se encontró y limpió el tracker, False si no existía
        """
        if rubber_band in self.rubber_band_z_tracker:
            z_count = len(self.rubber_band_z_tracker[rubber_band])
            del self.rubber_band_z_tracker[rubber_band]
            QgsMessageLog.logMessage(
                f"Z-TRACKER: Limpiado manualmente tracker con {z_count} Z's", 
                "SWM-3D", Qgis.Info
            )
            return True
        return False

    def _transform_geometry_with_vertex_z(self, geom: QgsGeometry, source_rubber_band: QgsRubberBand) -> Optional[QgsGeometry]:
        """
        Transforma una geometría aplicando la proyección 3D con Z individuales por vértice.
        Preserva las Z capturadas durante la digitalización usando coordenadas XY como clave.
        """
        try:
            if not self.trf_wld2prp or not geom:
                return geom
            
            # Verificar si la geometría ha cambiado para evitar procesamientos duplicados
            current_geom_wkt = geom.asWkt()
            if source_rubber_band in self.geometry_cache:
                if self.geometry_cache[source_rubber_band] == current_geom_wkt:
                    # Solo logear ocasionalmente para evitar spam
                    # QgsMessageLog.logMessage(f"SYNC: Geometría sin cambios, omitiendo procesamiento duplicado", "SWM-3D", Qgis.Info)
                    return geom  # No ha cambiado, usar transformación anterior
            
            # Actualizar cache de geometría
            self.geometry_cache[source_rubber_band] = current_geom_wkt
            
            QgsMessageLog.logMessage(f"SYNC: Procesando nueva geometría: {geom.type()}", "SWM-3D", Qgis.Info)
            
            # Transformar según tipo de geometría
            if geom.type() == QgsWkbTypes.PointGeometry:
                # Acceder a vértices preservando Z 
                if geom.wkbType() in [QgsWkbTypes.Point, QgsWkbTypes.Point25D, QgsWkbTypes.PointZ, QgsWkbTypes.PointM, QgsWkbTypes.PointZM]:
                    # Point simple - usar vertexAt para preservar Z
                    vertex = geom.vertexAt(0)  # QgsPoint con X,Y,Z original
                    
                    pnt_wrl = QgsPoint(vertex.x(), vertex.y(), vertex.z())
                    pnt_prj = self.trf_wld2prp.execute_wrl2prp(pnt_wrl)
                    if pnt_prj:
                        return QgsGeometry.fromPointXY(pnt_prj)
                        
                elif geom.wkbType() in [QgsWkbTypes.MultiPoint, QgsWkbTypes.MultiPoint25D, QgsWkbTypes.MultiPointZ, QgsWkbTypes.MultiPointM, QgsWkbTypes.MultiPointZM]:
                    # MultiPoint - acceder a vértices preservando Z
                    const_geom = geom.constGet()
                    if const_geom is None:
                        return geom
                        
                    transformed_points = []
                    for i in range(const_geom.vertexCount()):
                        vertex = geom.vertexAt(i)  # QgsPoint con X,Y,Z original
                        
                        pnt_wrl = QgsPoint(vertex.x(), vertex.y(), vertex.z())
                        pnt_prj = self.trf_wld2prp.execute_wrl2prp(pnt_wrl)
                        if pnt_prj:
                            transformed_points.append(pnt_prj)
                        else:
                            transformed_points.append(QgsPointXY(vertex.x(), vertex.y()))
                    
                    if transformed_points:
                        return QgsGeometry.fromMultiPointXY(transformed_points)
                else:
                    # Tipo de Point no reconocido, devolver geometría original
                    QgsMessageLog.logMessage(f"Tipo de geometría Point no reconocido: {geom.wkbType()}", 
                                           "SWM-3D", Qgis.Warning)
                    return geom
                    
            elif geom.type() in [QgsWkbTypes.LineGeometry, QgsWkbTypes.PolygonGeometry]:
                if geom.type() == QgsWkbTypes.LineGeometry:
                    # Acceder a vértices preservando Z
                    const_geom = geom.constGet()
                    if const_geom is None:
                        return geom
                        
                    # Transformar cada punto manteniendo Z original
                    transformed_points = []
                    for i in range(const_geom.vertexCount()):
                        vertex = geom.vertexAt(i)  # QgsPoint con X,Y,Z original
                        
                        # Usar Z original de la geometría (no cursor Z ni cache)
                        pnt_wrl = QgsPoint(vertex.x(), vertex.y(), vertex.z())
                        pnt_prj = self.trf_wld2prp.execute_wrl2prp(pnt_wrl)
                        if pnt_prj:
                            transformed_points.append(pnt_prj)
                        else:
                            transformed_points.append(QgsPointXY(vertex.x(), vertex.y()))
                    
                    if transformed_points:
                        return QgsGeometry.fromPolylineXY(transformed_points)
                        
                elif geom.type() == QgsWkbTypes.PolygonGeometry:
                    # Acceder a vértices preservando Z sin usar cache
                    const_geom = geom.constGet()
                    if const_geom is None:
                        return geom
                        
                    # Transformar cada punto manteniendo Z original
                    transformed_points = []
                    for i in range(const_geom.vertexCount()):
                        vertex = geom.vertexAt(i)  # QgsPoint con X,Y,Z original
                        
                        # Usar Z original de la geometría (no cursor Z ni cache)
                        pnt_wrl = QgsPoint(vertex.x(), vertex.y(), vertex.z())
                        pnt_prj = self.trf_wld2prp.execute_wrl2prp(pnt_wrl)
                        if pnt_prj:
                            transformed_points.append(pnt_prj)
                        else:
                            transformed_points.append(QgsPointXY(vertex.x(), vertex.y()))
                    
                    # Reconstruir geometría según el tipo
                    if transformed_points:
                        if geom.wkbType() in [QgsWkbTypes.MultiPolygon, QgsWkbTypes.MultiPolygon25D, QgsWkbTypes.MultiPolygonZ, QgsWkbTypes.MultiPolygonM, QgsWkbTypes.MultiPolygonZM]:
                            # Para MultiPolygon, necesitamos reconstruir la estructura de anillos
                            # Usar la estructura original para mantener la topología
                            return self._reconstruct_multipolygon_geometry(geom, transformed_points)
                        else:
                            # Para Polygon simple, reconstruir usando la estructura original
                            return self._reconstruct_polygon_geometry(geom, transformed_points)
                else:
                    QgsMessageLog.logMessage(f"Unrecognized geometry type: {str(geom.type())}", 
                                   "SWM-3D", Qgis.Warning)
            
            return geom
            
        except Exception as e:
            QgsMessageLog.logMessage(f"Error transformando geometría con Z de vértices: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)
            return geom

    def _reconstruct_polygon_geometry(self, original_geom: QgsGeometry, transformed_points: List) -> QgsGeometry:
        """
        Reconstruye un polígono simple preservando la estructura de anillos.
        """
        try:
            polygon = original_geom.asPolygon()
            if not polygon:
                return original_geom
                
            point_index = 0
            transformed_rings = []
            
            for ring in polygon:
                transformed_ring = []
                for _ in range(len(ring)):
                    if point_index < len(transformed_points):
                        transformed_ring.append(transformed_points[point_index])
                        point_index += 1
                
                if transformed_ring:
                    transformed_rings.append(transformed_ring)
            
            if transformed_rings:
                return QgsGeometry.fromPolygonXY(transformed_rings)
                
        except Exception as e:
            QgsMessageLog.logMessage(f"Error reconstruyendo polígono: {str(e)}", "SWM-3D", Qgis.Warning)
            
        return original_geom

    def _reconstruct_multipolygon_geometry(self, original_geom: QgsGeometry, transformed_points: List) -> QgsGeometry:
        """
        Reconstruye un multipolígono preservando la estructura de polígonos y anillos.
        """
        try:
            multipolygon = original_geom.asMultiPolygon()
            if not multipolygon:
                return original_geom
                
            point_index = 0
            transformed_polygons = []
            
            for polygon in multipolygon:
                transformed_rings = []
                
                for ring in polygon:
                    transformed_ring = []
                    for _ in range(len(ring)):
                        if point_index < len(transformed_points):
                            transformed_ring.append(transformed_points[point_index])
                            point_index += 1
                    
                    if transformed_ring:
                        transformed_rings.append(transformed_ring)
                
                if transformed_rings:
                    transformed_polygons.append(transformed_rings)
            
            if transformed_polygons:
                return QgsGeometry.fromMultiPolygonXY(transformed_polygons)
                
        except Exception as e:
            QgsMessageLog.logMessage(f"Error reconstruyendo multipolígono: {str(e)}", "SWM-3D", Qgis.Warning)
            
        return original_geom

    def force_sync_canvas_items(self):
        """
        Fuerza una sincronización inmediata de todos los map canvas items.
        Método público para ser llamado desde el exterior cuando sea necesario.
        """
        # Evitar múltiples llamadas rápidas desde sync_layers
        if self.sync_in_progress:
            QgsMessageLog.logMessage(f"SYNC: Omitiendo fuerza sync (en progreso) - Canvas {'LEFT' if self.is_left else 'RIGHT'}", "SWM-3D", Qgis.Info)
            return
            
        QgsMessageLog.logMessage(f"SYNC: Forzando sincronización inmediata - Canvas {'LEFT' if self.is_left else 'RIGHT'}", "SWM-3D", Qgis.Warning)
        self._sync_canvas_items()

    def set_canvas_items_sync_enabled(self, enabled: bool):
        """
        Habilita o deshabilita la sincronización automática de map canvas items.
        """
        if enabled:
            # Reconectar señales si es necesario
            self._setup_canvas_items_sync()
        else:
            # Desconectar señales para deshabilitar la sincronización
            try:
                if hasattr(self.qgis_main_canvas, 'mapCanvasRefreshed'):
                    self.qgis_main_canvas.mapCanvasRefreshed.disconnect(self._sync_canvas_items)
                
                if hasattr(self.qgis_main_canvas, 'scene') and self.qgis_main_canvas.scene():
                    scene = self.qgis_main_canvas.scene()
                    if hasattr(scene, 'changed'):
                        scene.changed.disconnect(self._on_scene_changed)
            except RuntimeError:
                pass  # Las señales pueden no estar conectadas

    def cleanup_canvas_items_sync(self):
        """
        Limpia todos los recursos relacionados con la sincronización de canvas items.
        Debe ser llamado al cerrar o destruir el canvas.
        """
        # Desconectar señales
        try:
            if hasattr(self.qgis_main_canvas, 'mapCanvasRefreshed'):
                self.qgis_main_canvas.mapCanvasRefreshed.disconnect(self._sync_canvas_items)
            
            if hasattr(self.qgis_main_canvas, 'scene') and self.qgis_main_canvas.scene():
                scene = self.qgis_main_canvas.scene()
                if hasattr(scene, 'changed'):
                    scene.changed.disconnect(self._on_scene_changed)
        except RuntimeError:
            pass  # Las señales pueden no estar conectadas
        
        # Limpiar todos los items sincronizados
        for synced_item in self.synced_items.values():
            try:
                if hasattr(synced_item, 'hide'):
                    synced_item.hide()
                self._safe_remove_item(synced_item)
            except Exception:
                pass  # Ignorar errores durante la limpieza
        
        self.synced_items.clear()
        self.geometry_cache.clear()
        
        # Limpiar sistema de tracking Z paralelo
        tracked_rubber_bands_count = len(self.rubber_band_z_tracker)
        self.rubber_band_z_tracker.clear()
        if tracked_rubber_bands_count > 0:
            QgsMessageLog.logMessage(
                f"Z-TRACKER: Limpiados {tracked_rubber_bands_count} rubber band trackers", 
                "SWM-3D", Qgis.Info
            )
        
        self.sync_in_progress = False

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


    def update_z_text(self, z_value):
        """Actualiza el texto Z mostrado en el canvas"""
        self.z_text = f"Z={z_value:.1f}"
        self.viewport().update()

    def apply_filter(self, image):
        """Ultra-fast version with precise results"""
        if self.filter == self.FILTER_NONE:
            return image

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
                # QgsMessageLog.logMessage(f"SYNC_LAYER Capa: {layer_main.name()}-{'LEFT' if self.is_left else 'RIGHT'}.", 
                #                          "SWM-3D", Qgis.Info)
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

                # QgsMessageLog.logMessage(f"UPDATE_SWM_HEADER Capa: {layer.name()}-{'LEFT' if self.is_left else 'RIGHT'}.", 
                #                          "SWM-3D", Qgis.Info)   
        self.render_complete()
