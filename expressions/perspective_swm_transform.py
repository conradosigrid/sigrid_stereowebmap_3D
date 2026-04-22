"""
perspective_swm_transform.py

QGIS expression function for perspective geometry transformation used by the
Sigrid SWM plugin.

This module defines the QGIS expression function
`perspective_swm_transform`, which applies the photogrammetric perspective
transformation to Z-enabled geometries at render time using Geometry
Generator expressions.

Evolution of the implementation
--------------------------------
Originally, this functionality was implemented as two separate QGIS
expression scripts (`perspective_swm_transform_left` and
`perspective_swm_transform_right`) that had to be manually copied into
the user's QGIS profile under the `python/expressions` directory.

In the current design, those functions have been:
- Unified into a single parametrized expression function
- Integrated directly into the plugin source tree
- Automatically registered at plugin startup via import and the
  `@qgsfunction` decorator

This makes the plugin fully self-contained and removes any dependency on
external user profile files. Todo está dentro del directorio plugins/SWM_3D

Design notes
------------
- The function is stateless and safe to be executed repeatedly by the
  QGIS expression engine during rendering
- Parsing of transformation parameters is minimized and cached locally
  for performance
- No plugin state, network logic, or UI code is handled here

The function operates exclusively at geometry level and is used only
during rendering. Interactive transformations and stateful logic are
handled elsewhere in the plugin.

"""
import math
from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.utils import qgsfunction



# ------------------------------------------------------------------
# Cache de parseo (clave = texto del header) (para no parsear siempre)
# ------------------------------------------------------------------
_PERSPECTIVE_CACHE = {}
_PROJECTIVE_CACHE = {}


# ------------------------------------------------------------------
# Helpers de módulo
# ------------------------------------------------------------------
def read_perspective(txt):
    cached = _PERSPECTIVE_CACHE.get(txt)
    if cached is not None:
        return cached
    fields = txt.split(';')

    x0 = float(fields[2])
    y0 = float(fields[3])
    z0 = float(fields[4])
    df = float(fields[5])

    r = [[0.0]*3 for _ in range(3)]
    k = 6
    for i in range(3):
        for j in range(3):
            r[i][j] = float(fields[k])
            k += 1

    result = (x0, y0, z0, df, r)
    _PERSPECTIVE_CACHE[txt] = result
    return result



def read_projective(txt):
    cached = _PROJECTIVE_CACHE.get(txt)
    if cached is not None:
        return cached
        
    fields = txt.split(';')

    a = [float(fields[2]), float(fields[3]), float(fields[4])]
    b = [float(fields[5]), float(fields[6]), float(fields[7])]
    c = [float(fields[8]), float(fields[9])]

    result = (a, b, c)
    _PROJECTIVE_CACHE[txt] = result
    return result



# ------------------------------------------------------------------
# Transformaciones elementales
# ------------------------------------------------------------------

def world_to_photo(x, y, z, x0, y0, z0, df, r):
    dx = x - x0
    dy = y - y0
    dz = z - z0

    den = r[0][2]*dx + r[1][2]*dy + r[2][2]*dz
    if den == 0:
        return None

    d = -df / den

    xp = (r[0][0]*dx + r[1][0]*dy + r[2][0]*dz) * d
    yp = (r[0][1]*dx + r[1][1]*dy + r[2][1]*dz) * d

    return xp, yp


def photo_to_proj(xp, yp, a, b, c):
    den = c[0]*xp + c[1]*yp + 1
    if den == 0:
        return None

    x2 = (a[0]*xp + a[1]*yp + a[2]) / den
    y2 = (b[0]*xp + b[1]*yp + b[2]) / den

    return x2, y2


