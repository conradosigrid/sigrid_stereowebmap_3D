# Sincronización Automática de Map Canvas Items

Esta funcionalidad sincroniza automáticamente todos los elementos de dibujo (rubber bands, vertex markers, etc.) entre el canvas principal de QGIS y los canvas estereoscópicos secundarios (Left y Right).

## Características

### Elementos Sincronizados
- **QgsVertexMarker**: Marcadores de vértices con todas sus propiedades (color, tamaño, tipo de icono, grosor)
- **QgsRubberBand**: Bandas de goma con geometría y propiedades de estilo
- **Transformación 3D**: Cuando está disponible la transformación de perspectiva, se aplica automáticamente a los elementos sincronizados

### Funcionamiento Automático
- **Detección automática**: Monitorea cambios en los map canvas items cada 200ms
- **Sincronización en tiempo real**: Los cambios aparecen inmediatamente en los canvas secundarios
- **Transformación de coordenadas**: Aplica proyección 3D cuando está configurada

## Control Manual

### Habilitar/Deshabilitar Sincronización
```python
# Desde el objeto window principal
window.set_canvas_items_sync_enabled(True)   # Habilitar
window.set_canvas_items_sync_enabled(False)  # Deshabilitar

# Desde canvas individual
canvas_left.set_canvas_items_sync_enabled(True)
canvas_right.set_canvas_items_sync_enabled(True)
```

### Forzar Sincronización Inmediata
```python
# Forzar sincronización en todos los canvas
window._sync_canvases_items()

# Forzar en canvas específico
canvas_left.force_sync_canvas_items()
canvas_right.force_sync_canvas_items()
```

## Arquitectura Técnica

### Componentes Principales
1. **Timer de Monitoreo**: Verifica cambios en el canvas principal cada 200ms
2. **Mapeo de Items**: Diccionario que relaciona items originales con sus copias sincronizadas
3. **Sincronización de Propiedades**: Copia automática de estilos, colores y geometría
4. **Transformación 3D**: Integración con el sistema de proyección estereoscópica

### Gestión de Memoria
- **Cleanup automático**: Los recursos se liberan automáticamente al cerrar
- **Detección de items eliminados**: Los items sincronizados se eliminan cuando el original desaparece
- **Manejo de excepciones**: Control robusto de errores con logging

## Casos de Uso

### Herramientas de Dibujo
Cuando usas herramientas de QGIS para dibujar (líneas, polígonos, puntos), los elementos aparecen automáticamente en ambos canvas estereoscópicos.

### Mediciones
Las herramientas de medición de QGIS mostrarán sus resultados en los tres canvas simultáneamente.

### Selecciones y Highlight
Las selecciones de features y highlights se reflejarán en todos los canvas.

### Digitización
Durante la digitización, los elementos temporales (rubber bands) aparecen en tiempo real en los canvas secundarios.

## Rendimiento

- **Optimización de timers**: Frecuencia ajustada para equilibrar responsividad y uso de CPU
- **Transformaciones eficientes**: Cache de transformaciones para evitar cálculos repetitivos  
- **Actualizaciones incrementales**: Solo sincroniza cambios, no todo el conjunto de items

## Extensibilidad

Para añadir soporte para nuevos tipos de map canvas items:

1. Modifica `_create_synced_item()` para añadir el nuevo tipo
2. Implementa `_sync_[tipo]_properties()` para sincronizar propiedades específicas
3. Añade transformación 3D en `_transform_geometry()` si es necesario

## Troubleshooting

### Items No Sincronizados
- Verifica que la sincronización esté habilitada
- Comprueba los logs en "SWM-3D" para errores
- Fuerza sincronización manual si es necesario

### Rendimiento Lento
- Considera deshabilitar temporalmente la sincronización durante operaciones pesadas
- Ajusta la frecuencia del timer si es necesario (modificar valores en código)

### Transformaciones Incorrectas  
- Verifica que `trf_wld2prp` esté correctamente configurado
- Comprueba los valores Z del cursor para las transformaciones de perspectiva