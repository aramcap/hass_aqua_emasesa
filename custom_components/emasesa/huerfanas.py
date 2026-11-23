"""
Aviso de series huérfanas.

El `statistic_id` de la estadística externa horaria cuelga del `entry_id`
de la cuenta (ver `statistics.statistic_id_de`), pero su nombre visible solo
del título. Si se borra la integración y se vuelve a añadir, la cuenta nueva
recibe un `entry_id` distinto: escribe en una serie nueva y la antigua se
queda en el recorder con exactamente el MISMO nombre, sin que nada escriba
ya en ella. En los selectores (p. ej. el Panel de Energía) las dos se ven
idénticas, y si se elige la equivocada el panel no muestra nunca datos
nuevos aunque la integración funcione perfectamente.

Este módulo lo detecta al arrancar y lo dice en el registro, con el
identificador de cada serie para poder distinguirlas. No borra nada: eliminar
datos históricos es decisión del usuario.
"""

from __future__ import annotations

import logging

from homeassistant.components.recorder.statistics import async_list_statistic_ids
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .statistics import SUFIJO_SERIE_HORARIA, statistic_id_de

_LOGGER = logging.getLogger(__name__)

# Clave de `hass.data` con los ids ya avisados en esta ejecución. Recargar la
# integración (p. ej. al cambiar una opción) la vuelve a arrancar, y sin esto
# repetiría el mismo aviso cada vez.
_CLAVE_AVISADAS = f"{DOMAIN}_huerfanas_avisadas"


def _es_serie_horaria_de_emasesa(estadistica: dict) -> bool:
    """Si una estadística del recorder es de las que crea esta integración."""
    statistic_id = estadistica.get("statistic_id") or ""
    return (
        estadistica.get("source") == DOMAIN
        and statistic_id.startswith(f"{DOMAIN}:")
        and statistic_id.endswith(SUFIJO_SERIE_HORARIA)
    )


def encontrar_huerfanas(estadisticas: list[dict], esperadas: set[str]) -> list[dict]:
    """
    Series horarias de esta integración que no corresponden a ninguna cuenta
    configurada, ordenadas por id. `esperadas` son los `statistic_id` de las
    cuentas que existen (habilitadas o no).

    Se ignora todo lo que no es una serie de esta integración: estadísticas
    de entidades, de otras integraciones y cualquier serie externa propia con
    otro sufijo que se pudiera añadir en el futuro.
    """
    return sorted(
        (
            e
            for e in estadisticas
            if _es_serie_horaria_de_emasesa(e) and e["statistic_id"] not in esperadas
        ),
        key=lambda e: e["statistic_id"],
    )


def mensaje_de_huerfana(huerfana: dict, activas: list[dict]) -> str:
    """Texto del aviso para una serie huérfana, con lo necesario para actuar."""
    if activas:
        activa = "La serie que sí recibe datos es " + " / ".join(
            f"{a['statistic_id']} ('{a.get('name')}')" for a in activas
        )
    else:
        activa = "La serie de la cuenta actual aún no existe: se creará con la primera importación"
    return (
        f"Hay una estadística de EMASESA que no corresponde a ninguna cuenta configurada: "
        f"{huerfana['statistic_id']} ('{huerfana.get('name')}'). Procede de una cuenta "
        f"que ya no está configurada (por ejemplo, eliminada y vuelta a añadir): nada "
        f"escribe ya en ella, así que si el Panel de Energía o una tarjeta de "
        f"estadísticas la usa, no verá datos nuevos. {activa}. Comprueba que usas esa y "
        f"no ésta: si se llaman igual, solo se distinguen por el identificador. Esta "
        f"integración no borra nada por su cuenta; si no la "
        f"necesitas, se elimina con el comando de websocket 'recorder/clear_statistics' "
        f"(statistic_ids: ['{huerfana['statistic_id']}'])."
    )


async def async_avisar_de_series_huerfanas(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    Registra un aviso por cada serie huérfana, una sola vez por ejecución.

    Nunca debe impedir que arranque la integración: cualquier fallo al
    consultar el recorder se registra en depuración y se ignora.
    """
    try:
        # `async_entries` incluye por defecto las cuentas deshabilitadas: la
        # serie de una cuenta deshabilitada no es huérfana, se reanudará al
        # volver a habilitarla.
        esperadas = {statistic_id_de(e.entry_id) for e in hass.config_entries.async_entries(DOMAIN)}
        existentes = await async_list_statistic_ids(hass)
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "No se ha podido comprobar si hay series huérfanas (entrada %s)",
            entry.entry_id,
            exc_info=True,
        )
        return

    huerfanas = encontrar_huerfanas(existentes, esperadas)
    _LOGGER.debug(
        "Series de EMASESA en el recorder: %d; esperadas por las cuentas configuradas: %d; "
        "huérfanas: %d",
        sum(1 for e in existentes if _es_serie_horaria_de_emasesa(e)),
        len(esperadas),
        len(huerfanas),
    )
    if not huerfanas:
        return

    activas = [
        e for e in existentes if _es_serie_horaria_de_emasesa(e) and e["statistic_id"] in esperadas
    ]
    ya_avisadas: set[str] = hass.data.setdefault(_CLAVE_AVISADAS, set())
    for huerfana in huerfanas:
        if huerfana["statistic_id"] in ya_avisadas:
            continue
        ya_avisadas.add(huerfana["statistic_id"])
        _LOGGER.warning("%s", mensaje_de_huerfana(huerfana, activas))
