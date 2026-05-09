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
    ├── mouse (source)
  ├── zoom
    ├── layers
    └── signals
        ↓
QgsSgdSwmCanvas (plugin)
    ├── Z (internal view state)
    ├── transformation
    ├── projected cursor
  └── render
"""
from qgis.core import QgsMessageLog, Qgis  # for debug messages.
from qgis.gui import QgsMapCanvas, QgsVertexMarker, QgsRubberBand, QgsMapCanvasItem
from qgis.core import QgsWkbTypes, QgsGeometry, QgsRasterLayer, QgsVectorLayer, QgsPoint, QgsPointXY
from qgis.core import QgsSymbol, QgsSingleSymbolRenderer, QgsGeometryGeneratorSymbolLayer
from qgis.PyQt.QtGui import QColor, QWheelEvent, QImage, QPainter
from qgis.PyQt.QtCore import Qt
from typing import Optional, Any, Dict, List

import re
import math
import numpy as np
# SWM libraries
from .transform import TrfWldToPrjPln
from .utils import is_sgd_swm_layer, is_z_layer
from .expressions.perspective_swm_transform import read_perspective, read_projective, world_to_photo, photo_to_proj


# Class Sigrid SWM slave (mirrored) canvas transformed from the main QGIS canvas
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

        # Cursor marker (must be created before item synchronization)
        self.cursor_marker = QgsVertexMarker(self)
        self.cursor_marker.setColor(QColor(Qt.GlobalColor.black))
        self.cursor_marker.setIconSize(10)
        self.cursor_marker.setIconType(QgsVertexMarker.ICON_CROSS)
        self.cursor_marker.setPenWidth(3)

        # Map canvas items synchronization (after creating cursor_marker)
        self.synced_items: Dict[QgsMapCanvasItem, QgsMapCanvasItem] = {}  # main_item -> synced_item
        self.geometry_cache: Dict[QgsMapCanvasItem, str] = {}  # rubber_band -> geometry_wkt to avoid duplicates
        self.sync_in_progress = False  # Prevent concurrent synchronizations
        
        # Parallel Z-tracking system - "twin object" for incremental Z capture
        self.rubber_band_z_tracker: Dict[QgsRubberBand, List[float]] = {}  # rubber_band -> [z1, z2, z3, ...]
        
        self._setup_canvas_items_sync()

        self.layer_swm = None
        self.layers_z = []
        self.limits = None
        self.z_text = ""  # Z cursor text

        self.setCanvasColor(QColor(0, 0, 0, 0))  # QColor(Qt.GlobalColor.transparent)

    # ============================================================================
    # == Cursor in the stereo canvas ==
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
    # == Map Canvas Item Synchronization ==
    # ============================================================================

    def _setup_canvas_items_sync(self):
        """
        Configures automatic synchronization of map canvas items from the main canvas.
        """
        # Perform initial synchronization
        self._sync_canvas_items()
        
        # Connect to main canvas signals for reactive synchronization
        if hasattr(self.qgis_main_canvas, 'mapCanvasRefreshed'):
            self.qgis_main_canvas.mapCanvasRefreshed.connect(self._sync_canvas_items)
        
        # Connect to scene signals to detect item changes
        if hasattr(self.qgis_main_canvas, 'scene') and self.qgis_main_canvas.scene():
            scene = self.qgis_main_canvas.scene()
            if hasattr(scene, 'changed'):
                scene.changed.connect(self._on_scene_changed)
        
        QgsMessageLog.logMessage(f"SYNC: Signal-based synchronization configured for {'LEFT' if self.is_left else 'RIGHT'} canvas", "SWM-3D", Qgis.Info)

    def _on_scene_changed(self, regions):
        """
        Handles changes in the main canvas scene.
        """
        # Synchronize only when there are meaningful changes
        if regions:  # Changed regions exist
            self._sync_canvas_items()
    
    def _sync_canvas_items(self):
        """
        Synchronizes all map canvas items from the main canvas with this canvas.
        """
        # Prevent concurrent synchronization
        if self.sync_in_progress:
            return
            
        self.sync_in_progress = True
        
        # QgsMessageLog.logMessage("SYNC: Starting canvas item synchronization", "SWM-3D", Qgis.Info)
        try:
            if not self.qgis_main_canvas:
                return
                
            main_items = self._get_canvas_items(self.qgis_main_canvas)
            current_main_items = set(main_items)
            synced_main_items = set(self.synced_items.keys())
            
            # Remove items that no longer exist in the main canvas
            items_to_remove = synced_main_items - current_main_items
            for main_item in items_to_remove:
                if main_item in self.synced_items:
                    synced_item = self.synced_items[main_item]
                    if hasattr(synced_item, 'hide'):
                        synced_item.hide()
                    # Remove from canvas safely
                    self._safe_remove_item(synced_item)
                    del self.synced_items[main_item]
                    
                    # Clear geometry cache for removed rubber bands
                    if main_item in self.geometry_cache:
                        del self.geometry_cache[main_item]
                        
                    # Clear Z tracker for removed rubber bands
                    if isinstance(main_item, QgsRubberBand) and main_item in self.rubber_band_z_tracker:
                        tracked_z_count = len(self.rubber_band_z_tracker[main_item])
                        del self.rubber_band_z_tracker[main_item]
                        QgsMessageLog.logMessage(
                            f"Z-TRACKER: Rubber band removed -> cleared {tracked_z_count} Z values", 
                            "SWM-3D", Qgis.Info
                        )
            
            # Add or update existing items
            for main_item in main_items:
                if main_item not in self.synced_items:
                    # Create new synchronized item
                    synced_item = self._create_synced_item(main_item)
                    if synced_item:
                        self.synced_items[main_item] = synced_item
                else:
                    # Update existing item
                    # QgsMessageLog.logMessage(f"SYNC: Updating existing item: {type(main_item).__name__}", "SWM-3D", Qgis.Info)
                    self._update_synced_item(main_item, self.synced_items[main_item])
                    
        except Exception as e:
            QgsMessageLog.logMessage(f"Error synchronizing map canvas items: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)
        finally:
            self.sync_in_progress = False

    def _get_canvas_items(self, canvas) -> List[QgsMapCanvasItem]:
        """
        Gets all map canvas items from a canvas.
        """
        items = []
        try:
            if hasattr(canvas, 'scene') and canvas.scene():
                for item in canvas.scene().items():
                    # Ensure it is a QgsMapCanvasItem and exclude our cursor marker if present
                    if isinstance(item, QgsMapCanvasItem):
                        # Exclude our own cursor marker to avoid recursion
                        if hasattr(self, 'cursor_marker') and item == self.cursor_marker:
                            continue
                        items.append(item)
        except Exception as e:
            QgsMessageLog.logMessage(f"Error getting canvas items: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)
        return items

    def _create_synced_item(self, main_item: QgsMapCanvasItem) -> Optional[QgsMapCanvasItem]:
        """
        Creates a synchronized copy of a map canvas item from the main canvas.
        """
        try:
            synced_item = None
            
            if isinstance(main_item, QgsVertexMarker):
                synced_item = QgsVertexMarker(self)
                self._sync_vertex_marker_properties(main_item, synced_item)
                
            elif isinstance(main_item, QgsRubberBand):
                # Get geometry type from the original rubber band
                geom_type = QgsWkbTypes.PolygonGeometry
                if hasattr(main_item, 'geometryType'):
                    geom_type = main_item.geometryType()
                    
                synced_item = QgsRubberBand(self, geom_type)
                self._sync_rubber_band_properties(main_item, synced_item)
            
            # Add more item types as needed
            # elif isinstance(main_item, OtherMapCanvasItemType):
            #     synced_item = self._create_other_item_type(main_item)
                
            if synced_item:
                synced_item.show()
                
            return synced_item
            
        except Exception as e:
            QgsMessageLog.logMessage(f"Error creating synchronized item: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)
            return None

    def _update_synced_item(self, main_item: QgsMapCanvasItem, synced_item: QgsMapCanvasItem):
        """
        Updates synchronized item properties based on the main item.
        """
        try:
            if isinstance(main_item, QgsVertexMarker) and isinstance(synced_item, QgsVertexMarker):
                self._sync_vertex_marker_properties(main_item, synced_item)
                
            elif isinstance(main_item, QgsRubberBand) and isinstance(synced_item, QgsRubberBand):
                self._sync_rubber_band_properties(main_item, synced_item)

            else:
                # For other item types, specific update logic can be added here
                QgsMessageLog.logMessage(f"Unmannaged item type for synchronization: {type(main_item)}", 
                        "SWM-3D", Qgis.Warning)    
                
            # Update visibility
            if hasattr(main_item, 'isVisible') and hasattr(synced_item, 'setVisible'):
                synced_item.setVisible(main_item.isVisible())
                
        except Exception as e:
            QgsMessageLog.logMessage(f"Error updating synchronized item: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)

    def _sync_vertex_marker_properties(self, source: QgsVertexMarker, target: QgsVertexMarker):
        """
        Synchronizes QgsVertexMarker properties.
        """
        try:
            # Copy basic properties safely
            # Check getter method availability before using them
            if hasattr(source, 'color'):
                target.setColor(source.color())
            
            # For iconSize, iconType and penWidth, some getters may not be available
            # If value retrieval fails, use reasonable defaults
            try:
                if hasattr(source, 'iconSize'):
                    target.setIconSize(source.iconSize())
                else:
                    target.setIconSize(10)  # Default value
            except AttributeError:
                target.setIconSize(10)
                
            try:
                if hasattr(source, 'iconType'):
                    target.setIconType(source.iconType())
                else:
                    target.setIconType(QgsVertexMarker.ICON_CROSS)  # Default value
            except AttributeError:
                target.setIconType(QgsVertexMarker.ICON_CROSS)
                
            try:
                if hasattr(source, 'penWidth'):
                    target.setPenWidth(source.penWidth())
                else:
                    target.setPenWidth(3)  # Default value
            except AttributeError:
                target.setPenWidth(3)
            
            # Copy and transform position
            center = source.center()
            if center and self.trf_wld2prp:
                # Apply 3D transformation if available
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
            QgsMessageLog.logMessage(f"Error synchronizing vertex marker: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)

    def _sync_rubber_band_properties(self, source: QgsRubberBand, target: QgsRubberBand):
        """
        Synchronizes QgsRubberBand properties using a parallel Z-tracking system.
        Captures incremental Z from the cursor and stores it in a "twin object".
        """
        try:
            # Copy style properties
            if hasattr(source, 'strokeColor'):
                target.setColor(source.strokeColor())
            elif hasattr(source, 'color'):
                target.setColor(source.color())
                
            if hasattr(source, 'fillColor'):
                target.setFillColor(source.fillColor())
                
            if hasattr(source, 'width'):
                target.setWidth(source.width())
            
            # Copy geometry using parallel Z tracking
            geom = source.asGeometry()
            if geom and not geom.isEmpty():
                # Z-TRACKING SYSTEM: detect changes and capture cursor Z
                self._track_rubber_band_z_changes(source, geom)
                
                # Apply tracked Z for stereo-canvas visualization
                geom_with_tracked_z = self._apply_tracked_z_to_geometry(geom, source)
                
                if self.trf_wld2prp:
                    # Apply 3D transformation with tracked Z, same logic as expressions
                    transformed_geom = self._transform_geometry(geom_with_tracked_z, source)
                    if transformed_geom:
                        target.setToGeometry(transformed_geom, None)
                    else:
                        target.setToGeometry(geom_with_tracked_z, None)
                else:
                    target.setToGeometry(geom_with_tracked_z, None)
            
        except Exception as e:
            QgsMessageLog.logMessage(f"Error synchronizing rubber band: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)

    def _track_rubber_band_z_changes(self, rubber_band: QgsRubberBand, current_geom: QgsGeometry):
        """
        Parallel Z-tracking system - "twin object" for incremental capture.
        Detects rubber band changes and captures cursor Z per vertex.
        """
        try:
            if not self.parent:
                return
                
            # Get current vertex count
            const_geom = current_geom.constGet()
            if not const_geom:
                return
                
            current_vertex_count = const_geom.vertexCount()
            
            # Get current cursor Z
            cursor_z = getattr(self.parent, 'z_cursor', 0.0)
            
            # Initialize tracker for a new rubber band
            if rubber_band not in self.rubber_band_z_tracker:
                self.rubber_band_z_tracker[rubber_band] = []
                QgsMessageLog.logMessage(
                    f"Z-TRACKER: New rubber band detected (cursor Z: {cursor_z})", 
                    "SWM-3D", Qgis.Info
                )
            
            # Get stored Z values for this rubber band
            tracked_z_list = self.rubber_band_z_tracker[rubber_band]
            
            # More vertices than tracked Z values -> new vertices were added
            if current_vertex_count > len(tracked_z_list):
                new_vertices_count = current_vertex_count - len(tracked_z_list)
                
                # Capture current cursor Z for new vertices
                for i in range(new_vertices_count):
                    tracked_z_list.append(cursor_z)
                
                QgsMessageLog.logMessage(
                    f"Z-TRACKER: {new_vertices_count} new vertices -> Z={cursor_z} "
                    f"(total: {len(tracked_z_list)} Z's)", 
                    "SWM-3D", Qgis.Warning
                )
                
            # Fewer vertices -> rubber band was reduced (undo, etc.)
            elif current_vertex_count < len(tracked_z_list):
                # Trim Z list to match current vertex count
                tracked_z_list[:] = tracked_z_list[:current_vertex_count]
                
                QgsMessageLog.logMessage(
                    f"Z-TRACKER: Rubber band reduced to {current_vertex_count} vertices "
                    f"(Z's: {len(tracked_z_list)})", 
                    "SWM-3D", Qgis.Info
                )
                
        except Exception as e:
            QgsMessageLog.logMessage(f"Error tracking Z changes: {str(e)}", "SWM-3D", Qgis.Warning)

    def _apply_tracked_z_to_geometry(self, geom: QgsGeometry, rubber_band: QgsRubberBand) -> QgsGeometry:
        """
        Applies parallel tracker Z values to geometry for visualization.
        Uses incrementally captured Z values, not the current cursor Z.
        """
        try:
            # If no tracked Z values exist for this rubber band, return unchanged
            if rubber_band not in self.rubber_band_z_tracker:
                return geom
                
            tracked_z_list = self.rubber_band_z_tracker[rubber_band]
            if not tracked_z_list:
                return geom
            
            # Create new geometry with tracker Z values
            new_geom = QgsGeometry(geom)
            const_geom = new_geom.constGet()
            if not const_geom:
                return geom
            
            vertex_count = const_geom.vertexCount()
            applied_count = 0
            
            # Apply tracker Z values to each vertex
            for i in range(vertex_count):
                if i < len(tracked_z_list):
                    vertex = new_geom.vertexAt(i)
                    tracked_z = tracked_z_list[i]
                    
                    if vertex.z() != tracked_z:
                        vertex.setZ(tracked_z)
                        new_geom.moveVertex(vertex, i)
                        applied_count += 1
            
            if applied_count > 0:
                z_summary = ", ".join([f"{z:.1f}" for z in tracked_z_list[:3]])  # First 3 Z values
                if len(tracked_z_list) > 3:
                    z_summary += "..."
                    
                QgsMessageLog.logMessage(
                    f"Z-TRACKER: Applied tracked Z values [{z_summary}] to {applied_count} vertices", 
                    "SWM-3D", Qgis.Info
                )
            
            return new_geom
            
        except Exception as e:
            QgsMessageLog.logMessage(f"Error applying tracked Z values: {str(e)}", "SWM-3D", Qgis.Warning)
            return geom

    def get_rubber_band_tracked_z(self, rubber_band: QgsRubberBand) -> List[float]:
        """
        Public method to get captured Z values for a specific rubber band.
        Useful when applying Z values after finishing vector-layer digitizing.
        
        Returns:
            List[float]: Captured Z values by vertex, or an empty list if not tracked
        """
        if rubber_band in self.rubber_band_z_tracker:
            return self.rubber_band_z_tracker[rubber_band].copy()  # Return a copy for safety
        return []
    
    def clear_rubber_band_tracked_z(self, rubber_band: QgsRubberBand) -> bool:
        """
        Clears tracked Z values for a specific rubber band.
        Useful after applying those Z values to final geometry.
        
        Returns:
            bool: True if tracker was found and cleared, False if it did not exist
        """
        if rubber_band in self.rubber_band_z_tracker:
            z_count = len(self.rubber_band_z_tracker[rubber_band])
            del self.rubber_band_z_tracker[rubber_band]
            QgsMessageLog.logMessage(
                f"Z-TRACKER: Tracker manually cleared with {z_count} Z values", 
                "SWM-3D", Qgis.Info
            )
            return True
        return False

    def _transform_geometry(self, geom: QgsGeometry, source_rubber_band: QgsRubberBand) -> Optional[QgsGeometry]:
        """
        Transforms geometry using the 3D perspective projection.
        Uses the same math functions from the expressions module (perspective_swm_transform),
        including the parameter-parsing cache.
        Input geometry must already contain assigned Z values (from the Z-tracker).
        """
        try:
            if not self.trf_wld2prp or not geom or geom.isEmpty():
                return geom

            # Geometry cache: avoid reprocessing unchanged geometry
            current_wkt = geom.asWkt()
            if source_rubber_band in self.geometry_cache:
                if self.geometry_cache[source_rubber_band] == current_wkt:
                    return geom
            self.geometry_cache[source_rubber_band] = current_wkt

            # Transformation parameters with module-level internal cache
            x0, y0, z0, df, r = read_perspective(self.trf_wld2prp.txt_perspective)
            a, b, c = read_projective(self.trf_wld2prp.txt_projective)

            gtype = QgsWkbTypes.geometryType(geom.wkbType())

            # ---- Point ----
            if gtype == QgsWkbTypes.PointGeometry:
                p = next(geom.vertices(), None)
                if p is None:
                    return geom
                z = p.z()
                if not math.isfinite(z):
                    return geom
                res = world_to_photo(p.x(), p.y(), z, x0, y0, z0, df, r)
                if not res:
                    return geom
                res = photo_to_proj(res[0], res[1], a, b, c)
                if not res:
                    return geom
                return QgsGeometry.fromPointXY(QgsPointXY(res[0], res[1]))

            # ---- Line ----
            elif gtype == QgsWkbTypes.LineGeometry:
                new_line = []
                const_geom = geom.constGet()
                if const_geom is None:
                    return geom
                for i in range(const_geom.vertexCount()):
                    p = geom.vertexAt(i)
                    z = p.z()
                    if not math.isfinite(z):
                        continue
                    res = world_to_photo(p.x(), p.y(), z, x0, y0, z0, df, r)
                    if not res:
                        continue
                    res = photo_to_proj(res[0], res[1], a, b, c)
                    if not res:
                        continue
                    new_line.append(QgsPointXY(res[0], res[1]))
                if len(new_line) < 2:
                    return geom
                return QgsGeometry.fromPolylineXY(new_line)

            # ---- Polygon ----
            elif gtype == QgsWkbTypes.PolygonGeometry:
                ring = []
                const_geom = geom.constGet()
                if const_geom is None:
                    return geom
                for i in range(const_geom.vertexCount()):
                    p = geom.vertexAt(i)
                    z = p.z()
                    if not math.isfinite(z):
                        continue
                    res = world_to_photo(p.x(), p.y(), z, x0, y0, z0, df, r)
                    if not res:
                        continue
                    res = photo_to_proj(res[0], res[1], a, b, c)
                    if not res:
                        continue
                    ring.append(QgsPointXY(res[0], res[1]))
                if len(ring) < 3:
                    return geom
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                return QgsGeometry.fromPolygonXY([ring])

            return geom

        except Exception as e:
            QgsMessageLog.logMessage(f"Error transforming geometry: {str(e)}", "SWM-3D", Qgis.Warning)
            return geom

    def force_sync_canvas_items(self):
        """
        Forces immediate synchronization of all map canvas items.
        Public method intended to be called externally when needed.
        """
        # Prevent multiple rapid calls from sync_layers
        if self.sync_in_progress:
            QgsMessageLog.logMessage(f"SYNC: Skipping forced sync (in progress) - Canvas {'LEFT' if self.is_left else 'RIGHT'}", "SWM-3D", Qgis.Info)
            return
            
        QgsMessageLog.logMessage(f"SYNC: Forcing immediate synchronization - Canvas {'LEFT' if self.is_left else 'RIGHT'}", "SWM-3D", Qgis.Warning)
        self._sync_canvas_items()

    def set_canvas_items_sync_enabled(self, enabled: bool):
        """
        Enables or disables automatic map canvas item synchronization.
        """
        if enabled:
            # Reconnect signals if needed
            self._setup_canvas_items_sync()
        else:
            # Disconnect signals to disable synchronization
            try:
                if hasattr(self.qgis_main_canvas, 'mapCanvasRefreshed'):
                    self.qgis_main_canvas.mapCanvasRefreshed.disconnect(self._sync_canvas_items)
                
                if hasattr(self.qgis_main_canvas, 'scene') and self.qgis_main_canvas.scene():
                    scene = self.qgis_main_canvas.scene()
                    if hasattr(scene, 'changed'):
                        scene.changed.disconnect(self._on_scene_changed)
            except RuntimeError:
                pass  # Signals may not be connected

    def cleanup_canvas_items_sync(self):
        """
        Cleans up all resources related to canvas-item synchronization.
        Must be called when closing or destroying the canvas.
        """
        # Disconnect signals
        try:
            if hasattr(self.qgis_main_canvas, 'mapCanvasRefreshed'):
                self.qgis_main_canvas.mapCanvasRefreshed.disconnect(self._sync_canvas_items)
            
            if hasattr(self.qgis_main_canvas, 'scene') and self.qgis_main_canvas.scene():
                scene = self.qgis_main_canvas.scene()
                if hasattr(scene, 'changed'):
                    scene.changed.disconnect(self._on_scene_changed)
        except RuntimeError:
            pass  # Signals may not be connected
        
        # Clear all synchronized items
        for synced_item in self.synced_items.values():
            try:
                if hasattr(synced_item, 'hide'):
                    synced_item.hide()
                self._safe_remove_item(synced_item)
            except Exception:
                pass  # Ignore cleanup errors
        
        self.synced_items.clear()
        self.geometry_cache.clear()
        
        # Clear parallel Z-tracking system
        tracked_rubber_bands_count = len(self.rubber_band_z_tracker)
        self.rubber_band_z_tracker.clear()
        if tracked_rubber_bands_count > 0:
            QgsMessageLog.logMessage(
                f"Z-TRACKER: Cleared {tracked_rubber_bands_count} rubber band trackers", 
                "SWM-3D", Qgis.Info
            )
        
        self.sync_in_progress = False

    def _safe_remove_item(self, item):
        """
        Removes an item from the canvas safely, avoiding Qt errors.
        """
        try:
            # Verify item exists and has a valid scene
            if not item:
                return
                
            item_scene = None
            if hasattr(item, 'scene'):
                item_scene = item.scene()
            
            # If item has no scene, there is nothing to remove
            if not item_scene:
                return
                
            # Verify item scene matches our scene
            canvas_scene = self.scene() if hasattr(self, 'scene') else None
            if canvas_scene and item_scene == canvas_scene:
                canvas_scene.removeItem(item)
            elif item_scene:
                # If scenes differ, remove from the item's scene
                item_scene.removeItem(item)
                
        except Exception as e:
            # Silence Qt errors related to scene management
            pass

    # ============================================================================
    # == End of Map Canvas Item Synchronization ==
    # ============================================================================

    def wheelEvent(self, event: QWheelEvent):  # type: ignore[override]
        """
        Ignore mouse wheel events on the stereo canvas. Wheel interaction is handled globally by the main window.
        """
        event.accept()   # consume the event
        return           # do not call super()

    # ============================================================================
    # == End of cursor handling in stereo canvas ==
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
        
        # Draw Z text if available
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
        """Updates the Z text shown in the canvas."""
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
        Cursor movement event sent by the parent.
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
                # This triggers an initial server request (GETCAPABILITIES)
                self.layer_swm = QgsRasterLayer(sigrid_layer_self_url, style_value, 'wms')            
                layers_self.append(self.layer_swm)  
            elif is_z_layer(layer_main):
                # Layer has Z values. Must apply Geometry Generator
                # Copy layer_main to apply Geometry Generator. Ensure the CRS and other properties are the same
                # 1) Create an independent logical view for the secondary canvas.
                layer_copy = QgsVectorLayer(layer_main.source(), layer_main.name(), layer_main.providerType())
                # Update (only once: is_left). Not sure if required. Disabled for now.
                # if self.is_left:
                #     layer_main.rendererChanged.connect(lambda: self.parent.trigger_sync_renderer_layerz(layer_copy.name()))
                # 2) Copy all styles from the original layer
                symbol = layer_main.renderer().symbol().clone()
                if symbol is None:
                    QgsMessageLog.logMessage(f"SYNC_LAYER Layer: {layer_main.name()}-{'LEFT' if self.is_left else 'RIGHT'}. "
                                             f"Style could not be interpreted.", "SWM-3D", Qgis.Error)
                    continue
                # 3) Create an initial placeholder Geometry Generator since perspective/projection is not known yet
                # A new expression will be created later once transformation data is available.
                # This dummy expression renders the layer in 2D without transformation (points render too, but without Z)
                symbol_layer = QgsGeometryGeneratorSymbolLayer.create({'geometryModifier': '$geometry'})
                if symbol_layer is None:
                    continue
                symbol_layer.setSubSymbol(symbol)
                # 4) Replace symbol layer (layer 0)
                final_symbol = QgsSymbol.defaultSymbol(layer_main.geometryType())
                if final_symbol is None:
                    continue
                final_symbol.changeSymbolLayer(0, symbol_layer) 
                # 5) Assign renderer
                renderer = QgsSingleSymbolRenderer(final_symbol)
                layer_copy.setRenderer(renderer) 
                if layer_main.hasScaleBasedVisibility():
                    # TODO: With glasses is not working. Real scale in glasses?
                    layer_copy.setScaleBasedVisibility(True)
                    layer_copy.setMinimumScale(layer_main.minimumScale())
                    layer_copy.setMaximumScale(layer_main.maximumScale())
                self.layers_z.append(layer_copy)
                layers_self.append(layer_copy) 
                # QgsMessageLog.logMessage(f"SYNC_LAYER Layer: {layer_main.name()}-{'LEFT' if self.is_left else 'RIGHT'}.", 
                #                          "SWM-3D", Qgis.Info)
            else:
                # Keep layers that are neither SWM nor Z-enabled
                layers_self.append(layer_main)

        self.setLayers(layers_self)
        
        # Force map canvas item synchronization after changing layers
        self.force_sync_canvas_items()

    def update_data_from_wms_header(self, reply):
        """
        Update photogrammetric transformation parameters from a SWM WMS reply
        and store them as layer custom properties so they can be consumed
        by Geometry Generator expressions.
        This function is critical because it prepares the Geometry Generator
        in Z-enabled layers to apply the photogrammetric transformation.
        IMPORTANT: if sync_layers is called without passing through here,
        Geometry Generator keeps an empty transformation and nothing is rendered
        in the secondary stereo canvas.
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

        # Update Geometry Generator for Z layers. Now transformation is known,
        # so Z layers (if present) can be updated to transform their geometries.
        for layer in self.layers():
            if is_z_layer(layer):
                layer.setCustomProperty("swm_trf_wrl2pht", txt_trf_wrl2pht)
                layer.setCustomProperty("swm_trf_pht2prp", txt_trf_pht2prp)
                # GeometryGenerator must be updated now that transformation is available
                side = 'left' if self.is_left else 'right'
                expression = (f"perspective_swm_transform($geometry,'{side}','{self.trf_wld2prp.txt_perspective}','{self.trf_wld2prp.txt_projective}')")
                symbol_layer = layer.renderer().symbol().symbolLayer(0)
                if not isinstance(symbol_layer, QgsGeometryGeneratorSymbolLayer):
                    QgsMessageLog.logMessage(f"Unexpected SymbolLayer type in layer {layer.name()}: {type(symbol_layer)}", "SWM-3D", Qgis.Warning)
                    continue
                symbol_layer.setGeometryExpression(expression)

                # QgsMessageLog.logMessage(f"UPDATE_SWM_HEADER Layer: {layer.name()}-{'LEFT' if self.is_left else 'RIGHT'}.", 
                #                          "SWM-3D", Qgis.Info)   
        self.render_complete()
