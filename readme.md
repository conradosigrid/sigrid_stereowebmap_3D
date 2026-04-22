***
# SWM-3D – Plugin QGIS - Nueva versión 1.0

## 1. Estado actual


La presente versión del plugin **SWM-3D** se instala sobre **QGIS 4** y permite conectarse al nuevo servicio WMS dado de alta en un servidor interno de TRAGSATEC.  

El plugin procesa el par estereoscópico enviado por el servicio WMS y lo muestra en una **ventana secundaria** dependiente de la ventana principal de QGIS.

El movimiento de zoom se realiza siempre sobre la ventana principal de QGIS y se refleja automáticamente en la secundaria de manera estereoscópica.  
La **rueda del ratón + tecla ALT**, actuando sobre la ventana principal, permite modificar la **Z del cursor**, que se refleja dinámicamente en la visualización estereoscópica.


---

## 2. Instalación

### 2.1 Requisitos

- **QGIS 4.x** (basado en Qt 6). 
  El desarrollo actual se ha efectuado con Windows Offline (Standalone) installer para 4.0.1 Norrköping, porque la instalación desde OSGEO4W más reciente no deja una versión DEV del QGIS4 que se pueda ejecutar automáticamente. Se explica en más detalle en el archivo `documentos/20260420 Instalación de QGIS4 y SWM-3D.docx`

```
https://www.qgis.org/resources/installation-guide/
```

Posiblemente esto cambiará en breve y la instalación desde OSGE4W volvería a ser la más recomendable:

```
https://trac.osgeo.org/osgeo4w/
```

- Sistema operativo **Windows**  
- Perfil de QGIS activo (por ejemplo `QGIS_4_DEV`)
- Acceso al servicio WMS fotogramétrico SWM desde:

```
https://fenix3d-des.tragsatec.es:8083/  -> pruebas
https://fenix3d-des.tragsatec.es:8084/  -> desarrollo
```

---

### 2.2 Instalación manual del plugin (modo desarrollo)

Actualmente el plugin se distribuye como **código fuente**, por lo que la instalación se realiza copiando el directorio del plugin al perfil de usuario de QGIS.

1. Localizar el directorio de plugins del perfil de QGIS.  
   Por defecto en Windows:
   
```
C:\Users\USUARIO\AppData\Roaming\QGIS\QGIS4\profiles\QGIS_4_DEV\python\plugins
```

2. Copiar el directorio completo del plugin en dicha ruta, de forma que quede:


```
C:\Users\USUARIO\AppData\Roaming\QGIS\QGIS4\profiles\QGIS_4_DEV\python\plugins\SWM_3D
```

3. Verificar que el directorio `SWM_3D` contiene al menos los siguientes ficheros y carpetas:

```
SWM_3D/
├── init.py
├── plugin.py
├── window.py
├── canvas.py
├── transform.py
├── utils.py
├── metadata.txt
└── expressions/
    └── perspective_swm_transform.py
```

---

### 2.3 Activación en QGIS

1. Iniciar **QGIS 4**.
2. Ir a:

Complementos → Administrar e instalar complementos

3. En la pestaña **Instalados**, localizar **SWM-3D**.
4. Activar el complemento.

Si la instalación es correcta:
- El plugin aparecerá en el menú de complementos.
- Al activarlo se abrirá la ventana secundaria estereoscópica.

---

### 2.4 Observaciones importantes

- El plugin **no debe copiarse** en el directorio global de instalación de QGIS, sino **únicamente en el perfil de usuario**.
- Tras actualizar el código:
- Desactivar y volver a activar el plugin, **o**
- Usar la opción “Recargar complemento” para que QGIS lea los cambios.
- En caso de error durante la carga, revisar el panel:

```
Ver → Paneles → Registro de mensajes
```

especialmente la pestaña **SWM-3D**.

---

## 3. Cambios

Los principales cambios de esta versión son los siguientes:

* Organización de las clases ya existentes en un sistema de ficheros que recoge mejor la arquitectura del script, donde se separan más claramente componentes y responsabilidades.  
  La filosofía seguida se basa en:
  * Coherencia
  * Robustez
  * Escalabilidad
  * Enfoque *QGIS-native*
  * Mantenimiento a largo plazo

  La explicación detallada de la nueva arquitectura y de los roles de cada clase y componente  
  se recoge en el archivo `./documentos/ARCHITECTURE.md`.

* Código adaptado a **Qt 6**, el nuevo estándar en **QGIS 4**.  
  Aunque no sea la versión LTR actual, el desarrollo de **SWM-3D** se orienta a esta versión,  
  que previsiblemente será LTR en el futuro. Las mejoras introducidas en las librerías Qt 6  
  justifican esta decisión.

* Introducción de control de errores adicional para evitar *crashes* inesperados y  
  garantizar que los fallos se reporten de forma explícita y controlada.

---

## 4. Líneas futuras

Los desarrollos más inmediatos a partir de esta versión básica son:

*   Comprobar si los bugs detectados por Conrado y por Andrea/Ada en versiones anteriores se mantienen en esta versión y arquitectura. Corregir si procede.
*   Testear y afinar la representación estereoscópica sobre distintos hardwares, especialmente la doble pantalla con espejo a 45º
*   Incorporar visualizaciones con sistemas de **anaglifo** y líneas horizontales **interlazadas**
*   Empezar a incluir herramientas de delineación trabajando directamente sobre las vistas estereoscópicas


***

**End of readme.md**

C:\Users\fherl\AppData\Roaming\QGIS\QGIS4\profiles\QGIS_4_DEV\python\plugins\SWM_3D