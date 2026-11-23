"""
Series de estadística huérfanas: aviso, incidencia en Reparaciones y borrado.

El `statistic_id` de la estadística externa horaria cuelga del `entry_id`
de la cuenta (ver `statistics.statistic_id_de`), pero su nombre visible solo
del título. Si se borra la integración y se vuelve a añadir, la cuenta nueva
recibe un `entry_id` distinto: escribe en una serie nueva y la antigua se
queda en el recorder con exactamente el MISMO nombre, sin que nada escriba
ya en ella. En los selectores (p. ej. el Panel de Energía) las dos se ven
idénticas, y si se elige la equivocada el panel no muestra nunca datos
nuevos aunque la integración funcione perfectamente.

Este módulo lo detecta al arrancar, lo dice en el registro y abre una
incidencia en Reparaciones desde la que el usuario puede eliminar la serie
(el flujo está en `repairs.py`). Nunca se borra nada sin una confirmación
explícita.

Sobre la seguridad del borrado: el recorder borra por `statistic_id` y NADA
más, sin mirar de quién es la serie. Por eso `async_borrar_serie` valida por
su cuenta, aunque el llamante ya lo haya hecho, que el id es de esta
integración y que no pertenece a ninguna cuenta configurada.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_list_statistic_ids,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ZONA_EMASESA
from .statistics import SUFIJO_SERIE_HORARIA, _inicio_a_datetime, statistic_id_de

_LOGGER = logging.getLogger(__name__)

# Claves de `hass.data` con lo ya hecho en esta ejecución. Recargar la
# integración (p. ej. al cambiar una opción) la vuelve a arrancar, y sin esto
# repetiría el mismo aviso en el registro cada vez.
_CLAVE_AVISADAS = f"{DOMAIN}_huerfanas_avisadas"
# Incidencias creadas, para retirar las que dejan de tener sentido (p. ej. si
# la serie se borra por otra vía mientras Home Assistant sigue en marcha).
_CLAVE_INCIDENCIAS = f"{DOMAIN}_huerfanas_incidencias"

PREFIJO_INCIDENCIA = "serie_huerfana_"
CLAVE_TRADUCCION_INCIDENCIA = "serie_huerfana"

# Mismo plazo que usa Home Assistant al esperar este mismo borrado.
SEGUNDOS_ESPERA_BORRADO = 10


def _id_es_de_emasesa(statistic_id: str) -> bool:
    """Si un `statistic_id` tiene la forma de los que crea esta integración."""
    return (
        isinstance(statistic_id, str)
        and statistic_id.startswith(f"{DOMAIN}:")
        and statistic_id.endswith(SUFIJO_SERIE_HORARIA)
    )


def _es_serie_horaria_de_emasesa(estadistica: dict) -> bool:
    """Si una estadística del recorder es de las que crea esta integración."""
    return estadistica.get("source") == DOMAIN and _id_es_de_emasesa(
        estadistica.get("statistic_id") or ""
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


def incidencia_de(statistic_id: str) -> str:
    """Identificador de la incidencia de Reparaciones de una serie huérfana."""
    return PREFIJO_INCIDENCIA + statistic_id.split(":", 1)[1]


def _esperadas(hass: HomeAssistant) -> set[str]:
    """
    `statistic_id` de las cuentas configuradas. `async_entries` incluye por
    defecto las deshabilitadas: la serie de una cuenta deshabilitada no es
    huérfana, se reanudará al volver a habilitarla.
    """
    return {statistic_id_de(e.entry_id) for e in hass.config_entries.async_entries(DOMAIN)}


async def async_listar_huerfanas(hass: HomeAssistant) -> tuple[list[dict], list[dict]]:
    """
    Devuelve (huerfanas, activas) según lo que hay AHORA en el recorder.
    Propaga cualquier fallo del recorder: decide el llamante.
    """
    esperadas = _esperadas(hass)
    existentes = await async_list_statistic_ids(hass)
    huerfanas = encontrar_huerfanas(existentes, esperadas)
    activas = [
        e for e in existentes if _es_serie_horaria_de_emasesa(e) and e["statistic_id"] in esperadas
    ]
    _LOGGER.debug(
        "Series de EMASESA en el recorder: %d; esperadas por las cuentas configuradas: %d; "
        "huérfanas: %d",
        sum(1 for e in existentes if _es_serie_horaria_de_emasesa(e)),
        len(esperadas),
        len(huerfanas),
    )
    return huerfanas, activas


async def async_describir_ultimo_dato(hass: HomeAssistant, statistic_id: str) -> str:
    """
    Cuándo es el último punto guardado de la serie y cuánto acumulaba, para
    que quien va a borrarla pueda juzgar qué contiene. Nunca falla: si no se
    puede saber, lo dice.
    """
    try:
        ultimas = await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, statistic_id, False, {"sum"}
        )
        fila = (ultimas.get(statistic_id) or [None])[0]
        if not fila:
            return "No contiene ningún punto."
        inicio = _inicio_a_datetime(fila["start"]).astimezone(ZONA_EMASESA)
        return (
            f"Su último dato es del {inicio:%d/%m/%Y %H:%M} "
            f"(acumulado: {float(fila.get('sum') or 0.0):.1f} L)."
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("No se ha podido leer el último dato de %s", statistic_id, exc_info=True)
        return "No se ha podido leer su último dato."


def describir_activas(activas: list[dict]) -> str:
    """La frase que dice cuál es la serie buena, o que aún no existe."""
    if not activas:
        return "La serie de la cuenta actual aún no existe: se creará con la primera importación."
    return "La serie que sí recibe datos es " + " / ".join(
        f"{a['statistic_id']} ('{a.get('name')}')" for a in activas
    ) + "."


def placeholders_de_huerfana(huerfana: dict, activas: list[dict], ultimo_dato: str) -> dict[str, str]:
    """
    Textos que rellenan la incidencia y su flujo de reparación. Todos deben
    ser `str`: es lo que exige Home Assistant. Las plantillas de
    `strings.json` solo pueden usar estas claves (hay un test que lo vigila).
    """
    statistic_id = huerfana["statistic_id"]
    return {
        "statistic_id": statistic_id,
        "nombre": str(huerfana.get("name") or statistic_id),
        "id_corto": statistic_id.split(":", 1)[1][: -len(SUFIJO_SERIE_HORARIA)][:8],
        "activa": describir_activas(activas),
        "ultimo_dato": ultimo_dato,
    }


def mensaje_de_huerfana(huerfana: dict, activas: list[dict]) -> str:
    """Texto del aviso del registro para una serie huérfana."""
    return (
        f"Hay una estadística de EMASESA que no corresponde a ninguna cuenta configurada: "
        f"{huerfana['statistic_id']} ('{huerfana.get('name')}'). Procede de una cuenta "
        f"que ya no está configurada (por ejemplo, eliminada y vuelta a añadir): nada "
        f"escribe ya en ella, así que si el Panel de Energía o una tarjeta de "
        f"estadísticas la usa, no verá datos nuevos. {describir_activas(activas)} "
        f"Comprueba que usas esa y no ésta: si se llaman igual, solo se distinguen por "
        f"el identificador. Esta integración no borra nada por su cuenta; puedes "
        f"eliminarla desde Ajustes → Reparaciones."
    )


async def async_borrar_serie(hass: HomeAssistant, statistic_id: str) -> None:
    """
    Elimina una serie huérfana del recorder y espera a que termine.

    Es IRREVERSIBLE y el recorder no mira de quién es la serie, así que aquí
    se comprueba, aunque el llamante ya lo haya hecho, que el id es de esta
    integración y que NO pertenece a ninguna cuenta configurada. Lanza
    `ValueError` si no cumple (sin tocar el recorder) y `TimeoutError` si el
    recorder no confirma a tiempo (la orden queda encolada y se ejecutará).
    """
    if not _id_es_de_emasesa(statistic_id):
        raise ValueError(f"{statistic_id!r} no es una serie de EMASESA")
    if statistic_id in _esperadas(hass):
        raise ValueError(f"{statistic_id!r} pertenece a una cuenta configurada")

    hecho = asyncio.Event()

    def _al_terminar() -> None:
        # El recorder lo llama desde su propio hilo.
        hass.loop.call_soon_threadsafe(hecho.set)

    get_instance(hass).async_clear_statistics([statistic_id], on_done=_al_terminar)
    async with asyncio.timeout(SEGUNDOS_ESPERA_BORRADO):
        await hecho.wait()
    _LOGGER.info("Eliminada la serie de estadísticas huérfana %s", statistic_id)


def _sincronizar_incidencias(
    hass: HomeAssistant, huerfanas: list[dict], vigentes: dict[str, dict[str, str]]
) -> None:
    """Crea la incidencia de cada huérfana y retira las que ya no aplican."""
    creadas: set[str] = hass.data.setdefault(_CLAVE_INCIDENCIAS, set())

    for huerfana in huerfanas:
        id_incidencia = incidencia_de(huerfana["statistic_id"])
        ir.async_create_issue(
            hass,
            DOMAIN,
            id_incidencia,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=CLAVE_TRADUCCION_INCIDENCIA,
            translation_placeholders=vigentes[huerfana["statistic_id"]],
            data={"statistic_id": huerfana["statistic_id"]},
        )
        creadas.add(id_incidencia)

    actuales = {incidencia_de(h["statistic_id"]) for h in huerfanas}
    for obsoleta in creadas - actuales:
        ir.async_delete_issue(hass, DOMAIN, obsoleta)
        creadas.discard(obsoleta)


async def async_avisar_de_series_huerfanas(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    Avisa en el registro (una vez por ejecución) y abre una incidencia en
    Reparaciones por cada serie huérfana.

    Nunca debe impedir que arranque la integración: cualquier fallo al
    consultar el recorder se registra en depuración y se ignora.
    """
    try:
        huerfanas, activas = await async_listar_huerfanas(hass)
        vigentes = {
            h["statistic_id"]: placeholders_de_huerfana(
                h, activas, await async_describir_ultimo_dato(hass, h["statistic_id"])
            )
            for h in huerfanas
        }
        _sincronizar_incidencias(hass, huerfanas, vigentes)
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "No se ha podido comprobar si hay series huérfanas (entrada %s)",
            entry.entry_id,
            exc_info=True,
        )
        return

    ya_avisadas: set[str] = hass.data.setdefault(_CLAVE_AVISADAS, set())
    for huerfana in huerfanas:
        if huerfana["statistic_id"] in ya_avisadas:
            continue
        ya_avisadas.add(huerfana["statistic_id"])
        _LOGGER.warning("%s", mensaje_de_huerfana(huerfana, activas))
