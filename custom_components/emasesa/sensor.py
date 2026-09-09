"""Sensores de la integración EMASESA."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EmasesaCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EmasesaCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            EmasesaLatestDaySensor(coordinator, entry),
            EmasesaCumulativeSensor(coordinator, entry),
        ]
    )


class EmasesaBaseEntity(CoordinatorEntity[EmasesaCoordinator], SensorEntity):
    """Entidad base: agrupa los sensores bajo un único dispositivo EMASESA."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EmasesaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="EMASESA",
            model="Telelectura (Oficina Online, no oficial)",
            entry_type=DeviceEntryType.SERVICE,
        )


class EmasesaLatestDaySensor(EmasesaBaseEntity):
    """Litros del último día con lectura disponible (normalmente ayer)."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_icon = "mdi:water"
    _attr_translation_key = "ultimo_dia"

    def __init__(self, coordinator: EmasesaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ultimo_dia"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        latest = data.get("latest")
        return latest["litros"] if latest else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        latest = data.get("latest")
        readings = data.get("readings") or []
        return {
            "fecha": latest["date"].isoformat() if latest else None,
            "historico_reciente": {r["date"].isoformat(): r["litros"] for r in readings},
        }


class EmasesaCumulativeSensor(EmasesaBaseEntity):
    """
    Contador acumulado (nunca decrece) para poder añadir el agua al
    Panel de Energía de Home Assistant. EMASESA solo da consumos por
    día/hora, no una lectura de contador continua, así que este valor lo
    construye y persiste la propia integración (ver coordinator.py):
    suma cada día nuevo que aparece, una sola vez, y lo guarda en disco
    para no perder el acumulado al reiniciar Home Assistant.
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_icon = "mdi:water-plus"
    _attr_translation_key = "consumo_acumulado"

    def __init__(self, coordinator: EmasesaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_acumulado"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        return data.get("cumulative_total")
