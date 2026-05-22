import os
import numpy as np
"""
canvas.py

Custom QGIS map canvas for the Sigrid SWM plugin.


This module implements QgsSgdSwmCanvas, a specialized canvas class for stereoscopic visualization.
The mirror canvas synchronizes:
    - Extent, layers, and cursor with the main canvas
    - Items of type QgsMapCanvasItem (including standard QgsRubberBand and QgsVertexMarker)
    - Stereo filters and 3D transformations

Known limitation:
    - Rubber bands from standard tools (such as Measure) are synchronized and visible in the stereo canvases.
    - Temporary rubber bands from digitizing tools (polyline, polygon, etc.) are NOT synchronized nor accessible, because QGIS manages them privately inside QgsMapToolCapture and does not expose them as QgsMapCanvasItem in the main scene.
    - Therefore, the last in-progress segment during digitizing will not be visible in the stereo canvases until the vertex is completed.

This limitation is structural in QGIS and cannot be solved generically or stably without fragile, tool-specific code.

The canvas does not handle network requests, WMS headers, or mathematical transformation parsing; those responsibilities belong to the window controller and expression functions.

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
from qgis.core import QgsWkbTypes, QgsGeometry, QgsRasterLayer, QgsVectorLayer, QgsPoint, QgsPointXY, QgsFeatureRequest
from qgis.core import QgsSymbol, QgsSingleSymbolRenderer, QgsGeometryGeneratorSymbolLayer
from qgis.core import QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsProject
from qgis.PyQt.QtGui import QColor, QWheelEvent, QImage, QPainter
from qgis.PyQt.QtCore import Qt, QTimer
from typing import Optional, Any, Dict, List, Tuple
import hashlib

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

    def _debug_save_channels(self, arr, prefix):
        """Guarda canales de un array de imagen como PNG en disco para depuración."""
        try:
            from PIL import Image
            outdir = os.path.join(os.path.expanduser("~"), "swm_debug")
            os.makedirs(outdir, exist_ok=True)
            labels = ['R', 'G', 'B', 'A'] if arr.shape[2] >= 4 else ['R', 'G', 'B']
            for i, ch in enumerate(labels):
                img = Image.fromarray(arr[:, :, i])
                img.save(os.path.join(outdir, f"{prefix}_ch{ch}.png"))
        except Exception as e:
            from qgis.core import QgsMessageLog
            QgsMessageLog.logMessage(f"[STEREO] Error saving debug channels: {e}", "StereoWebMap")

    def __init__(self, is_left: bool, qgis_main_canvas, filter: int = FILTER_NONE, parent: Optional[Any] = None):
        super(QgsSgdSwmCanvas, self).__init__(parent)
 
        self.parent = parent
        self.qgis_main_canvas = qgis_main_canvas
        self.is_left = is_left
        self.filter = filter

        # Transformation world to projection plane
        self.trf_wld2prp = None

        # Cursor marker (must be created before item synchronization)
        # Two overlapped markers create a black outline with white center.
        self.cursor_marker = QgsVertexMarker(self)
        self.cursor_marker.setColor(QColor(Qt.GlobalColor.black))
        self.cursor_marker.setIconSize(10)
        self.cursor_marker.setIconType(QgsVertexMarker.ICON_CROSS)
        self.cursor_marker.setPenWidth(5)

        self.cursor_marker_inner = QgsVertexMarker(self)
        self.cursor_marker_inner.setColor(QColor(Qt.GlobalColor.white))
        self.cursor_marker_inner.setIconSize(10)
        self.cursor_marker_inner.setIconType(QgsVertexMarker.ICON_CROSS)
        self.cursor_marker_inner.setPenWidth(2)

        # Map canvas items synchronization (after creating cursor_marker)
        self.synced_items: Dict[QgsMapCanvasItem, QgsMapCanvasItem] = {}  # main_item -> synced_item
        self.geometry_cache: Dict[QgsMapCanvasItem, str] = {}  # rubber_band -> geometry_wkt to avoid duplicates
        self.sync_in_progress = False  # Prevent concurrent synchronizations
        # Per-rubber-band fixed-vertex Z values for stereo visualization.
        # The dynamic last vertex (if any) uses current cursor Z.
        self.rubber_band_fixed_z: Dict[QgsMapCanvasItem, List[float]] = {}
        self.rubber_band_last_dynamic_z: Dict[QgsMapCanvasItem, float] = {}
        self.rubber_band_z_by_geom_hash: Dict[str, List[float]] = {}
        self.vertex_marker_fixed_z: Dict[QgsMapCanvasItem, float] = {}
        self.vertex_marker_last_center: Dict[QgsMapCanvasItem, QgsPointXY] = {}
        self.vertex_marker_rb_match: Dict[QgsMapCanvasItem, Tuple[QgsRubberBand, int]] = {}
        
        self._setup_canvas_items_sync()

        self.layer_swm = None
        self.layers_z = []
        self._swm_layer_cache: Dict[str, QgsRasterLayer] = {}
        self._z_layer_cache: Dict[str, QgsVectorLayer] = {}
        self.limits = None
        self.z_text = ""  # Z cursor text
        self._last_rendered_buffer: Optional[QImage] = None
        self._last_base_buffer: Optional[QImage] = None

        overlay_color = QColor(Qt.GlobalColor.white) if self.filter != self.FILTER_NONE else QColor(0, 0, 0, 0)
        self.setCanvasColor(overlay_color)
        # Keep rendered layer images cached so vector-only visibility toggles
        # do not force unnecessary WMS requests when extent is unchanged.
        self.setCachingEnabled(True)

        # In filtered (overlay) modes our paintEvent lives on the canvas widget,
        # not on the viewport.  QgsMapCanvas completes its async tile render by
        # calling viewport().update(), which bypasses our paintEvent and leaves
        # the viewport with unfiltered content.  Re-running our paintEvent after
        # every completed render ensures the filter (interlaced / anaglyph) is
        # always applied.
        if self.filter != self.FILTER_NONE:
            self.mapCanvasRefreshed.connect(self.update)
            self.mapCanvasRefreshed.connect(self._schedule_base_capture)

    def _is_stereo_projection_active(self) -> bool:
        """Delegates stereo activation to the parent window when available."""
        try:
            if self.parent and hasattr(self.parent, '_is_stereo_projection_active'):
                return bool(self.parent._is_stereo_projection_active())
        except Exception:
            pass
        return True

    def _schedule_base_capture(self):
        """Schedule raw viewport capture after Qt finishes repainting."""
        QTimer.singleShot(0, self._capture_base_buffer)

    def _capture_base_buffer(self):
        """Capture the latest raw map image from viewport (no custom paintEvent post-processing)."""
        try:
            viewport = self.viewport()
            if viewport is None:
                return
            pix = viewport.grab()
            if pix and not pix.isNull():
                self._last_base_buffer = pix.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        except Exception:
            pass

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
        point_wrl = self._reproject_point_to_world(point_xy)
        
        # Calculate projected position
        if self.trf_wld2prp and self._is_stereo_projection_active():
            pnt_wrl = QgsPoint(point_wrl.x(), point_wrl.y(), z)
            pnt_prj = self.trf_wld2prp.execute_wrl2prp(pnt_wrl)
            if pnt_prj:
                self.cursor_marker.setCenter(pnt_prj)
                self.cursor_marker_inner.setCenter(pnt_prj)
                self.cursor_marker.show()
                self.cursor_marker_inner.show()
                if self.filter != self.FILTER_NONE:
                    self.update()
                return

        self.cursor_marker.setCenter(point_xy)
        self.cursor_marker_inner.setCenter(point_xy)
        self.cursor_marker.show()
        self.cursor_marker_inner.show()
        if self.filter != self.FILTER_NONE:
            self.update()

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
        
        # Startup sync configured.

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
            main_items.sort(key=self._canvas_item_sync_priority)
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

                    if main_item in self.rubber_band_fixed_z:
                        del self.rubber_band_fixed_z[main_item]
                    if main_item in self.rubber_band_last_dynamic_z:
                        del self.rubber_band_last_dynamic_z[main_item]
                    if main_item in self.vertex_marker_fixed_z:
                        del self.vertex_marker_fixed_z[main_item]
                    if main_item in self.vertex_marker_last_center:
                        del self.vertex_marker_last_center[main_item]
                    if main_item in self.vertex_marker_rb_match:
                        del self.vertex_marker_rb_match[main_item]
                        

            
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

    def _canvas_item_sync_priority(self, item: QgsMapCanvasItem) -> int:
        """
        Synchronization order: rubber bands first, then vertex markers, then the rest.
        This guarantees marker Z lookup can use up-to-date tracked rubber-band vertices.
        """
        if isinstance(item, QgsRubberBand):
            return 0
        if isinstance(item, QgsVertexMarker):
            return 1
        return 2

    def _get_world_crs(self) -> Optional[QgsCoordinateReferenceSystem]:
        """
        Returns the "world" CRS expected by the photogrammetric transform.
        Uses the SWM layer CRS when available.
        """
        if self.layer_swm:
            swm_crs = self.layer_swm.crs()
            if swm_crs and swm_crs.isValid():
                return swm_crs
        return None

    def _reproject_point_to_world(self, point_xy: QgsPointXY) -> QgsPointXY:
        """
        Reprojects a point from main canvas destination CRS to SWM world CRS.
        """
        try:
            world_crs = self._get_world_crs()
            if not world_crs:
                return point_xy

            source_crs = self.qgis_main_canvas.mapSettings().destinationCrs()
            if not source_crs or not source_crs.isValid() or source_crs == world_crs:
                return point_xy

            trf = QgsCoordinateTransform(source_crs, world_crs, QgsProject.instance())
            return trf.transform(point_xy)
        except Exception as e:
            QgsMessageLog.logMessage(f"CRS: Error reprojecting cursor point to SWM CRS: {str(e)}",
                                     "SWM-3D", Qgis.Warning)
            return point_xy

    def _reproject_geometry_to_world(self, geom: QgsGeometry) -> QgsGeometry:
        """
        Reprojects geometry from main canvas destination CRS to SWM world CRS.
        """
        try:
            world_crs = self._get_world_crs()
            if not world_crs:
                return geom

            source_crs = self.qgis_main_canvas.mapSettings().destinationCrs()
            if not source_crs or not source_crs.isValid() or source_crs == world_crs:
                return geom

            trf = QgsCoordinateTransform(source_crs, world_crs, QgsProject.instance())
            transformed = QgsGeometry(geom)
            source_z_values: List[float] = []
            const_geom = geom.constGet()
            if const_geom:
                for i in range(const_geom.vertexCount()):
                    source_z_values.append(float(geom.vertexAt(i).z()))

            if transformed.transform(trf) == 0:
                # QGIS CRS transforms can flatten Z on temporary geometries.
                # Re-attach the original per-vertex Z values so downstream 3D projection
                # always receives the same Z that was already captured for each vertex.
                if source_z_values:
                    transformed_const = transformed.constGet()
                    if transformed_const:
                        for i in range(min(len(source_z_values), transformed_const.vertexCount())):
                            v = transformed.vertexAt(i)
                            if math.isfinite(source_z_values[i]):
                                v.setZ(source_z_values[i])
                                transformed.moveVertex(v, i)
                return transformed
            return geom
        except Exception as e:
            QgsMessageLog.logMessage(f"CRS: Error reprojecting geometry to SWM CRS: {str(e)}",
                                     "SWM-3D", Qgis.Warning)
            return geom

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
                        # Exclude our own cursor markers to avoid recursion
                        if hasattr(self, 'cursor_marker') and item == self.cursor_marker:
                            continue
                        if hasattr(self, 'cursor_marker_inner') and item == self.cursor_marker_inner:
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
                QgsMessageLog.logMessage(f"Unmanaged item type for synchronization: {type(main_item)}", 
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
        Applies dynamic center-of-vertex offset based on marker symbol size.
        """
        try:
            # Copy basic properties safely
            # Check getter method availability before using them
            icon_size = 10  # Default size
            if hasattr(source, 'color'):
                target.setColor(source.color())
            
            # For iconSize, iconType and penWidth, some getters may not be available
            # If value retrieval fails, use reasonable defaults
            try:
                if hasattr(source, 'iconSize'):
                    icon_size = source.iconSize()
                    target.setIconSize(icon_size)
                else:
                    target.setIconSize(icon_size)
            except AttributeError:
                target.setIconSize(icon_size)
                
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
            if center and self.trf_wld2prp and self._is_stereo_projection_active():
                matched_center = self._get_vertex_marker_center_from_synced_rubber_band(source, QgsPointXY(center.x(), center.y()))
                if matched_center is not None:
                    target.setCenter(matched_center)
                    return

                # Apply 3D transformation if available
                z = self._resolve_vertex_marker_z(source, center)
                center_wrl = self._reproject_point_to_world(QgsPointXY(center.x(), center.y()))
                pnt_prj = self._project_world_point_with_expression_math(center_wrl.x(), center_wrl.y(), z)
                if pnt_prj:
                    # Apply dynamic center-of-vertex offset based on symbol size
                    # Offset is proportional to half the symbol width
                    # Formula: offset = iconSize / 20 (iconSize 10 → 0.5, iconSize 20 → 1.0, etc.)
                    offset_value = icon_size / 20.0
                    offset_x = -offset_value
                    offset_y = offset_value
                    
                    adjusted_point = QgsPointXY(pnt_prj.x() + offset_x, pnt_prj.y() + offset_y)
                    target.setCenter(adjusted_point)
                else:
                    target.setCenter(center)
            else:
                target.setCenter(center)
                
        except Exception as e:
            QgsMessageLog.logMessage(f"Error synchronizing vertex marker: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)

    def _get_vertex_marker_center_from_synced_rubber_band(self, source_marker: QgsVertexMarker, center: QgsPointXY) -> Optional[QgsPointXY]:
        """
        Deterministic marker placement: if a marker matches a fixed rubber-band vertex,
        return the already-transformed vertex position from the synced rubber band.
        """
        try:
            match = self._match_vertex_marker_to_rubber_band_vertex(source_marker, center)
            if not match:
                return None

            rb_source, idx = match
            rb_synced = self.synced_items.get(rb_source)
            if not isinstance(rb_synced, QgsRubberBand):
                return None

            geom_synced = rb_synced.asGeometry()
            if not geom_synced or geom_synced.isEmpty():
                return None
            const_geom = geom_synced.constGet()
            if not const_geom or idx < 0 or idx >= const_geom.vertexCount():
                return None

            v = geom_synced.vertexAt(idx)
            return QgsPointXY(v.x(), v.y())
        except Exception:
            return None

    def _match_vertex_marker_to_rubber_band_vertex(self, source_marker: QgsVertexMarker, center: QgsPointXY) -> Optional[Tuple[QgsRubberBand, int]]:
        """
        Finds/validates stable match of marker XY to fixed rubber-band vertex index.
        """
        try:
            if not self.qgis_main_canvas:
                return None

            tol = max(float(self.qgis_main_canvas.mapUnitsPerPixel()) * 10.0, 1e-7)
            tol2 = tol * tol

            existing = self.vertex_marker_rb_match.get(source_marker)
            if existing:
                rb_item, idx = existing
                fixed_z = self.rubber_band_fixed_z.get(rb_item)
                geom = rb_item.asGeometry() if isinstance(rb_item, QgsRubberBand) else None
                if not geom or geom.isEmpty():
                    geom = None
                const_geom = geom.constGet() if geom and not geom.isEmpty() else None
                if fixed_z and geom and const_geom and idx < min(len(fixed_z), const_geom.vertexCount()):
                    v = geom.vertexAt(idx)
                    dx = v.x() - center.x()
                    dy = v.y() - center.y()
                    if (dx * dx + dy * dy) <= (tol2 * 4.0):
                        return (rb_item, idx)

            best_d2 = float('inf')
            best_match: Optional[Tuple[QgsRubberBand, int]] = None

            for rb_item, fixed_z in self.rubber_band_fixed_z.items():
                if not isinstance(rb_item, QgsRubberBand) or not fixed_z:
                    continue
                geom = rb_item.asGeometry()
                if not geom or geom.isEmpty():
                    continue
                const_geom = geom.constGet()
                if not const_geom:
                    continue

                fixed_count = min(len(fixed_z), const_geom.vertexCount())
                for i in range(fixed_count):
                    v = geom.vertexAt(i)
                    dx = v.x() - center.x()
                    dy = v.y() - center.y()
                    d2 = dx * dx + dy * dy
                    if d2 < best_d2:
                        best_d2 = d2
                        best_match = (rb_item, i)

            if best_match and best_d2 <= tol2:
                self.vertex_marker_rb_match[source_marker] = best_match
                return best_match

            return None
        except Exception:
            return None

    def _project_world_point_with_expression_math(self, x: float, y: float, z: float) -> Optional[QgsPointXY]:
        """
        Projects one world XYZ point using the same math path as geometry transformation.
        This avoids divergence between marker and rubber-band projection routes.
        """
        try:
            if not self.trf_wld2prp:
                return None

            x0, y0, z0, df, r = read_perspective(self.trf_wld2prp.txt_perspective)
            a, b, c = read_projective(self.trf_wld2prp.txt_projective)

            photo = world_to_photo(x, y, z, x0, y0, z0, df, r)
            if not photo:
                return None
            proj = photo_to_proj(photo[0], photo[1], a, b, c)
            if not proj:
                return None
            return QgsPointXY(proj[0], proj[1])
        except Exception:
            return None

    def _resolve_vertex_marker_z(self, source: QgsVertexMarker, center: QgsPointXY) -> float:
        """
        Resolves a stable Z for tool-generated vertex markers.
        Clicked vertices keep their capture-time Z instead of following cursor Z.
        """
        try:
            matched_z = self._resolve_vertex_marker_z_from_rubber_band(source, center)
            if matched_z is not None:
                self.vertex_marker_fixed_z[source] = float(matched_z)
                self.vertex_marker_last_center[source] = QgsPointXY(center.x(), center.y())
                return float(matched_z)

            rb_z = self._get_marker_z_from_tracked_rubber_band(center)
            if rb_z is not None:
                self.vertex_marker_fixed_z[source] = float(rb_z)
                self.vertex_marker_last_center[source] = QgsPointXY(center.x(), center.y())
                return float(rb_z)

            # If center has changed, treat as new marker position and refresh fixed Z.
            previous_center = self.vertex_marker_last_center.get(source)
            moved = True
            if previous_center is not None:
                dx = center.x() - previous_center.x()
                dy = center.y() - previous_center.y()
                moved = (dx * dx + dy * dy) > 1e-18

            if moved or source not in self.vertex_marker_fixed_z:
                new_z = float(getattr(self.parent, 'z_cursor', 0.0)) if self.parent else 0.0
                self.vertex_marker_fixed_z[source] = new_z
                self.vertex_marker_last_center[source] = QgsPointXY(center.x(), center.y())

            return float(self.vertex_marker_fixed_z.get(source, 0.0))
        except Exception:
            return float(getattr(self.parent, 'z_cursor', 0.0)) if self.parent else 0.0

    def _resolve_vertex_marker_z_from_rubber_band(self, source: QgsVertexMarker, center: QgsPointXY) -> Optional[float]:
        """
        Stable marker-to-rubber-band matching by geometry (no timing).
        Each marker is matched to a fixed rubber-band vertex index and reuses its Z.
        """
        try:
            if not self.qgis_main_canvas:
                return None

            tol = max(float(self.qgis_main_canvas.mapUnitsPerPixel()) * 10.0, 1e-7)
            tol2 = tol * tol

            # Reuse existing match when still valid.
            existing = self.vertex_marker_rb_match.get(source)
            if existing:
                rb_item, idx = existing
                fixed_z = self.rubber_band_fixed_z.get(rb_item)
                if fixed_z and idx < len(fixed_z):
                    geom = rb_item.asGeometry()
                    if geom and not geom.isEmpty():
                        const_geom = geom.constGet()
                        if const_geom and idx < const_geom.vertexCount():
                            v = geom.vertexAt(idx)
                            dx = v.x() - center.x()
                            dy = v.y() - center.y()
                            if (dx * dx + dy * dy) <= (tol2 * 4.0):
                                return float(fixed_z[idx])

            # Find best new match among all fixed rubber-band vertices.
            best_d2 = float('inf')
            best_rb: Optional[QgsRubberBand] = None
            best_idx = -1
            best_z: Optional[float] = None

            for rb_item, fixed_z in self.rubber_band_fixed_z.items():
                if not isinstance(rb_item, QgsRubberBand) or not fixed_z:
                    continue
                geom = rb_item.asGeometry()
                if not geom or geom.isEmpty():
                    continue
                const_geom = geom.constGet()
                if not const_geom:
                    continue

                fixed_count = min(len(fixed_z), const_geom.vertexCount())
                for i in range(fixed_count):
                    v = geom.vertexAt(i)
                    dx = v.x() - center.x()
                    dy = v.y() - center.y()
                    d2 = dx * dx + dy * dy
                    if d2 < best_d2:
                        best_d2 = d2
                        best_rb = rb_item
                        best_idx = i
                        best_z = float(fixed_z[i])

            if best_rb is not None and best_idx >= 0 and best_z is not None and best_d2 <= tol2:
                self.vertex_marker_rb_match[source] = (best_rb, best_idx)
                return best_z

            return None
        except Exception:
            return None

    def _get_marker_z_from_tracked_rubber_band(self, center: QgsPointXY) -> Optional[float]:
        """
        Returns Z from the nearest fixed rubber-band vertex that matches marker XY.
        """
        try:
            if not self.qgis_main_canvas:
                return None

            tol = max(float(self.qgis_main_canvas.mapUnitsPerPixel()) * 6.0, 1e-7)
            tol2 = tol * tol
            best_d2 = float('inf')
            best_z: Optional[float] = None

            for rb_item, fixed_z in self.rubber_band_fixed_z.items():
                if not isinstance(rb_item, QgsRubberBand) or not fixed_z:
                    continue

                geom = rb_item.asGeometry()
                if not geom or geom.isEmpty():
                    continue

                const_geom = geom.constGet()
                if not const_geom:
                    continue

                fixed_count = min(len(fixed_z), const_geom.vertexCount())
                for i in range(fixed_count):
                    v = geom.vertexAt(i)
                    dx = v.x() - center.x()
                    dy = v.y() - center.y()
                    d2 = dx * dx + dy * dy
                    if d2 <= tol2 and d2 < best_d2:
                        best_d2 = d2
                        best_z = float(fixed_z[i])

            return best_z
        except Exception:
            return None

    def _sync_rubber_band_properties(self, source: QgsRubberBand, target: QgsRubberBand):
        """
        Synchronizes QgsRubberBand properties and geometry.
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
            
            # Copy geometry
            geom = source.asGeometry()
            if geom and not geom.isEmpty():
                geom_for_render = QgsGeometry(geom)
                const_geom = geom_for_render.constGet()
                if const_geom:
                    vertex_count = const_geom.vertexCount()
                    if vertex_count > 0 and self.parent:
                        cursor_z = float(getattr(self.parent, 'z_cursor', 0.0))
                        dynamic_last = self._rubber_band_has_dynamic_last_vertex(geom_for_render)
                        fixed_vertex_count = vertex_count - 1 if (dynamic_last and vertex_count > 1) else vertex_count

                        tracked_fixed_z = self.rubber_band_fixed_z.setdefault(source, [])

                        # Trim tracker if geometry shrank.
                        if len(tracked_fixed_z) > fixed_vertex_count:
                            tracked_fixed_z[:] = tracked_fixed_z[:fixed_vertex_count]

                        # Capture Z for newly fixed vertices only.
                        while len(tracked_fixed_z) < fixed_vertex_count:
                            new_idx = len(tracked_fixed_z)
                            pending_click_z = self._get_pending_click_z_for_vertex(new_idx)
                            src_v = geom.vertexAt(new_idx)
                            src_z = src_v.z()
                            if pending_click_z is not None:
                                tracked_fixed_z.append(float(pending_click_z))
                            elif math.isfinite(src_z) and abs(src_z) > 1e-9:
                                tracked_fixed_z.append(float(src_z))
                            elif new_idx > 0 and source in self.rubber_band_last_dynamic_z:
                                tracked_fixed_z.append(float(self.rubber_band_last_dynamic_z[source]))
                            else:
                                tracked_fixed_z.append(cursor_z)

                        # Apply fixed-vertex Z values.
                        for i in range(min(fixed_vertex_count, len(tracked_fixed_z))):
                            v = geom_for_render.vertexAt(i)
                            if v.z() != tracked_fixed_z[i]:
                                v.setZ(tracked_fixed_z[i])
                                geom_for_render.moveVertex(v, i)

                        # Keep the in-progress endpoint attached to current cursor Z.
                        if dynamic_last and vertex_count > 1:
                            last_idx = vertex_count - 1
                            v_last = geom_for_render.vertexAt(last_idx)
                            if v_last.z() != cursor_z:
                                v_last.setZ(cursor_z)
                                geom_for_render.moveVertex(v_last, last_idx)
                            self.rubber_band_last_dynamic_z[source] = cursor_z

                        # Publish fixed Z snapshot by geometry hash for final feature interception.
                        if not dynamic_last and fixed_vertex_count > 0:
                            geom_hash = self._geom_xy_hash(geom_for_render)
                            if geom_hash:
                                self.rubber_band_z_by_geom_hash[geom_hash] = tracked_fixed_z[:fixed_vertex_count].copy()
                                if len(self.rubber_band_z_by_geom_hash) > 256:
                                    oldest_key = next(iter(self.rubber_band_z_by_geom_hash.keys()))
                                    del self.rubber_band_z_by_geom_hash[oldest_key]

                geom_world = self._reproject_geometry_to_world(geom_for_render)
                
                if self.trf_wld2prp and self._is_stereo_projection_active():
                    transformed_geom = self._transform_geometry(geom_world, source)
                    if transformed_geom:
                        target.setToGeometry(transformed_geom, None)
                    else:
                        target.setToGeometry(geom_world, None)
                else:
                    target.setToGeometry(geom_world, None)
            
        except Exception as e:
            QgsMessageLog.logMessage(f"Error synchronizing rubber band: {str(e)}", 
                                   "SWM-3D", Qgis.Warning)

    def _get_pending_click_z_for_vertex(self, vertex_index: int) -> Optional[float]:
        """
        Returns the recorded cursor Z for the given vertex index of the active digitizing layer.
        This improves rubber-band rendering so fixed vertices keep their click-time Z.
        """
        try:
            if not self.parent or not hasattr(self.parent, 'iface'):
                return None
            iface = self.parent.iface
            if not iface or not hasattr(iface, 'activeLayer'):
                return None
            active_layer = iface.activeLayer()
            if not active_layer:
                return None
            layer_id = active_layer.id() if hasattr(active_layer, 'id') else None
            if not layer_id:
                return None

            pending_by_layer = getattr(self.parent, '_pending_digitize_z_clicks', None)
            if not isinstance(pending_by_layer, dict):
                return None
            pending = pending_by_layer.get(layer_id)
            if not isinstance(pending, list):
                return None
            if vertex_index < 0 or vertex_index >= len(pending):
                return None

            z_val = pending[vertex_index]
            return float(z_val) if math.isfinite(float(z_val)) else None
        except Exception:
            return None

    def _rubber_band_has_dynamic_last_vertex(self, geom: QgsGeometry) -> bool:
        """
        Heuristic for map-tool rubber bands (e.g., Measure):
        if last vertex is at current mouse map position, treat it as dynamic endpoint.
        """
        try:
            if not self.qgis_main_canvas:
                return False
            const_geom = geom.constGet()
            if not const_geom or const_geom.vertexCount() < 2:
                return False

            last_idx = const_geom.vertexCount() - 1
            last_v = geom.vertexAt(last_idx)

            mouse_px = self.qgis_main_canvas.mouseLastXY()
            mouse_map = self.qgis_main_canvas.getCoordinateTransform().toMapCoordinates(mouse_px)

            dx = last_v.x() - mouse_map.x()
            dy = last_v.y() - mouse_map.y()
            # Tolerance tied to canvas scale: ~4 screen pixels in map units.
            tol = max(float(self.qgis_main_canvas.mapUnitsPerPixel()) * 4.0, 1e-6)
            return (dx * dx + dy * dy) <= (tol * tol)
        except Exception:
            return False

    def _geom_xy_hash(self, geom: QgsGeometry) -> str:
        """
        Stable XY hash for vertex sequence (ignores Z).
        """
        try:
            if not geom or geom.isEmpty():
                return ""
            const_geom = geom.constGet()
            if not const_geom:
                return ""
            parts = []
            for i in range(const_geom.vertexCount()):
                v = geom.vertexAt(i)
                parts.append(f"{v.x():.8f},{v.y():.8f}")
            key = ";".join(parts)
            return hashlib.sha1(key.encode("utf-8")).hexdigest()
        except Exception:
            return ""

    def get_tracked_z_for_geometry(self, geom: QgsGeometry) -> List[float]:
        """
        Returns tracked fixed Z list for a geometry XY signature, if available.
        """
        geom_hash = self._geom_xy_hash(geom)
        if not geom_hash:
            return []
        z_vals = self.rubber_band_z_by_geom_hash.get(geom_hash)
        return z_vals.copy() if z_vals else []

    def _transform_geometry(self, geom: QgsGeometry, source_rubber_band: QgsRubberBand) -> Optional[QgsGeometry]:
        """
        Transforms geometry using the 3D perspective projection.
        Uses the same math functions from the expressions module (perspective_swm_transform),
        including the parameter-parsing cache.
        Input geometry must already contain assigned Z values.
        """
        try:
            if not self._is_stereo_projection_active() or not self.trf_wld2prp or not geom or geom.isEmpty():
                return geom

            # Geometry cache: include dynamic Z state because rubber-band geometry is often 2D.
            tracked_fixed = self.rubber_band_fixed_z.get(source_rubber_band, [])
            cursor_z = float(getattr(self.parent, 'z_cursor', 0.0)) if self.parent else 0.0
            current_wkt = geom.asWkt()
            z_signature = ",".join(f"{z:.6f}" for z in tracked_fixed)
            cache_key = f"{current_wkt}|{cursor_z:.6f}|{z_signature}"
            if source_rubber_band in self.geometry_cache:
                if self.geometry_cache[source_rubber_band] == cache_key:
                    # Important: never return raw input geometry on cache hit.
                    # During digitizing refreshes this would bypass 3D projection and flatten to plane.
                    pass
            self.geometry_cache[source_rubber_band] = cache_key

            # Transformation parameters with module-level internal cache
            x0, y0, z0, df, r = read_perspective(self.trf_wld2prp.txt_perspective)
            a, b, c = read_projective(self.trf_wld2prp.txt_projective)

            gtype = QgsWkbTypes.geometryType(geom.wkbType())

            # ---- Point / MultiPoint ----
            if gtype == QgsWkbTypes.PointGeometry:
                transformed_points = []
                const_geom = geom.constGet()
                if const_geom is None:
                    return geom
                vertex_index = 0
                for i in range(const_geom.vertexCount()):
                    p = geom.vertexAt(i)
                    z = self._resolve_rubber_band_vertex_z(source_rubber_band, vertex_index, p.z())
                    vertex_index += 1
                    if not math.isfinite(z):
                        continue
                    res = world_to_photo(p.x(), p.y(), z, x0, y0, z0, df, r)
                    if not res:
                        continue
                    res = photo_to_proj(res[0], res[1], a, b, c)
                    if not res:
                        continue
                    transformed_points.append(QgsPointXY(res[0], res[1]))

                if not transformed_points:
                    return geom

                if QgsWkbTypes.isMultiType(geom.wkbType()) or len(transformed_points) > 1:
                    return QgsGeometry.fromMultiPointXY(transformed_points)

                return QgsGeometry.fromPointXY(transformed_points[0])

            # ---- Line / MultiLine ----
            elif gtype == QgsWkbTypes.LineGeometry:
                if QgsWkbTypes.isMultiType(geom.wkbType()):
                    # Keep each line part independent to avoid bridges between parts.
                    transformed_multi = []
                    source_multi = geom.asMultiPolyline()
                    vertex_index = 0
                    for line in source_multi:
                        transformed_line = []
                        for p in line:
                            z = self._resolve_rubber_band_vertex_z(source_rubber_band, vertex_index, float("nan"))
                            vertex_index += 1
                            if not math.isfinite(z):
                                continue
                            res = world_to_photo(p.x(), p.y(), z, x0, y0, z0, df, r)
                            if not res:
                                continue
                            res = photo_to_proj(res[0], res[1], a, b, c)
                            if not res:
                                continue
                            transformed_line.append(QgsPointXY(res[0], res[1]))
                        if len(transformed_line) >= 2:
                            transformed_multi.append(transformed_line)

                    if not transformed_multi:
                        return geom
                    return QgsGeometry.fromMultiPolylineXY(transformed_multi)

                new_line = []
                const_geom = geom.constGet()
                if const_geom is None:
                    return geom
                vertex_index = 0
                for i in range(const_geom.vertexCount()):
                    p = geom.vertexAt(i)
                    z = self._resolve_rubber_band_vertex_z(source_rubber_band, vertex_index, p.z())
                    vertex_index += 1
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

            # ---- Polygon / MultiPolygon ----
            elif gtype == QgsWkbTypes.PolygonGeometry:
                if QgsWkbTypes.isMultiType(geom.wkbType()):
                    # Preserve polygon and ring boundaries; flattening vertices would join parts.
                    transformed_multi = []
                    source_multi = geom.asMultiPolygon()
                    vertex_index = 0
                    for polygon in source_multi:
                        transformed_polygon = []
                        for ring in polygon:
                            transformed_ring = []
                            for p in ring:
                                z = self._resolve_rubber_band_vertex_z(source_rubber_band, vertex_index, float("nan"))
                                vertex_index += 1
                                if not math.isfinite(z):
                                    continue
                                res = world_to_photo(p.x(), p.y(), z, x0, y0, z0, df, r)
                                if not res:
                                    continue
                                res = photo_to_proj(res[0], res[1], a, b, c)
                                if not res:
                                    continue
                                transformed_ring.append(QgsPointXY(res[0], res[1]))

                            if len(transformed_ring) >= 3 and transformed_ring[0] != transformed_ring[-1]:
                                transformed_ring.append(transformed_ring[0])
                            if len(transformed_ring) >= 4:
                                transformed_polygon.append(transformed_ring)

                        if transformed_polygon:
                            transformed_multi.append(transformed_polygon)

                    if not transformed_multi:
                        return geom
                    return QgsGeometry.fromMultiPolygonXY(transformed_multi)

                transformed_polygon = []
                source_polygon = geom.asPolygon()
                vertex_index = 0
                for ring in source_polygon:
                    transformed_ring = []
                    for p in ring:
                        z = self._resolve_rubber_band_vertex_z(source_rubber_band, vertex_index, float("nan"))
                        vertex_index += 1
                        if not math.isfinite(z):
                            continue
                        res = world_to_photo(p.x(), p.y(), z, x0, y0, z0, df, r)
                        if not res:
                            continue
                        res = photo_to_proj(res[0], res[1], a, b, c)
                        if not res:
                            continue
                        transformed_ring.append(QgsPointXY(res[0], res[1]))

                    if len(transformed_ring) >= 3 and transformed_ring[0] != transformed_ring[-1]:
                        transformed_ring.append(transformed_ring[0])
                    if len(transformed_ring) >= 4:
                        transformed_polygon.append(transformed_ring)

                if not transformed_polygon:
                    return geom
                return QgsGeometry.fromPolygonXY(transformed_polygon)

            return geom

        except Exception as e:
            QgsMessageLog.logMessage(f"Error transforming geometry: {str(e)}", "SWM-3D", Qgis.Warning)
            return geom

    def _resolve_rubber_band_vertex_z(self, source_rubber_band: QgsRubberBand, vertex_index: int, raw_z: float) -> float:
        """
        Resolves Z for rubber-band vertex transformation.
        Rubber-band geometries are frequently 2D, so we prioritize tracked Z values.
        """
        try:
            if math.isfinite(raw_z) and abs(raw_z) > 1e-9:
                return float(raw_z)

            tracked_fixed = self.rubber_band_fixed_z.get(source_rubber_band, [])
            if vertex_index < len(tracked_fixed):
                return float(tracked_fixed[vertex_index])

            if self.parent:
                return float(getattr(self.parent, 'z_cursor', 0.0))
            return 0.0
        except Exception:
            return 0.0

    def force_sync_canvas_items(self):
        """
        Forces immediate synchronization of all map canvas items.
        Public method intended to be called externally when needed.
        """
        # Prevent multiple rapid calls from sync_layers
        if self.sync_in_progress:
            return

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
            except (RuntimeError, TypeError):
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
        except (RuntimeError, TypeError):
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
        self.rubber_band_fixed_z.clear()
        self.rubber_band_last_dynamic_z.clear()
        self.rubber_band_z_by_geom_hash.clear()
        self.vertex_marker_fixed_z.clear()
        self.vertex_marker_last_center.clear()
        self.vertex_marker_rb_match.clear()

        # Cleanup main-extent limits rubber band
        if self.limits:
            try:
                self.limits.hide()
                self._safe_remove_item(self.limits)
            except Exception:
                pass
            self.limits = None
        
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
        if self._is_overlay_mode():
            rendered = self._render_canvas_buffer()
            self._last_rendered_buffer = rendered
            composed = self._compose_overlay_image()
            if composed is None:
                # Fallback while opposite eye catches up.
                if self.parent and self.parent.stereo_id == 1:
                    composed = rendered
                else:
                    composed = self.apply_filter(rendered.copy())
            self._paint_image_to_viewport(composed, replace=True)
        elif self.filter == self.FILTER_NONE:
            super().paintEvent(e)

            # Draw Z text without filter when filter mode is disabled.
            if self.z_text:
                painter = QPainter(self.viewport())
                self._draw_z_text_with_painter(painter)
                painter.end()
        else:
            rendered = self._render_canvas_buffer()
            self._last_rendered_buffer = rendered
            filtered = self.apply_filter(rendered.copy())
            self._paint_image_to_viewport(filtered)

    def _render_canvas_buffer(self) -> QImage:
        """Render this canvas content, including Z text, into an off-screen image."""
        buffer = QImage(self.size(), QImage.Format.Format_ARGB32)
        buffer.fill(QColor(Qt.GlobalColor.white) if self._is_overlay_mode() else QColor(0, 0, 0, 0))

        painter = QPainter(buffer)
        super().render(painter)
        if self.z_text:
            self._draw_z_text_with_painter(painter)
        painter.end()
        return buffer

    def _is_overlay_mode(self) -> bool:
        return bool(self.parent and self.parent.stereo_id <= 3)

    def _compose_overlay_image(self) -> Optional[QImage]:
        if not self.parent:
            return None

        left_canvas = self.parent.canvas_left
        right_canvas = self.parent.canvas_right
        if not left_canvas or not right_canvas:
            return None

        if left_canvas._last_rendered_buffer is None:
            left_canvas._last_rendered_buffer = left_canvas._render_canvas_buffer()
        if right_canvas._last_rendered_buffer is None:
            right_canvas._last_rendered_buffer = right_canvas._render_canvas_buffer()

        left_image = left_canvas._last_rendered_buffer
        right_image = right_canvas._last_rendered_buffer
        if left_image is None or right_image is None:
            return None
        if left_image.size() != right_image.size():
            return None

        try:
            left_filtered = left_canvas.apply_filter(left_image.copy()).convertToFormat(QImage.Format.Format_RGBA8888)
            right_filtered = right_canvas.apply_filter(right_image.copy()).convertToFormat(QImage.Format.Format_RGBA8888)

            left_ptr = left_filtered.bits()
            left_ptr.setsize(left_filtered.sizeInBytes())
            left_arr = np.frombuffer(left_ptr, np.uint8).reshape(left_filtered.height(), left_filtered.width(), 4)

            right_ptr = right_filtered.bits()
            right_ptr.setsize(right_filtered.sizeInBytes())
            right_arr = np.frombuffer(right_ptr, np.uint8).reshape(right_filtered.height(), right_filtered.width(), 4)

            if self.parent.stereo_id == 1:
                left_rgba = left_image.convertToFormat(QImage.Format.Format_RGBA8888)
                right_rgba = right_image.convertToFormat(QImage.Format.Format_RGBA8888)

                left_rgba_ptr = left_rgba.bits()
                left_rgba_ptr.setsize(left_rgba.sizeInBytes())
                left_rgba_arr = np.frombuffer(left_rgba_ptr, np.uint8).reshape(left_rgba.height(), left_rgba.width(), 4)

                right_rgba_ptr = right_rgba.bits()
                right_rgba_ptr.setsize(right_rgba.sizeInBytes())
                right_rgba_arr = np.frombuffer(right_rgba_ptr, np.uint8).reshape(right_rgba.height(), right_rgba.width(), 4)

                composed = np.empty_like(left_rgba_arr)
                composed[:, :, 0] = left_rgba_arr[:, :, 0]
                composed[:, :, 1] = right_rgba_arr[:, :, 1]
                composed[:, :, 2] = right_rgba_arr[:, :, 2]
                composed[:, :, 3] = 255

                result = QImage(
                    composed.data,
                    left_rgba.width(),
                    left_rgba.height(),
                    composed.strides[0],
                    QImage.Format.Format_RGBA8888,
                )
                return result.copy()
            else:
                # Interlaced overlay: each filtered eye already owns alternate rows.
                left_alpha = left_arr[:, :, 3] > 0
                right_alpha = right_arr[:, :, 3] > 0
                composed = np.zeros_like(left_arr)
                composed[left_alpha] = left_arr[left_alpha]
                composed[right_alpha] = right_arr[right_alpha]

            result = QImage(
                composed.data,
                left_filtered.width(),
                left_filtered.height(),
                composed.strides[0],
                QImage.Format.Format_RGBA8888,
            )
            return result.copy()
        except Exception as e:
            QgsMessageLog.logMessage(f"Error composing stereo overlay: {str(e)}", "SWM-3D", Qgis.Critical)
            return None

    def _clear_viewport(self):
        viewport_painter = QPainter(self.viewport())
        viewport_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        viewport_painter.fillRect(self.viewport().rect(), QColor(0, 0, 0, 0))
        viewport_painter.end()

    def _paint_image_to_viewport(self, image: QImage, replace: bool = False):
        """Paint one already-composed image to the viewport."""
        viewport_painter = QPainter(self.viewport())
        if replace:
            viewport_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        else:
            viewport_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        viewport_painter.drawImage(0, 0, image)
        viewport_painter.end()

    def _apply_view_mirror_to_painter(self, painter: QPainter):
        """
        Applies the same mirror transform used by the canvas view.
        """
        try:
            view_transform = self.transform()
            if view_transform.m11() < 0:
                painter.translate(self.width(), 0)
                painter.scale(-1, 1)
            if view_transform.m22() < 0:
                painter.translate(0, self.height())
                painter.scale(1, -1)
        except Exception:
            pass

    def _draw_z_text_with_painter(self, painter: QPainter):
        """
        Draws the Z overlay text using current mirror settings.
        """
        from qgis.PyQt.QtGui import QFont

        self._apply_view_mirror_to_painter(painter)

        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(
            int(self.width() / 2 - painter.fontMetrics().horizontalAdvance(self.z_text) / 2),
            int(self.height() * 3 / 4),
            self.z_text,
        )


    def update_z_text(self, z_value):
        """Updates the Z text shown in the canvas."""
        self.z_text = f"Z={z_value:.1f}"
        self.update()

    def apply_filter(self, image):
        """Apply stereo filter for interlaced and simple per-eye color views."""
        if self.filter == self.FILTER_NONE:
            return image

        # Use explicit RGBA channel order to avoid format-dependent channel ambiguity.
        rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
        ptr = rgba.bits()
        ptr.setsize(rgba.sizeInBytes())
        arr = np.frombuffer(ptr, np.uint8).reshape(rgba.height(), rgba.width(), 4)

        if self.filter == self.FILTER_RED:
            arr[:, :, 1] = 0  # keep R only
            arr[:, :, 2] = 0
        elif self.filter == self.FILTER_CYAN:
            arr[:, :, 0] = 0  # keep G+B
        elif self.filter in [self.FILTER_EVEN, self.FILTER_ODD]:
            # Alternate lines. Non-selected lines become fully transparent.
            mask = np.zeros_like(arr)
            lines = range(0, rgba.height(), 2) if self.filter == self.FILTER_EVEN else range(1, rgba.height(), 2)
            mask[lines, :, :] = arr[lines, :, :]
            arr = mask
 
        # Convert back to QImage
        result = QImage(arr.data, rgba.width(), rgba.height(), rgba.bytesPerLine(), QImage.Format.Format_RGBA8888)
        return result.copy()  # Important: .copy() to avoid memory issues 
    
    def render_complete(self):
        # Draws a rectangle corresponding to the main canvas extent in this canvas
        # TODO: Fails. Rubber band?

        if self.parent and not self.parent.isVisible():
            return

        extent = self.qgis_main_canvas.extent()  # Get the extent of the main canvas

        # Keep and update a single rubber band instance to avoid scene mismatch warnings.
        needs_new_limits = self.limits is None
        if self.limits:
            try:
                limits_scene = self.limits.scene() if hasattr(self.limits, 'scene') else None
                canvas_scene = self.scene() if hasattr(self, 'scene') else None
                if canvas_scene and limits_scene and limits_scene != canvas_scene:
                    self._safe_remove_item(self.limits)
                    self.limits = None
                    needs_new_limits = True
            except Exception:
                self.limits = None
                needs_new_limits = True

        if needs_new_limits:
            self.limits = QgsRubberBand(self, QgsWkbTypes.PolygonGeometry)
            border_color = QColor(200, 200, 200)  # Gray color for the border
            self.limits.setColor(border_color)
            self.limits.setWidth(1)
            self.limits.setFillColor(QColor(0, 0, 0, 0))  # QColor(Qt.GlobalColor.transparent)

        if not self.limits:
            return

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
        # Get visible layers from the project layer tree (canonical visibility state).
        # This avoids stale snapshots that can happen around legend toggle events.
        layers_main = []
        project = QgsProject.instance()
        if project:
            root = project.layerTreeRoot()
            if root:
                for node in root.findLayers():
                    layer = node.layer()
                    if layer and node.isVisible():
                        layers_main.append(layer)
        if not layers_main:
            # Fallback to canvas layers if tree lookup is unavailable.
            layers_main = self.qgis_main_canvas.layers()

        layers_self = []  # Get the layers from this canvas
        self.layer_swm = None
        self.layers_z = []
        active_z_layer_ids = set()

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
                cached_swm = self._swm_layer_cache.get(sigrid_layer_self_url)
                if cached_swm and cached_swm.isValid():
                    # Reuse existing WMS layer instance to avoid new service requests.
                    self.layer_swm = cached_swm
                else:
                    # https://gis.stackexchange.com/questions/467847/creating-qgsrasterlayer-from-wms-layer-using-pyqgis-in-qgis-3-28
                    # This triggers an initial server request (GETCAPABILITIES)
                    self.layer_swm = QgsRasterLayer(sigrid_layer_self_url, style_value, 'wms')
                    self._swm_layer_cache[sigrid_layer_self_url] = self.layer_swm
                layers_self.append(self.layer_swm)  
            elif is_z_layer(layer_main):
                active_z_layer_ids.add(layer_main.id())
                # Layer has Z values. Must apply Geometry Generator
                # Copy layer_main to apply Geometry Generator. Ensure the CRS and other properties are the same
                # 1) Create an independent logical view for the secondary canvas.
                layer_copy = None

                # Editable layers keep unsaved features in edit buffers. Use a materialized
                # snapshot so stereo canvases render those in-progress edits immediately.
                if layer_main.isEditable():
                    try:
                        layer_copy = layer_main.materialize(QgsFeatureRequest())
                        if layer_copy and layer_copy.isValid():
                            layer_copy.setName(layer_main.name())
                            self._z_layer_cache[layer_main.id()] = layer_copy
                    except Exception as e:
                        QgsMessageLog.logMessage(
                            f"SYNC_LAYER: Error materializing editable layer '{layer_main.name()}': {str(e)}",
                            "SWM-3D",
                            Qgis.Warning,
                        )
                        layer_copy = None

                if layer_copy is None:
                    layer_copy = self._z_layer_cache.get(layer_main.id())
                    if layer_copy is None or not layer_copy.isValid() or layer_copy.source() != layer_main.source():
                        layer_copy = QgsVectorLayer(layer_main.source(), layer_main.name(), layer_main.providerType())
                        self._z_layer_cache[layer_main.id()] = layer_copy
                source_crs = layer_main.crs()
                if source_crs and source_crs.isValid():
                    layer_copy.setCustomProperty("swm_source_authid", source_crs.authid())
                    # Keep declared CRS aligned with source layer so feature filtering by extent works correctly.
                    layer_copy.setCrs(source_crs)
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
                # Stereo canvases use a synthetic render context; scale-based visibility
                # from the main layer can hide features unexpectedly after CRS changes.
                layer_copy.setScaleBasedVisibility(False)
                self.layers_z.append(layer_copy)
                layers_self.append(layer_copy)
            else:
                # Keep layers that are neither SWM nor Z-enabled
                layers_self.append(layer_main)

        # Remove stale cached Z copies from layers no longer present.
        for layer_id in list(self._z_layer_cache.keys()):
            if layer_id not in active_z_layer_ids:
                del self._z_layer_cache[layer_id]

        self.setLayers(layers_self)

        # Re-apply current 3D transform expression after any visibility/order sync.
        # Without this, toggling a Z layer can leave it in 2D ($geometry)
        # until another SWM WMS reply arrives.
        self._apply_current_transform_to_z_layers()
        
        # Force map canvas item synchronization after changing layers
        self.force_sync_canvas_items()

    def _build_current_geometry_expression(self, layer: QgsVectorLayer) -> str:
        """
        Builds the current Geometry Generator expression for a Z layer.
        Returns a 2D passthrough when no photogrammetric transform is available.
        """
        geometry_input_expr = "$geometry"

        if not self._is_stereo_projection_active():
            return geometry_input_expr

        swm_authid = ""
        if self.layer_swm and self.layer_swm.crs() and self.layer_swm.crs().isValid():
            swm_authid = self.layer_swm.crs().authid()

        layer_authid = str(layer.customProperty("swm_source_authid", "")).strip()
        if not layer_authid:
            layer_crs = layer.crs()
            layer_authid = layer_crs.authid() if layer_crs and layer_crs.isValid() else ""

        if swm_authid and layer_authid and swm_authid != layer_authid:
            geometry_input_expr = f"transform($geometry,'{layer_authid}','{swm_authid}')"

        # No transform loaded yet: keep 2D rendering path.
        if not self.trf_wld2prp:
            return geometry_input_expr

        side = 'left' if self.is_left else 'right'
        perspective_expr = (
            f"perspective_swm_transform({geometry_input_expr},'{side}','{self.trf_wld2prp.txt_perspective}','{self.trf_wld2prp.txt_projective}')"
        )

        if swm_authid and layer_authid and swm_authid != layer_authid:
            # Return geometry to the layer CRS so QGIS render pipeline transforms once to canvas CRS.
            return f"transform({perspective_expr},'{swm_authid}','{layer_authid}')"

        return perspective_expr

    def _apply_current_transform_to_z_layers(self) -> bool:
        """
        Applies the current Geometry Generator expression to all cached Z layers.
        Returns True when at least one layer expression changed.
        """
        expressions_updated = False

        for layer in self.layers_z:
            symbol_layer = layer.renderer().symbol().symbolLayer(0)
            if not isinstance(symbol_layer, QgsGeometryGeneratorSymbolLayer):
                QgsMessageLog.logMessage(
                    f"Unexpected SymbolLayer type in layer {layer.name()}: {type(symbol_layer)}",
                    "SWM-3D",
                    Qgis.Warning,
                )
                continue

            expression = self._build_current_geometry_expression(layer)
            current_expression = symbol_layer.geometryExpression()
            if current_expression != expression:
                symbol_layer.setGeometryExpression(expression)
                layer.triggerRepaint()
                expressions_updated = True

        return expressions_updated

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

        # Update Geometry Generator for Z layers known by this canvas copy.
        # Using self.layers_z avoids relying on provider-side wkb reporting of the copy.
        expressions_updated = False

        for layer in self.layers_z:
            layer.setCustomProperty("swm_trf_wrl2pht", txt_trf_wrl2pht)
            layer.setCustomProperty("swm_trf_pht2prp", txt_trf_pht2prp)

        # GeometryGenerator must be updated now that transformation is available.
        # layer.triggerRepaint() (called inside _apply_current_transform_to_z_layers) is
        # enough to re-render the Z vector layers with the new expression.
        # Do NOT call self.refresh() here: that would issue a second WMS GetMap request
        # immediately after the first one just completed, doubling network load per zoom.
        self._apply_current_transform_to_z_layers()

        self.render_complete()
