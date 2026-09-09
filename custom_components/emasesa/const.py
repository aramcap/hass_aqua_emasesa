"""Constantes de la integración EMASESA."""

from __future__ import annotations

DOMAIN = "emasesa"

CONF_NAME_DEFAULT = "EMASESA"

# Identificador de "dispositivo de confianza" que persiste el config
# entry. Se genera una única vez (config_flow.py) y se reutiliza en
# TODOS los logins futuros: es lo que le permite a EMASESA no volver a
# pedir el código SMS después de la verificación inicial.
CONF_DEVICE_ID = "device_id"

# Cuántos días hacia atrás se piden en cada actualización. EMASESA solo
# permite consultar la telelectura de los últimos meses; 14 días es de
# sobra para no perder ningún día nuevo entre actualizaciones (incluso si
# la integración lleva un tiempo sin poder conectar) sin pedir un rango
# enorme en cada refresco.
CONSULTA_VENTANA_DIAS = 14

CONF_SCAN_INTERVAL_MINUTES = "scan_interval_minutes"
DEFAULT_SCAN_INTERVAL_MINUTES = 360
MIN_SCAN_INTERVAL_MINUTES = 15
MAX_SCAN_INTERVAL_MINUTES = 1440

STORAGE_VERSION = 1
STORAGE_KEY_FMT = f"{DOMAIN}_{{entry_id}}"
