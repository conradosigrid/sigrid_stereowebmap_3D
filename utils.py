"""

utils.py

Utility helper functions for the Sigrid SWM plugin.

This module contains small, stateless helper functions used across the
plugin, such as layer type checks and metadata extraction. These utilities
do not depend on UI elements or plugin state and are shared by different
modules.

"""
import re
from qgis.core import QgsMapLayer, QgsWkbTypes


# Function to extract metadata using regular expressions
def extract_metadata(html, tag):
    if not html:
        return None
    # Search for the tag in the HTML (e.g., <b>Abstract:</b> SigridSwmFlightService<br>)
    pattern = rf'<b>{tag}:</b>\s*(.*?)<br>'
    match = re.search(pattern, html, re.IGNORECASE)
    return match.group(1).strip() if match else None


# Function to detect if is a sigrid Swm layer
def is_sgd_swm_layer(layer):
    """Checks if the layer is a Sigrid StereoWebMap layer."""
    ret = layer.type() == QgsMapLayer.RasterLayer and layer.providerType() == 'wms'
    if ret:
        # SgdWmsPhtLyrId = "SigridSwmFlightService"
        SgdWmsPhtLyrId = "SigridPhotogrammetricFlightService"
        metadata_html = layer.dataProvider().htmlMetadata()
        ret = re.search(SgdWmsPhtLyrId, metadata_html) is not None
    return ret


def is_z_layer(layer):
    """
    Returns True if the layer is a vector layer with Z coordinates,
    regardless of geometry type (PointZ, LineZ, PolygonZ, Multi*, etc.).
    """
    ret = (layer.type() == QgsMapLayer.VectorLayer) and QgsWkbTypes.hasZ(layer.wkbType())
    return ret
