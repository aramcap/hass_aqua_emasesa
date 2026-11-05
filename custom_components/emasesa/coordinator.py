"""DataUpdateCoordinator para la integración EMASESA."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EmasesaApiClient, EmasesaApiError, EmasesaAuthError, EmasesaTwoFactorRequired
from .const import (
    CARGA_MASIVA_DIAS_DEFECTO,
    CARGA_MASIVA_MAX_DIAS,
    CARGA_MASIVA_MIN_DIAS,
    DOMAIN,
    LECTURA_DIARIA_VENTANA_DIAS,
    STORAGE_VERSION,
)
from .statistics import EmasesaHourlyStatisticsImporter

_LOGGER = logging.getLogger(__name__)

# Cuántas fechas "ya contadas" se conservan como máximo en el
# almacenamiento persistente del contador acumulado. Con una fecha por
# día, 400 cubre más de un año de histórico — de sobra para no volver a
# sumar por error un día que ya se contó, sin dejar crecer el fichero
# indefinidamente.
_MAX_FECHAS_RECORDADAS = 400

# Cuántas de las últimas lecturas diarias se guardan en disco para que los
# sensores de último día / última fecha de lectura sigan teniendo valor
# tras reiniciar Home Assistant aunque EMASESA no devuelva nada en el
# primer refresco. Un mes de sobra cubre cualquier ventana normal.
_MAX_LECTURAS_RECORDADAS = 31


def _serializar_lecturas(lecturas: list[dict]) -> list[dict]:
    """Lecturas diarias a un formato JSON (las fechas pasan a ISO)."""
    return [
        {"date": r["date"].isoformat(), "litros": float(r["litros"])} for r in lecturas
    ]


def _deserializar_lecturas(crudo: object) -> list[dict]:
    """
    Inverso de `_serializar_lecturas`, tolerante: una entrada corrupta se
    ignora en vez de impedir que arranque la integración.
    """
    if not isinstance(crudo, list):
        return []
    lecturas: list[dict] = []
    for item in crudo:
        try:
            lecturas.append(
                {"date": date.fromisoformat(item["date"]), "litros": float(item["litros"])}
            )
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(lecturas, key=lambda r: r["date"])


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
        # Últimas lecturas diarias válidas, persistidas junto al contador.
        self._ultimas_lecturas: list[dict] = []
        self._storage_loaded = False
        # Momento (UTC) del último fetch a EMASESA que ha terminado sin
        # error, para un sensor de diagnóstico "última actualización" —
        # distinto de la fecha de la lectura en sí, que la publica
        # EMASESA con uno o dos días de retraso.
        self.last_fetch_success: datetime | None = None
        # Estadística externa del recorder con granularidad horaria
        # (ver statistics.py) — independiente del sensor
        # `consumo_acumulado`, es la fuente recomendada para el Panel
        # de Energía.
        self._hourly_stats = EmasesaHourlyStatisticsImporter(hass, entry.entry_id, entry.title, api)

    async def async_load_storage(self) -> None:
        """Carga el contador acumulado persistido (llamar antes del primer refresh)."""
        data = await self._store.async_load()
        if data:
            self._cumulative_total = float(data.get("total", 0.0))
            self._counted_dates = set(data.get("counted_dates", []))
            self._ultimas_lecturas = _deserializar_lecturas(data.get("ultimas_lecturas"))
            _LOGGER.debug(
                "Contador EMASESA restaurado: %.1f L, %d días contados, %d lectura(s) "
                "reciente(s)%s",
                self._cumulative_total,
                len(self._counted_dates),
                len(self._ultimas_lecturas),
                f" (la última, del {self._ultimas_lecturas[-1]['date'].strftime('%d/%m/%Y')})"
                if self._ultimas_lecturas
                else "",
            )
        self._storage_loaded = True

    async def _async_save_storage(self) -> None:
        await self._store.async_save(
            {
                "total": self._cumulative_total,
                "counted_dates": sorted(self._counted_dates),
                "ultimas_lecturas": _serializar_lecturas(self._ultimas_lecturas),
            }
        )

    def _recordar_lecturas(self, readings: list[dict]) -> bool:
        """
        Memoriza las últimas lecturas válidas. Una actualización SIN
        lecturas no borra lo memorizado. Devuelve si ha cambiado algo.
        """
        if not readings:
            return False
        recientes = sorted(readings, key=lambda r: r["date"])[-_MAX_LECTURAS_RECORDADAS:]
        if recientes == self._ultimas_lecturas:
            return False
        self._ultimas_lecturas = recientes
        return True

    async def _async_registrar_lecturas(self, readings: list[dict]) -> None:
        """Suma al contador lo nuevo, memoriza las lecturas y persiste si algo cambió."""
        cambio_contador = self._apply_readings(readings)
        cambio_lecturas = self._recordar_lecturas(readings)
        if cambio_contador or cambio_lecturas:
            await self._async_save_storage()

    def _apply_readings(self, readings: list[dict]) -> bool:
        """
        Suma al contador acumulado los días de `readings` que todavía no
        se hubieran contado (deduplicado por fecha, así que da igual si
        `readings` se solapa con lo ya contado). Devuelve si ha cambiado
        algo, para que el llamante decida si persistir.
        """
        changed = False
        for r in readings:
            key = r["date"].isoformat()
            if key not in self._counted_dates:
                self._cumulative_total += r["litros"]
                self._counted_dates.add(key)
                changed = True
        if changed and len(self._counted_dates) > _MAX_FECHAS_RECORDADAS:
            self._counted_dates = set(sorted(self._counted_dates)[-_MAX_FECHAS_RECORDADAS:])
        return changed

    def _build_data(self, readings: list[dict]) -> dict:
        """
        Monta el estado que consumen los sensores, CONSERVANDO la última
        lectura válida si esta actualización no ha traído ninguna.

        Una actualización puede terminar sin lecturas sin que haya
        fallado nada: EMASESA todavía no ha publicado un día nuevo, o el
        rango consultado se ha quedado sin etiquetas interpretables. Si
        en ese caso se devolviera `latest: None`, los sensores de último
        día y de última fecha de lectura pasarían a "desconocido" en
        cada actualización, perdiendo un dato que sigue siendo cierto
        (el consumo de ese día no deja de existir porque EMASESA no
        publique uno nuevo).

        Esto también vale tras reiniciar Home Assistant: la última lectura
        válida se persiste en disco junto al contador acumulado.

        Que la fecha se quede quieta es además la señal que documenta
        `ultima_fecha_lectura`: permite ver cuánto lleva EMASESA sin
        publicar. Para saber si la integración sigue consultando bien
        está el sensor de diagnóstico `ultima_actualizacion`.
        """
        if not readings:
            # Primero lo que ya hay en memoria; si no hay nada (p. ej. el
            # primer refresco tras reiniciar Home Assistant), lo que se
            # guardó en disco la última vez que EMASESA devolvió algo.
            readings = (self.data or {}).get("readings") or self._ultimas_lecturas
        return {
            "readings": readings,
            "latest": readings[-1] if readings else None,
            "cumulative_total": round(self._cumulative_total, 1),
        }

    def _baseline_antes_de(self, readings: list[dict]) -> float:
        """
        Acumulado real justo ANTES del primer día de `readings`, usando
        el propio contador (que no tiene huecos: se basa en fechas
        contadas una a una, nunca se salta ninguna) como ancla en vez de
        fiarse de lo que ya hubiera en la estadística externa horaria —
        así la carga masiva puede arreglar huecos de esta última sin
        heredar sus posibles inconsistencias.
        """
        return self._cumulative_total - sum(r["litros"] for r in readings)

    async def _async_update_data(self) -> dict:
        if not self._storage_loaded:
            await self.async_load_storage()

        # Si todavía no se ha contado ningún día, esta es la primera vez
        # que la integración consigue datos: se trae de golpe la carga
        # masiva por defecto en vez de la ventana estrecha habitual, para
        # no arrancar con el Panel de Energía (ni el histórico reciente)
        # vacíos.
        es_primera_carga = not self._counted_dates
        dias_ventana = CARGA_MASIVA_DIAS_DEFECTO if es_primera_carga else LECTURA_DIARIA_VENTANA_DIAS
        hasta = date.today()
        desde = hasta - timedelta(days=dias_ventana - 1)

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

        if not readings:
            _LOGGER.warning(
                "EMASESA no ha devuelto ninguna lectura para %s..%s; los sensores "
                "conservan su último valor válido",
                desde,
                hasta,
            )

        await self._async_registrar_lecturas(readings)

        # Estadística externa horaria (Panel de Energía). La carga
        # masiva (primera vez, o manual desde las opciones) recalcula y
        # sobrescribe todo el tramo consultado partiendo del acumulado
        # justo anterior a la ventana, lo que de paso arregla cualquier
        # hueco de una carga anterior fallida; la actualización normal,
        # en cambio, solo añade lo nuevo desde el último punto ya
        # importado, para no multiplicar peticiones a EMASESA en cada
        # actualización. Un fallo aquí NO debe tirar abajo el resto de
        # la actualización (los sensores son más importantes que las
        # estadísticas): solo se propaga si EMASESA vuelve a exigir el
        # SMS, igual que en la consulta diaria.
        franjas_importadas: int | None = None
        try:
            if es_primera_carga:
                franjas_importadas = await self._hourly_stats.async_reimport_window(
                    readings, self._baseline_antes_de(readings)
                )
            else:
                franjas_importadas = await self._hourly_stats.async_import_hourly_statistics(
                    [r["date"] for r in readings]
                )
        except EmasesaTwoFactorRequired as err:
            _LOGGER.warning(
                "EMASESA ha revocado la confianza en este dispositivo (al "
                "importar la estadística horaria); hace falta reautenticar "
                "la integración"
            )
            self._entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "No se ha podido importar la estadística externa horaria de EMASESA"
            )

        self.last_fetch_success = dt_util.utcnow()
        datos = self._build_data(readings)

        # Resumen de la actualización entera, para poder diagnosticarla de
        # un vistazo. El contador y la estadística horaria son dos cosas
        # distintas y pueden divergir (el contador no tiene huecos; la
        # estadística sí, si alguna importación falló), así que se
        # registran juntos a propósito.
        ultimo = datos.get("latest")
        _LOGGER.debug(
            "Actualización terminada (%s): ventana %s..%s, %d lectura(s); último día "
            "con dato %s; contador acumulado %.1f L sobre %d día(s) contado(s); "
            "estadística horaria: %s",
            "carga masiva inicial" if es_primera_carga else "lectura periódica",
            desde,
            hasta,
            len(readings),
            f"{ultimo['date'].strftime('%d/%m/%Y')} = {ultimo['litros']:.0f} L"
            if ultimo
            else "ninguno",
            self._cumulative_total,
            len(self._counted_dates),
            "no se ha podido importar"
            if franjas_importadas is None
            else f"{franjas_importadas} franja(s) escrita(s)",
        )
        return datos

    async def async_trigger_carga_masiva(self, dias: int) -> dict:
        """
        Lanza una carga masiva manual (desde las opciones de la
        integración) para los últimos `dias` días. A diferencia de la
        actualización periódica, SIEMPRE recalcula y sobrescribe la
        estadística horaria de toda la ventana pedida (ver
        `EmasesaHourlyStatisticsImporter.async_reimport_window`) —
        pensada para solucionar huecos de una carga anterior que haya
        fallado a medias.

        Deja que `EmasesaAuthError`/`EmasesaApiError` se propaguen tal
        cual (no como `UpdateFailed`), para que quien la invoque (el
        options flow) las traduzca directamente a un error de formulario,
        igual que hace `config_flow.py` con el login.
        """
        dias = max(CARGA_MASIVA_MIN_DIAS, min(dias, CARGA_MASIVA_MAX_DIAS))
        if not self._storage_loaded:
            await self.async_load_storage()

        hasta = date.today()
        desde = hasta - timedelta(days=dias - 1)
        _LOGGER.debug(
            "Carga masiva manual: se piden %d día(s) hacia atrás (%s..%s). Los días "
            "que EMASESA aún no haya publicado no se pueden traer, así que la "
            "ventana real puede ser más corta.",
            dias,
            desde,
            hasta,
        )

        try:
            readings = await self.api.async_get_daily_readings(desde, hasta)
        except EmasesaTwoFactorRequired as err:
            _LOGGER.warning(
                "EMASESA ha revocado la confianza en este dispositivo (al "
                "lanzar la carga masiva manual); hace falta reautenticar "
                "la integración"
            )
            self._entry.async_start_reauth(self.hass)
            raise

        await self._async_registrar_lecturas(readings)

        try:
            escritas = await self._hourly_stats.async_reimport_window(
                readings, self._baseline_antes_de(readings)
            )
        except EmasesaTwoFactorRequired as err:
            self._entry.async_start_reauth(self.hass)
            raise

        self.last_fetch_success = dt_util.utcnow()
        data = self._build_data(readings)
        _LOGGER.debug(
            "Carga masiva manual terminada: %d día(s) traído(s) de los %d pedidos, "
            "%d franja(s) reescrita(s); contador acumulado %.1f L sobre %d día(s) "
            "contado(s)",
            len(readings),
            dias,
            escritas,
            self._cumulative_total,
            len(self._counted_dates),
        )
        self.async_set_updated_data(data)
        return data
