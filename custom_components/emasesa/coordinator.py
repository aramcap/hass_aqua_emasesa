"""DataUpdateCoordinator para la integración EMASESA."""

from __future__ import annotations

import logging
from datetime import date, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EmasesaApiClient, EmasesaApiError, EmasesaAuthError, EmasesaTwoFactorRequired
from .const import CONSULTA_VENTANA_DIAS, DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

# Cuántas fechas "ya contadas" se conservan como máximo en el
# almacenamiento persistente del contador acumulado. Con una fecha por
# día, 400 cubre más de un año de histórico — de sobra para no volver a
# sumar por error un día que ya se contó, sin dejar crecer el fichero
# indefinidamente.
_MAX_FECHAS_RECORDADAS = 400


class EmasesaCoordinator(DataUpdateCoordinator[dict]):
    """Consulta periódicamente EMASESA y mantiene un contador acumulado."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: EmasesaApiClient, update_interval_minutes: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=update_interval_minutes),
        )
        self.api = api
        self._entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        self._cumulative_total: float = 0.0
        self._counted_dates: set[str] = set()
        self._storage_loaded = False

    async def async_load_storage(self) -> None:
        """Carga el contador acumulado persistido (llamar antes del primer refresh)."""
        data = await self._store.async_load()
        if data:
            self._cumulative_total = float(data.get("total", 0.0))
            self._counted_dates = set(data.get("counted_dates", []))
            _LOGGER.debug(
                "Contador EMASESA restaurado: %.1f L, %d días contados",
                self._cumulative_total,
                len(self._counted_dates),
            )
        self._storage_loaded = True

    async def _async_save_storage(self) -> None:
        await self._store.async_save(
            {"total": self._cumulative_total, "counted_dates": sorted(self._counted_dates)}
        )

    async def _async_update_data(self) -> dict:
        if not self._storage_loaded:
            await self.async_load_storage()

        hasta = date.today()
        desde = hasta - timedelta(days=CONSULTA_VENTANA_DIAS - 1)

        try:
            readings = await self.api.async_get_daily_readings(desde, hasta)
        except EmasesaTwoFactorRequired as err:
            # El device_id de confianza ha dejado de serlo (revocado por
            # EMASESA, contraseña cambiada, etc.). No hay forma de
            # completar esto sin que un humano introduzca el código SMS,
            # así que se pide un reauth: Home Assistant mostrará una
            # notificación para reconfigurar la integración, que repite
            # el mismo paso de verificación que en el alta inicial.
            _LOGGER.warning(
                "EMASESA ha revocado la confianza en este dispositivo; "
                "hace falta reautenticar la integración"
            )
            self._entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed(str(err)) from err
        except EmasesaAuthError as err:
            raise UpdateFailed(f"Autenticación rechazada por EMASESA: {err}") from err
        except EmasesaApiError as err:
            raise UpdateFailed(f"Error consultando EMASESA: {err}") from err

        changed = False
        for r in readings:
            key = r["date"].isoformat()
            if key not in self._counted_dates:
                self._cumulative_total += r["litros"]
                self._counted_dates.add(key)
                changed = True

        if changed:
            if len(self._counted_dates) > _MAX_FECHAS_RECORDADAS:
                self._counted_dates = set(sorted(self._counted_dates)[-_MAX_FECHAS_RECORDADAS:])
            await self._async_save_storage()

        return {
            "readings": readings,
            "latest": readings[-1] if readings else None,
            "cumulative_total": round(self._cumulative_total, 1),
        }
