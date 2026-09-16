"""
Botón de diagnóstico: lanza a mano la lectura periódica de EMASESA.

Ejecuta exactamente lo mismo que la actualización automática (cada
`scan_interval_minutes`, 6 horas por defecto): consulta la ventana corta
de días, actualiza los sensores y añade a la estadística externa horaria
las franjas posteriores al último punto importado.

Sirve para no tener que esperar al siguiente ciclo cuando se está
comprobando si EMASESA ya ha publicado un día nuevo, o cuando se está
mirando el registro para diagnosticar por qué no llega algo. Para
recuperar histórico o rellenar huecos NO es esto lo que hace falta, sino
la carga masiva manual de las opciones de la integración (ver
`config_flow.py`), que reescribe un tramo entero en vez de añadir solo lo
nuevo.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EmasesaCoordinator
from .entity import emasesa_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EmasesaCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([EmasesaRefreshButton(coordinator, entry)])


class EmasesaRefreshButton(ButtonEntity):
    """Lanza una lectura igual que la que dispara el temporizador."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:database-refresh"
    _attr_translation_key = "forzar_lectura"

    def __init__(self, coordinator: EmasesaCoordinator, entry: ConfigEntry) -> None:
        self.coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_forzar_lectura"

    # Deliberadamente NO hereda de `CoordinatorEntity`: esa clase marca la
    # entidad como no disponible cuando la última actualización falló, que
    # es justo cuando más falta hace poder pulsar el botón para reintentar
    # y ver qué pasa en el registro.

    @property
    def device_info(self) -> DeviceInfo:
        return emasesa_device_info(self._entry)

    async def async_press(self) -> None:
        _LOGGER.debug("Lectura de EMASESA lanzada a mano desde el botón de diagnóstico")
        await self.coordinator.async_refresh()
        if not self.coordinator.last_update_success:
            # `async_refresh()` no propaga el error (solo lo registra y
            # marca la actualización como fallida), así que se levanta
            # aquí para que la interfaz avise en vez de quedarse callada.
            raise HomeAssistantError(
                "La lectura de EMASESA lanzada a mano ha fallado: "
                f"{self.coordinator.last_exception}"
            )
