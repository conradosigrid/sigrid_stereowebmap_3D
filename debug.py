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