@qgsfunction(args='auto', group='Sigrid SWM', usesgeometry=True)
def perspective_swm_transform(geometry, side, txt_trf_wrl2pht, txt_trf_pht2prp):
    """
    Returns the geometry after a perspective transformation using the Sigrid StereoWepMap model.
    Transformation parameters are extracted from the header of a Sigrid StereoWepMap response.
    The transformation is applied to the X Y and Z coordinates of the geometry.
    The 3D point first is transformed to the camera coordinate system, 
    Later point is projected to projection plain.

    :param geometry: QgsGeometry to transform.
    :param side: str 'left' o 'right'
    :param txt_trf_wrl2pht: Response header value of a SWM service with world to photo perspective transform.
    :param txt_trf_pht2prp: Response header value of a SWM service with photo to projection plain projective transform .
    :return: Transformed QgsGeometry.
    """
    
    if geometry is None or geometry.isEmpty():
        return geometry
        
    if side not in ('left', 'right'):
        return geometry

    try:
        x0, y0, z0, df, r = read_perspective(txt_trf_wrl2pht)
        a, b, c = read_projective(txt_trf_pht2prp)
    except Exception:
        return geometry

    gtype = QgsWkbTypes.geometryType(geometry.wkbType())

    # -------------------------------
    # Punto
    # -------------------------------
    if gtype == QgsWkbTypes.PointGeometry:
        # MultiPoint
        if QgsWkbTypes.isMultiType(geometry.wkbType()):
            new_points = []

            for p in geometry.asMultiPoint():
                if not math.isfinite(p.z()):
                    continue

                res = world_to_photo(p.x(), p.y(), p.z(), x0, y0, z0, df, r)
                if not res:
                    continue

                res = photo_to_proj(res[0], res[1], a, b, c)
                if not res:
                    continue

                new_points.append(QgsPointXY(res[0], res[1]))

            if not new_points:
                return geometry

            return QgsGeometry.fromMultiPointXY(new_points)

        # Single Point
        else:
            p = geometry.asPoint()
            if not math.isfinite(p.z()):
                return geometry

            res = world_to_photo(p.x(), p.y(), p.z(), x0, y0, z0, df, r)
            if not res:
                return geometry

            res = photo_to_proj(res[0], res[1], a, b, c)
            if not res:
                return geometry

            return QgsGeometry.fromPointXY(QgsPointXY(*res))

# --------------------------------------------------
    # LineString
    # --------------------------------------------------
    elif gtype == QgsWkbTypes.LineGeometry:
        new_line = []

        for p in geometry.vertices():
            if not math.isfinite(p.z()):
                continue

            res = world_to_photo(p.x(), p.y(), p.z(), x0, y0, z0, df, r)
            if not res:
                continue

            res = photo_to_proj(res[0], res[1], a, b, c)
            if not res:
                continue

            new_line.append(QgsPointXY(res[0], res[1]))

        if len(new_line) < 2:
            return geometry

        return QgsGeometry.fromPolylineXY(new_line)

    # --------------------------------------------------
    # Polygon
    # --------------------------------------------------
    elif gtype == QgsWkbTypes.PolygonGeometry:
        new_rings = []

        for ring in geometry.asPolygon():
            new_ring = []

            for p in ring:
                if not math.isfinite(p.z()):
                    continue

                res = world_to_photo(p.x(), p.y(), p.z(), x0, y0, z0, df, r)
                if not res:
                    continue

                res = photo_to_proj(res[0], res[1], a, b, c)
                if not res:
                    continue

                new_ring.append(QgsPointXY(res[0], res[1]))

            # Un polígono necesita al menos 4 puntos (cerrado)
            if len(new_ring) >= 4:
                new_rings.append(new_ring)

        if not new_rings:
            return geometry

        return QgsGeometry.fromPolygonXY(new_rings)

    # --------------------------------------------------
    # MultiLineString
    # --------------------------------------------------
    elif gtype == QgsWkbTypes.MultiLineGeometry:
        new_lines = []

        for line in geometry.asMultiPolyline():
            new_line = []

            for p in line:
                if not math.isfinite(p.z()):
                    continue

                res = world_to_photo(p.x(), p.y(), p.z(), x0, y0, z0, df, r)
                if not res:
                    continue

                res = photo_to_proj(res[0], res[1], a, b, c)
                if not res:
                    continue

                new_line.append(QgsPointXY(res[0], res[1]))

            if len(new_line) >= 2:
                new_lines.append(new_line)

        if not new_lines:
            return geometry

        return QgsGeometry.fromMultiPolylineXY(new_lines)

    # --------------------------------------------------
    # MultiPolygon
    # --------------------------------------------------
    elif gtype == QgsWkbTypes.MultiPolygonGeometry:
        new_polygons = []

        for polygon in geometry.asMultiPolygon():
            new_rings = []

            for ring in polygon:
                new_ring = []

                for p in ring:
                    if not math.isfinite(p.z()):
                        continue

                    res = world_to_photo(p.x(), p.y(), p.z(), x0, y0, z0, df, r)
                    if not res:
                        continue

                    res = photo_to_proj(res[0], res[1], a, b, c)
                    if not res:
                        continue

                    new_ring.append(QgsPointXY(res[0], res[1]))

                if len(new_ring) >= 4:
                    new_rings.append(new_ring)

            if new_rings:
                new_polygons.append(new_rings)

        if not new_polygons:
            return geometry

        return QgsGeometry.fromMultiPolygonXY(new_polygons)


    return geometry

