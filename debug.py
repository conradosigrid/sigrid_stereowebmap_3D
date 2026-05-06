"""
Este módulo se encarga de configurar y activar el modo debug para el desarrollo local.
Como la librería debugpy no es un requisito para el funcionamiento normal del plugin, se maneja de forma opcional y segura, 
sin afectar a los usuarios finales aunque no tengan instalado debugpy.
"""
DEBUG = True  
# se podría leer de una variable de entorno, pero esto es más directo para el desarrollo local
# import os
# DEBUG = os.environ.get("SWM3D_DEBUG", "0") == "1"
# y lanzando QGIS con
# set SWM3D_DEBUG=1  # con debug
# set SWM3D_DEBUG=0  # sin debug


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
        # NUNCA rompas la carga del plugin
        print(f"[SWM-3D] Debug skipped: {e}")
