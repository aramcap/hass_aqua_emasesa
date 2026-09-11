"""Constantes de la integración EMASESA."""

from __future__ import annotations

DOMAIN = "emasesa"

CONF_NAME_DEFAULT = "EMASESA"

# Identificador de "dispositivo de confianza" que persiste el config
# entry. Se genera una única vez (config_flow.py) y se reutiliza en
# TODOS los logins futuros: es lo que le permite a EMASESA no volver a
# pedir el código SMS después de la verificación inicial.
CONF_DEVICE_ID = "device_id"

# Ventana de la actualización periódica (cada scan_interval_minutes):
# 2 días, nunca 1 — si `desde == hasta`, EMASESA cambia a vista horaria
# en vez de diaria (ver api.py), lo que rompería esta consulta. En la
# práctica esto solo trae el día más reciente que EMASESA ha publicado
# desde la última actualización.
LECTURA_DIARIA_VENTANA_DIAS = 2

# Carga masiva: trae de golpe el histórico de varios días. Se ejecuta
# sola la primera vez que se da de alta la integración (para no
# arrancar con el Panel de Energía vacío) y se puede volver a lanzar a
# mano desde las opciones de la integración, ajustando cuántos días
# hacia atrás cubre, para solucionar huecos de una carga anterior que
# haya fallado a medias.
CARGA_MASIVA_DIAS_DEFECTO = 30
CARGA_MASIVA_MIN_DIAS = 2
CARGA_MASIVA_MAX_DIAS = 90

CONF_SCAN_INTERVAL_MINUTES = "scan_interval_minutes"
DEFAULT_SCAN_INTERVAL_MINUTES = 360
MIN_SCAN_INTERVAL_MINUTES = 15
MAX_SCAN_INTERVAL_MINUTES = 1440

CONF_CARGA_MASIVA_DIAS = "dias"

STORAGE_VERSION = 1
STORAGE_KEY_FMT = f"{DOMAIN}_{{entry_id}}"
