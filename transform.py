"""
transform.py

Photogrammetric transformation model for the Sigrid SWM plugin.

This module defines the TrfWldToPrjPln class, which represents the
world-to-projection-plane transformation used by the plugin. It is
fed with transformation parameters read from WMS headers and provides
methods to transform individual 3D points to 2D projection coordinates.

The transformation model is used for interactive elements such as
cursor projection. Geometry-wide transformations for rendering are
handled separately by QGIS expression functions.

This module does not handle rendering, network communication, or
geometry iteration.

It is "the Python representation of the active photogrammetric model"

"""
import math

from qgis.core import QgsPointXY, QgsPoint
from qgis.core import QgsMessageLog, Qgis  # for debug messages.


# Class to transform coordinate world to projection plane
class TrfWldToPrjPln:
    """Class to transform coordinate world to projection plane."""

    def __init__(self):
        # Perspective transformation (world to photo)
        self.x0 = self.y0 = self.z0 = self.df = 0.0
        self.r = [[0.0 for _ in range(3)] for _ in range(3)]
        self.txt_perspective = ""

        # Projective transform (photo to projection plane)
        self.a = [0.0] * 3
        self.b = [0.0] * 3
        self.c = [0.0] * 2
        self.ai = [0.0] * 3
        self.bi = [0.0] * 3
        self.ci = [0.0] * 2
        self.txt_projective = ""

    def execute_pht2prp(self, pnt_pht):
        """Projective transformation photo to projection plane."""
        x_pht = pnt_pht.x()
        y_pht = pnt_pht.y()

        den = self.c[0] * x_pht + self.c[1] * y_pht + 1
        if den == 0:
            return None
        x_prp = (self.a[0] * x_pht + self.a[1] * y_pht + self.a[2]) / den
        y_prp = (self.b[0] * x_pht + self.b[1] * y_pht + self.b[2]) / den

        return QgsPointXY(x_prp, y_prp)

    def execute_prp2pht(self, pnt_prp):
        """Projective transformation (projection plane to photo)."""
        x_prp = pnt_prp.x()
        y_prp = pnt_prp.y()

        den = self.ci[0] * x_prp + self.ci[1] * y_prp + 1
        if den == 0:
            return None
        x_pht = (self.ai[0] * x_prp + self.ai[1] * y_prp + self.ai[2]) / den
        y_pht = (self.bi[0] * x_prp + self.bi[1] * y_prp + self.bi[2]) / den

        return QgsPointXY(x_pht, y_pht)

    def estimate_flight_angle_deg_from_prp2pht(self, sample_step: float = 1000.0):
        """
        Estimate strip direction angle (degrees) from inverse projective transform.

        It samples a horizontal segment in projection-plane coordinates and maps it
        back to photo coordinates using execute_prp2pht(). The returned angle is the
        orientation of that mapped segment against the positive X axis.
        """
        if sample_step <= 0:
            return None

        p0 = self.execute_prp2pht(QgsPointXY(0.0, 0.0))
        p1 = self.execute_prp2pht(QgsPointXY(sample_step, 0.0))
        if p0 is None or p1 is None:
            return None

        dx = p1.x() - p0.x()
        dy = p1.y() - p0.y()
        if abs(dx) < 1e-12 and abs(dy) < 1e-12:
            return None

        return math.degrees(math.atan2(dy, dx))

    def execute_prp_wrl2pht(self, pnt_wrl):
        """Perspective transformation (world 3D to photo 2D)."""
        dx = pnt_wrl.x() - self.x0
        dy = pnt_wrl.y() - self.y0
        dz = pnt_wrl.z() - self.z0
        r = self.r

        den = (r[0][2] * dx + r[1][2] * dy + r[2][2] * dz)
        if den == 0:
            return None
        daux = -self.df / den
        x_pht = (r[0][0] * dx + r[1][0] * dy + r[2][0] * dz) * daux
        y_pht = (r[0][1] * dx + r[1][1] * dy + r[2][1] * dz) * daux

        return QgsPointXY(x_pht, y_pht)

    def execute_wrl2prp(self, pnt_wrl):
        """Transformation world 3D to projection plane 2D."""
        pnt_pht = self.execute_prp_wrl2pht(pnt_wrl)
        if not pnt_pht:
            return None
        return self.execute_pht2prp(pnt_pht)

    def execute_prp2wrl_at_z(self, pnt_prp, z: float):
        """Returns the world point on a projection-plane ray at the given Z."""
        if self.df == 0:
            return None

        pnt_pht = self.execute_prp2pht(pnt_prp)
        if not pnt_pht:
            return None

        photo_scale_x = -pnt_pht.x() / self.df
        photo_scale_y = -pnt_pht.y() / self.df
        r = self.r
        direction_x = photo_scale_x * r[0][0] + photo_scale_y * r[0][1] + r[0][2]
        direction_y = photo_scale_x * r[1][0] + photo_scale_y * r[1][1] + r[1][2]
        direction_z = photo_scale_x * r[2][0] + photo_scale_y * r[2][1] + r[2][2]
        if abs(direction_z) < 1e-12:
            return None

        distance = (float(z) - self.z0) / direction_z
        return QgsPointXY(self.x0 + distance * direction_x, self.y0 + distance * direction_y)

    def read_perspective(self, txt):
        """Read perspective parameters from a text string.
        from WMS header (world -> photo).
        """
        self.txt_perspective = txt
        fields = txt.split(';')
        if len(fields) < 6:
            QgsMessageLog.logMessage(f"[DEBUG] <read_perspective> fields ({fields}). Invalid size (<6)",
                                     "SWM_3D", Qgis.Info)
            return
        self.x0 = float(fields[2])
        self.y0 = float(fields[3])
        self.z0 = float(fields[4])
        self.df = float(fields[5])

        ifld = 6
        for i in range(3):
            for j in range(3):
                self.r[i][j] = float(fields[ifld])
                ifld += 1

    def read_projective(self, txt):
        """Read projective transformation parameters from a text string.
        from WMS header (photo -> projection plane).
        """
        self.txt_projective = txt
        fields = txt.split(';')

        if len(fields) < 10:
            QgsMessageLog.logMessage(f"[DEBUG] <read_projective> fields ({fields}). Invalid size (<10)",
                                     "SWM_3D", Qgis.Info)
            return
        self.a[0] = float(fields[2])
        self.a[1] = float(fields[3])
        self.a[2] = float(fields[4])
        self.b[0] = float(fields[5])
        self.b[1] = float(fields[6])
        self.b[2] = float(fields[7])
        self.c[0] = float(fields[8])
        self.c[1] = float(fields[9])

        # Inverse transformation
        div = self.a[0] * self.b[1] - self.a[1] * self.b[0]
        self.ai[0] = (self.b[1] - self.b[2] * self.c[1]) / div
        self.ai[1] = (self.a[2] * self.c[1] - self.a[1]) / div
        self.ai[2] = (self.a[1] * self.b[2] - self.a[2] * self.b[1]) / div
        self.bi[0] = (self.b[2] * self.c[0] - self.b[0]) / div
        self.bi[1] = (self.a[0] - self.a[2] * self.c[0]) / div
        self.bi[2] = (self.a[2] * self.b[0] - self.a[0] * self.b[2]) / div
        self.ci[0] = (self.b[0] * self.c[1] - self.b[1] * self.c[0]) / div
        self.ci[1] = (self.a[1] * self.c[0] - self.a[0] * self.c[1]) / div
