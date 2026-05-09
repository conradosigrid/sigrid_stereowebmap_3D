"""

plugin.py

Entry point for the Sigrid SWM QGIS plugin.

This module defines the main plugin class and integrates the plugin
into the QGIS interface. Its responsibilities include:

- Registering custom QGIS expression functions on plugin startup
- Creating and managing the plugin action (menu and toolbar)
- Opening and closing the main SWM window
- Cleaning up resources when the plugin is unloaded

This module does not handle rendering, network communication, or
mathematical transformations. It only manages the plugin lifecycle
and delegates all functionality to the window, canvas, and expression
modules.

Architecture of the modules included in this plugin:

sigrid_swm/
├── __init__.py        # classFactory
├── plugin.py          # DEBUG + lifecycle
├── window.py          # orchestration
├── canvas.py          # rendering
├                      # includes Z control
├── transform.py       # mathematical model
├── utils.py           # extract_metadata, is_*_layer
└── expressions/
    ├── __init__.py
    └── perspective_swm_transform.py
"""
from qgis.PyQt.QtWidgets import QAction
# SWM libraries
from .window import QSgdSwmWindow
try:
    from .debug import attach_debugger
    attach_debugger()
except ImportError:
    pass

class SgdSwmPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.window = None
        self.action = None
        self._debug_waited = False

    def initGui(self):
        # Automatic registration of expressions
        self._register_expressions()
        # Create the action for the plugin
        self.action = QAction("Open SWM-3D", self.iface.mainWindow())
        self.action.triggered.connect(self.run)

        self.iface.addPluginToMenu( "&SWM-3D Plugin", self.action)
        self.iface.addToolBarIcon(self.action)

    def _register_expressions(self):
        # Importing the module is enough to register functions (@qgsfunction)
        from .expressions import perspective_swm_transform

    def run(self):
        if self.window is None:
            self.window = QSgdSwmWindow(self.iface)
            if self.window.init_error:
                # The plugin should not show dialogs. User interaction is handled in window.py
                # QMessageBox.critical(self.iface.mainWindow(), "Error", self.window.init_error)
                # Log for developers / advanced users
                from qgis.core import QgsMessageLog, Qgis
                QgsMessageLog.logMessage(self.window.init_error, "SWM-3D", Qgis.Critical)

                self.window = None
                return

        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def unload(self):
        # Remove the action from the menu and toolbar
        from qgis.core import QgsExpression

        try:
            QgsExpression.unregisterFunction("perspective_swm_transform")
        except Exception:
            pass

        if self.window:
            self.window.close()
            self.window = None

        self.iface.removePluginMenu("&SWM-3D Plugin", self.action)
        self.iface.removeToolBarIcon(self.action)
