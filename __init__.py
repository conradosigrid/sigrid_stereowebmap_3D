def classFactory(iface):
    from .plugin import SgdSwmPlugin
    return SgdSwmPlugin(iface)