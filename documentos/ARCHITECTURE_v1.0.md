***

# SWM-3D – Plugin Architecture

## 1. Purpose

**SWM-3D** is a QGIS plugin for **stereoscopic visualization of SWM photogrammetric WMS services**.  
It provides a secondary window that displays **left/right stereo views** derived from the main QGIS map canvas, applying photogrammetric 3D transformations and an interactive depth (Z) control.

The plugin is designed to:

*   Mirror the content of the QGIS main canvas.
*   Apply photogrammetric world?image?projection-plane transformations.
*   Provide an interactive Z parameter controlled by mouse wheel input.
*   Keep a strict separation between **input**, **state**, and **representation**.

***

## 2. Core Design Principle

> **Global input and shared state belong to the controller.  
> Visual representation belongs to the views.**

This principle drives all architectural decisions in SWM-3D.

***

## 3. High-Level Architecture

    User
     +- Physical mouse
         +- QGIS Main MapCanvas   (single source of truth)
             +- XY cursor position
             +- Zoom / extent
             +- Layer stack
             +- Wheel + modifiers
                     ¦
                     ?
            QSgdSwmWindow         (GLOBAL CONTROLLER)
            +- z_cursor               Global depth parameter
            +- z_proj_plane           Projection plane Z
            +- eventFilter()          ALT + wheel capture
            +- update_z_label()       Status bar feedback
            +- network_reply_handle() WMS header processing
                     ¦
            +-----------------+
            ?                 ?
    QgsSgdSwmCanvas       QgsSgdSwmCanvas
       (LEFT view)            (RIGHT view)
            +- Virtual cursor
            +- 3D reprojection
            +- Stereo render

Key idea:  
**The QGIS main canvas is always the master.**  
The plugin window and its canvases are *derived views*.

***

## 4. Module Responsibilities

### 4.1 `plugin.py` – Plugin Lifecycle

**Role:** Entry point and lifecycle manager.

Responsibilities:

*   Register the plugin in QGIS.
*   Create menu/toolbar action.
*   Create and destroy `QSgdSwmWindow`.

Non-responsibilities:

*   Rendering
*   Network logic
*   Input handling
*   Z management
*   Geometry transformations

This module must remain minimal and stable.

***

### 4.2 `QSgdSwmWindow` (`window.py`) – Global Controller

`QSgdSwmWindow` is the **central coordinator** of the plugin.

#### 4.2.1 Global Shared State

The window owns all **shared state**:

```python
self.z_cursor        # current Z value (float)
self._z_proj_plane   # Z of projection plane from WMS headers
```

Characteristics:

*   Single instance.
*   Shared by all stereoscopic canvases.
*   Never duplicated.

***


## 4.2.2 Input Handling (`eventFilter`)

The window installs a **global event filter** on `QApplication` in order to capture user input that affects the global visualization state of the plugin.

**Purpose:**

- Capture **ALT + mouse wheel** events generated while interacting with the QGIS main canvas.
- Convert wheel input into a **?Z (depth offset)**.
- Update the global `z_cursor` value.
- Trigger a controlled propagation of the new Z value to all stereoscopic canvases.

This mechanism allows interactive depth control without interfering with the standard zoom and navigation tools of QGIS.

**Why input handling is implemented in the window controller:**

- Mouse input originates from the **QGIS main canvas**, which is the single source of truth for user interaction.
- Plugin canvases are **derived, non-interactive views** and must never capture or interpret physical input directly.
- Installing the event filter in the controller prevents duplication of input logic across multiple canvases and guarantees consistent behavior.

The controller translates physical input events into **high-level state changes** (Z updates), leaving all rendering responsibilities to the canvases.

***

## 4.2.3 Projection Plane Handling (`z_proj_plane`)

The window processes relevant WMS responses in `network_reply_handle()` in order to configure the photogrammetric reference frame.

**Processing steps:**

- Filter incoming network replies to retain only **SWM photogrammetric LEFT / RIGHT** WMS responses.
- Read the `SIGRID_PROJECTIONPLAINZ` HTTP header.
- Validate header content and handle invalid or empty values gracefully.
- Update the internal `_z_proj_plane` reference value.
- Initialize or re-synchronize the global `z_cursor` accordingly.

`z_proj_plane` represents the **reference depth of the photogrammetric projection plane** and defines the initial or baseline Z position of the virtual cursor.

This operation occurs **only** on valid photogrammetric responses and never on standard QGIS WMS traffic.

***

## 4.2.4 Propagation to Canvases

Each time the global `z_cursor` value changes, the window explicitly notifies all stereoscopic canvases:

```python
canvas_left.update_cursor_with_z(z_cursor)
canvas_right.update_cursor_with_z(z_cursor)
```

In addition to Z changes, the controller is also responsible for detecting and propagating the following events originating from the QGIS main canvas:

