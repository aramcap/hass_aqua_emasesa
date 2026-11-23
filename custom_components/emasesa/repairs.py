"""
Reparaciones: eliminar una serie de estadísticas huérfana.

Home Assistant descubre este fichero por su nombre y llama a
`async_create_fix_flow` cuando el usuario pulsa «Reparar» en una incidencia
creada con `is_fixable=True` (ver `huerfanas.py`).

El flujo es de un solo paso con confirmación explícita, porque borrar la
serie es irreversible. Antes de mostrar la confirmación Y otra vez al
confirmar se vuelve a comprobar, contra lo que hay en ese momento en el
recorder, que la serie sigue siendo una huérfana: el usuario puede tardar en
pulsar, y mientras tanto se ha podido volver a añadir una cuenta o borrar la
serie por otra vía.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .huerfanas import (
    PREFIJO_INCIDENCIA,
    async_borrar_serie,
    async_describir_ultimo_dato,
    async_listar_huerfanas,
    placeholders_de_huerfana,
)

_LOGGER = logging.getLogger(__name__)


class BorrarSerieHuerfanaFlow(RepairsFlow):
    """Pide confirmación y elimina la serie huérfana de la incidencia."""

    async def async_step_init(self, user_input: dict[str, str] | None = None):
        """Primer paso: va directo a la confirmación."""
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, str] | None = None):
        """Muestra qué se va a borrar y, al confirmar, lo borra."""
        statistic_id = (self.data or {}).get("statistic_id")
        if not isinstance(statistic_id, str) or not statistic_id:
            return self._abortar_y_retirar("datos_invalidos")

        try:
            huerfanas, activas = await async_listar_huerfanas(self.hass)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("No se ha podido consultar el recorder", exc_info=True)
            return self.async_abort(reason="recorder_no_disponible")

        huerfana = next((h for h in huerfanas if h["statistic_id"] == statistic_id), None)
        if huerfana is None:
            # Ya no existe, o ya no es huérfana (p. ej. se ha vuelto a añadir
            # la cuenta): no hay nada que borrar y la incidencia sobra.
            return self._abortar_y_retirar("ya_no_es_huerfana")

        if user_input is None:
            ultimo_dato = await async_describir_ultimo_dato(self.hass, statistic_id)
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=placeholders_de_huerfana(huerfana, activas, ultimo_dato),
            )

        try:
            await async_borrar_serie(self.hass, statistic_id)
        except ValueError:
            return self._abortar_y_retirar("ya_no_es_huerfana")
        except TimeoutError:
            # La orden queda encolada y se ejecutará, pero no está confirmado:
            # se deja la incidencia para poder reintentar, y reintentar es
            # inocuo porque borrar algo que ya no existe no falla.
            return self.async_abort(reason="tiempo_agotado")

        # Al terminar sin abortar, Reparaciones retira la incidencia sola.
        return self.async_create_entry(data={})

    def _abortar_y_retirar(self, motivo: str):
        """Aborta y quita la incidencia, que ya no tiene sentido (abortar no la quita)."""
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        return self.async_abort(reason=motivo)


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Crea el flujo de reparación de una incidencia de esta integración."""
    if issue_id.startswith(PREFIJO_INCIDENCIA):
        return BorrarSerieHuerfanaFlow()
    raise ValueError(f"Incidencia de EMASESA desconocida: {issue_id!r}")
