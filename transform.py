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
        x_pht = pnt_prp.x()
        y_pht = pnt_prp.y()

        den = self.ci[0] * x_pht + self.ci[1] * y_pht + 1
        if den == 0:
            return None
        x_prp = (self.ai[0] * x_pht + self.ai[1] * y_pht + self.ai[2]) / den
        y_prp = (self.bi[0] * x_pht + self.bi[1] * y_pht + self.bi[2]) / den

        return QgsPointXY(x_prp, y_prp)

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