- **Cursor displacement (X/Y)**  
  Propagated via `xyCoordinates` to all plugin canvases.

- **Map extent changes (zoom or pan)**  
  Propagated via `extentsChanged`.

- **Layer changes** (addition, removal, reordering, renderer updates).

### Viewpoint-dependent rendering

In **SWM-3D**, any change in map extent (either zoom or pan) implies a **change in the photogrammetric viewpoint**.  
Unlike standard planar rendering, the stereoscopic WMS imagery depends on the camera position and viewing geometry.

Therefore:

- A change in extent is treated as a **camera/viewpoint change**.
- Stereoscopic canvases must **fully resynchronize their rendering** on every extent change.
- This includes reapplying layer synchronization and explicitly forcing a refresh.

As a result, extent synchronization intentionally performs a complete refresh cycle:

```python
canvas.setExtent(extent)
canvas.sync_layers()
canvas.refresh()
```

The controller **never performs geometry computation or rendering itself**; it strictly coordinates when and why canvases must update.

***

## 4.2.5 Status Bar Integration

The window manages a **persistent status bar widget** that exposes the current depth state to the user:

```
Zbase=XXX   Zcurs=YYY
```

**Characteristics:**

- Implemented using a `QLabel`.
- Installed as a permanent widget in the QGIS status bar.
- Updated automatically in `update_z_label()` whenever `z_cursor` or `z_proj_plane` changes.
- Represents **continuous application state**, not transient notifications.

This feedback provides precise depth awareness during interactive stereoscopic navigation.

***

### Design Note

These sections reflect a key architectural decision in SWM-3D:

> **Depth, viewpoint and input are global concerns handled by the controller;  
> rendering is a purely local responsibility of the canvases.**


***

### 4.3 `QgsSgdSwmCanvas` (`canvas.py`) – Derived Stereo View

`QgsSgdSwmCanvas` is a **passive, derived view** of the main canvas.

#### 4.3.1 What the Canvas Does NOT Do

*   Does not capture physical mouse input.
*   Does not maintain its own Z state.
*   Does not manage UI widgets.
*   Does not interpret wheel events.
*   Does not own shared state.

***

#### 4.3.2 What the Canvas Does

*   Mirrors extent, layers, and cursor XY from the main canvas.
*   Stores the last received `(X, Y)` from the master canvas.
*   Maintains a **virtual cursor** (`QgsVertexMarker`).
*   Applies photogrammetric transformations using `TrfWldToPrjPln`.
*   Renders stereo imagery and geometry.

***

#### 4.3.3 Public Interface

```python
def update_cursor_with_z(self, z: float)
```

*   Receives the global Z value from the window.
*   Reprojects `(X, Y, Z)` through the photogrammetric model.
*   Updates the virtual cursor position.
*   Does not store Z.

***

## 5. Mathematical Model (`transform.py`)

Although not a UI component, `transform.py` is fundamental.

`TrfWldToPrjPln`:

*   Parses photogrammetric parameters from SIGRID headers.
*   Implements:
    *   World (X, Y, Z) ? Photo coordinates
    *   Photo ? Projection plane
*   Reused by:
    *   Geometry Generator expressions.
    *   Cursor reprojection logic.

The model is **pure mathematics**, UI-agnostic and deterministic.

***

## 6. Interaction Flow

### 6.1 Cursor Movement (XY)

1.  User moves the mouse.
2.  QGIS main canvas emits `xyCoordinates`.
3.  Each plugin canvas stores the last `(X, Y)`.
4.  No Z logic involved.

***

### 6.2 Depth Control (ALT + Wheel)

1.  User rotates the wheel while holding ALT.
2.  `QSgdSwmWindow.eventFilter()` captures the event.
3.  A Z increment is computed.
4.  `z_cursor` is updated.
5.  Status bar label is refreshed.
6.  All canvases receive the new Z value.
7.  Each canvas reprojects the cursor in 3D.

Result:  
**Stereo cursor shifts up/down without moving the mouse.**

***

## 7. Architectural Advantages

*   Single source of truth for Z.
*   No state duplication across views.
*   Clean master-slave relationship.
*   Strict MVC-like separation.
*   Robust against Qt 6 / QGIS 4 changes.
*   Scales to additional views or modes.

***

## 8. Design Rule Summary

> *   **Input & shared state ? Controller (`QSgdSwmWindow`)**
> *   **Rendering & visualization ? Views (`QgsSgdSwmCanvas`)**
> *   **Mathematics ? Model (`TrfWldToPrjPln`)**

This rule is consistently applied throughout SWM-3D.

***

## 9. Architectural Status

+ Coherent  
+ Robust  
+ Scalable  
+ QGIS-native  
+ Ready for long-term maintenance

***

**End of ARCHITECTURE.md**
