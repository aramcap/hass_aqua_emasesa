"""Cosas comunes a todas las entidades de la integración."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


def emasesa_device_info(entry: ConfigEntry) -> DeviceInfo:
    """
    Dispositivo único bajo el que se agrupan las entidades de una cuenta.

    Vive aquí, y no en la entidad base de `sensor.py`, porque lo comparten
    plataformas distintas (sensores y botones) que no pueden heredar de la
    misma clase base: cada una tiene que heredar de la suya
    (`SensorEntity`, `ButtonEntity`).
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="EMASESA",
        model="Telelectura (Oficina Online, no oficial)",
        entry_type=DeviceEntryType.SERVICE,
    )
