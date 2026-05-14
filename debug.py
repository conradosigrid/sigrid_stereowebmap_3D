"""
This module configures and enables debug mode for local development.
Because debugpy is not required for normal plugin usage, it is handled
optionally and safely, without affecting end users who do not have it installed.
"""
DEBUG = False  
# This could be read from an environment variable, but this is simpler for local development
# import os
# DEBUG = os.environ.get("SWM3D_DEBUG", "0") == "1"
# and launching QGIS with
# set SWM3D_DEBUG=1  # with debug
# set SWM3D_DEBUG=0  # without debug


def attach_debugger():
    if not DEBUG:
        return

    try:
        import debugpy
        debugpy.configure(python=r"C:/OSGeo4W/apps/Python312/python.exe")
        if not debugpy.is_client_connected():
            debugpy.listen(("localhost", 5678))
            print("[SWM-3D] debugpy listening on port 5678...")
            print("[SWM-3D] Waiting for debugger to attach...")
            debugpy.wait_for_client()
            print("[SWM-3D] Debugger attached")
        else:
            print("[SWM-3D] Debugger already attached")

    except Exception as e:
        # NEVER break plugin loading
        print(f"[SWM-3D] Debug skipped: {e}")


def run_canvas_multipart_regression_checks(swm_canvas):
    """
    Manual smoke checks for multipart geometry transformation in QgsSgdSwmCanvas.

    Usage from QGIS Python console:
    - from your plugin debug module import run_canvas_multipart_regression_checks
    - run_canvas_multipart_regression_checks(plugin_instance.canvas_left)

    Returns a dict with per-geometry results and raises AssertionError on failure.
    """
    from qgis.core import QgsGeometry, QgsWkbTypes
    from qgis.gui import QgsRubberBand

    if swm_canvas is None:
        raise ValueError("swm_canvas is required")

    if not getattr(swm_canvas, "trf_wld2prp", None):
        raise ValueError("swm_canvas.trf_wld2prp is not configured; open a stereo session first")

    # Temporary rubber band used only as key container for tracked Z fallback.
    rb = QgsRubberBand(swm_canvas)

    # Keep Z deterministic in case source geometries are 2D.
    swm_canvas.rubber_band_fixed_z[rb] = [10.0] * 64

    samples = {
        "multipoint": QgsGeometry.fromWkt("MultiPoint((0 0),(10 0),(20 0))"),
        "multiline": QgsGeometry.fromWkt("MultiLineString((0 0,10 0),(0 10,10 10))"),
        "multipolygon": QgsGeometry.fromWkt(
            "MultiPolygon(((0 0,10 0,10 10,0 10,0 0)),((20 0,30 0,30 10,20 10,20 0)))"
        ),
    }

    results = {}

    transformed_mp = swm_canvas._transform_geometry(samples["multipoint"], rb)
    assert transformed_mp is not None and not transformed_mp.isEmpty(), "multipoint transform returned empty"
    assert QgsWkbTypes.isMultiType(transformed_mp.wkbType()), "multipoint transformed as single point"
    assert len(transformed_mp.asMultiPoint()) == 3, "multipoint part count changed"
    results["multipoint"] = "ok"

    transformed_ml = swm_canvas._transform_geometry(samples["multiline"], rb)
    assert transformed_ml is not None and not transformed_ml.isEmpty(), "multiline transform returned empty"
    assert QgsWkbTypes.isMultiType(transformed_ml.wkbType()), "multiline transformed as single line"
    assert len(transformed_ml.asMultiPolyline()) == 2, "multiline part count changed"
    results["multiline"] = "ok"

    transformed_mpg = swm_canvas._transform_geometry(samples["multipolygon"], rb)
    assert transformed_mpg is not None and not transformed_mpg.isEmpty(), "multipolygon transform returned empty"
    assert QgsWkbTypes.isMultiType(transformed_mpg.wkbType()), "multipolygon transformed as single polygon"
    assert len(transformed_mpg.asMultiPolygon()) == 2, "multipolygon part count changed"
    results["multipolygon"] = "ok"

    # Cleanup temporary state.
    swm_canvas.rubber_band_fixed_z.pop(rb, None)
    try:
        rb.reset()
        rb.deleteLater()
    except Exception:
        pass

    return results
