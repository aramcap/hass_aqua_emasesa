"""
Importa el consumo de EMASESA como una estadística EXTERNA del
recorder, con granularidad horaria, independiente de cualquier entidad.

------------------------------------------------------------------------
Por qué una estadística externa y no "corregir" el sensor existente
------------------------------------------------------------------------
El sensor `consumo_acumulado` (ver `sensor.py`) es un `total_increasing`
normal. Las estadísticas horarias que Home Assistant genera para un
sensor así las construye el recorder a partir de los cambios de ESTADO
que registra en tiempo real — no de ninguna fecha "interna" que traiga
el propio dato. Si además intentáramos corregir a posteriori las
estadísticas de ESE MISMO sensor con el histórico horario que da
EMASESA, entrarían en conflicto con lo que el recorder ya generó a
partir de sus propios cambios de estado: se acabaría contando el mismo
consumo dos veces (una vez por el cambio de estado real del sensor,
y otra por la corrección histórica).

La solución robusta — la misma que usa la integración `opower`, incluida
en Home Assistant, para lecturas diarias con retraso de compañías de
luz/gas (ver `homeassistant/components/opower/coordinator.py`,
`_insert_statistics`, en el propio código fuente de Home Assistant) es
una estadística "externa": vive en su propia serie, identificada por un
`statistic_id` con formato `"<domain>:<algo>"` que NO corresponde a
ninguna entidad, y que solo escribe esta integración. No hay ninguna
otra fuente compitiendo por esos mismos puntos, así que no hace falta
preocuparse por duplicar consumo.

El sensor `consumo_acumulado` se mantiene tal cual (útil para
automatizaciones, tarjetas de histórico, plantillas...), pero deja de
ser la fuente recomendada para el Panel de Energía: en su lugar se
selecciona esta estadística externa (aparece en el selector de fuentes
de Energía con el nombre configurado más abajo, no como un sensor).

------------------------------------------------------------------------
Cómo se importa
------------------------------------------------------------------------
Hay dos formas de importar, para dos situaciones distintas:

- `async_import_hourly_statistics()`: la actualización periódica normal
  (cada `scan_interval_minutes`). Solo AÑADE lo nuevo: consulta primero
  el último punto ya importado (`get_last_statistics`) y solo pide a
  EMASESA los días a partir de ahí — en régimen normal, como mucho un
  día nuevo por actualización, para no multiplicar peticiones al portal.
- `async_reimport_window()`: la carga masiva (automática la primera vez
  que se da de alta la integración, o manual desde las opciones). Vuelve
  a pedir y a SOBRESCRIBIR el tramo completo pedido, sin mirar ningún
  checkpoint — más peticiones, pero arregla cualquier hueco de una carga
  anterior que hubiera fallado a medias, cosa que la importación
  incremental no puede hacer sola (un día que fallase quedaría saltado
  para siempre, porque el checkpoint avanza igualmente).

Las franjas horarias que da EMASESA son horas locales de Sevilla
(España peninsular), así que se localizan explícitamente con
`ZONA_EMASESA` (`ZoneInfo("Europe/Madrid")`, ver `const.py`) en vez de
depender de la zona horaria que tenga configurada esta instancia de
Home Assistant.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .api import EmasesaApiClient, EmasesaApiError, EmasesaTwoFactorRequired
from .const import DOMAIN, ZONA_EMASESA

try:  # Home Assistant >= 2025.5
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # Home Assistant < 2025.5
    StatisticMeanType = None  # type: ignore[assignment]

# `unit_class` (la clase de conversión de unidades a la que pertenece la
# magnitud: litros -> volumen) es obligatoria en `StatisticMetaData`
# desde Home Assistant 2025.11, y no declararla deja de funcionar en
# 2026.11. No se puede pasar siempre: el recorder monta la fila con
# `StatisticsMeta(**metadata)`, así que en una versión cuyo modelo no
# tiene esa columna la clave de más provocaría un `TypeError` al
# escribir. Se mira si la propia TypedDict la declara, que es
# exactamente la condición que importa.
_SOPORTA_UNIT_CLASS = "unit_class" in getattr(StatisticMetaData, "__annotations__", {})

# Misma clase de unidad que usa `opower` (incluida en Home Assistant)
# para el agua y el gas; el literal es la reserva por si la constante
# aún no existiera en la versión instalada.
_UNIT_CLASS_VOLUMEN = getattr(VolumeConverter, "UNIT_CLASS", "volume")

_LOGGER = logging.getLogger(__name__)


def _slug(texto: str) -> str:
    """
    Sanea un texto para usarlo como mitad de un `statistic_id`: el
    recorder exige `^[\\da-z_]+$` a cada lado de los dos puntos (sin
    guion bajo al principio/final ni dobles). `entry.entry_id` ya viene
    así (hexadecimal en minúsculas), pero se sanea de todas formas por
    si acaso.
    """
    limpio = "".join(c if c.isalnum() else "_" for c in texto.lower())
    while "__" in limpio:
        limpio = limpio.replace("__", "_")
    return limpio.strip("_") or "cuenta"


def _inicio_a_datetime(start: float | datetime) -> datetime:
    """
    Normaliza el `start` de una fila de `get_last_statistics`.

    El recorder devuelve el inicio de la franja como marca de tiempo
    UNIX (un `float`), no como `datetime` — es lo que asume también
    `opower` (incluida en Home Assistant), que hace
    `dt_util.utc_from_timestamp(...)` con ese mismo valor. Se acepta un
    `datetime` tal cual por si alguna versión lo devolviera ya
    convertido.
    """
    if isinstance(start, datetime):
        return start
    return dt_util.utc_from_timestamp(start)


class EmasesaHourlyStatisticsImporter:
    """
    Importa el desglose horario de EMASESA como estadística externa del
    recorder (`statistic_id` = `f"{DOMAIN}:{entry_id}_consumo_horario"`),
    reanudando siempre desde el último punto ya importado.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, entry_title: str, api: EmasesaApiClient) -> None:
        self.hass = hass
        self.api = api
        self.statistic_id = f"{DOMAIN}:{_slug(entry_id)}_consumo_horario"
        # Los metadatos se montan campo a campo porque `StatisticMetaData`
        # ha ido cambiando: `has_mean` quedó obsoleto en Home Assistant
        # 2025.5 a favor de `mean_type`, y `unit_class` pasó a ser
        # obligatorio en 2025.11 (las dos cosas dejan de funcionar en
        # 2026.11). Se envía en cada caso lo que esa versión entiende,
        # para seguir arrancando en instalaciones anteriores — el mínimo
        # que declara `hacs.json` es 2024.1.
        metadata: dict = {
            "has_sum": True,
            "name": f"{entry_title} consumo por hora",
            "source": DOMAIN,
            "statistic_id": self.statistic_id,
            "unit_of_measurement": UnitOfVolume.LITERS,
        }
        if StatisticMeanType is not None:
            # El consumo por hora se acumula (suma), no se promedia.
            metadata["mean_type"] = StatisticMeanType.NONE
        else:
            metadata["has_mean"] = False
        if _SOPORTA_UNIT_CLASS:
            metadata["unit_class"] = _UNIT_CLASS_VOLUMEN
        self._metadata = StatisticMetaData(**metadata)

    async def _async_last_checkpoint(self) -> tuple[float, datetime | None]:
        """Devuelve (suma_acumulada, fecha_del_último_punto_importado)."""
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, self.statistic_id, False, {"sum"}
        )
        rows = last.get(self.statistic_id)
        if not rows:
            return 0.0, None
        row = rows[0]
        return float(row.get("sum") or 0.0), _inicio_a_datetime(row["start"])

    async def async_import_hourly_statistics(self, dias: list[date]) -> int:
        """
        Importa el desglose horario de los días de `dias` que aún no
        estén cubiertos por el último punto importado. Devuelve cuántas
        franjas horarias nuevas se han añadido (0 si no había nada
        nuevo). Puede lanzar `EmasesaTwoFactorRequired` si EMASESA
        revoca la confianza en el dispositivo a mitad de la importación
        (el coordinator lo convierte en un reauth, igual que con la
        consulta diaria).
        """
        if not dias:
            return 0

        acumulado, ultimo_punto = await self._async_last_checkpoint()
        ultima_fecha = ultimo_punto.astimezone(ZONA_EMASESA).date() if ultimo_punto else None

        # Se vuelve a pedir el día del último punto por si se quedó a
        # mitad (el filtro por franja de más abajo evita reimportar lo
        # que ya estaba); los días anteriores ya están cubiertos.
        dias_a_pedir = sorted(
            {d for d in dias if ultima_fecha is None or d >= ultima_fecha}
        )

        nuevas: list[StatisticData] = []
        for dia in dias_a_pedir:
            try:
                franjas = await self.api.async_get_hourly_readings(dia)
            except EmasesaTwoFactorRequired:
                raise
            except EmasesaApiError as err:
                _LOGGER.warning(
                    "No se ha podido importar el desglose horario de %s: %s", dia, err
                )
                continue

            for franja in franjas:
                inicio = datetime(
                    dia.year, dia.month, dia.day, franja["hora_inicio"], tzinfo=ZONA_EMASESA
                )
                if ultimo_punto is not None and inicio <= ultimo_punto:
                    continue
                acumulado += franja["litros"]
                nuevas.append(StatisticData(start=inicio, state=franja["litros"], sum=acumulado))

        # Se llama SIEMPRE, aunque no haya franjas nuevas: además de
        # escribir los puntos, `async_add_external_statistics` reescribe
        # la fila de metadatos de la serie. Si solo se llamara cuando hay
        # datos nuevos, una instalación que ya esté al día no volvería a
        # escribir nunca, y los metadatos se quedarían congelados con la
        # forma que tuvieran el día que se creó la serie — que es
        # exactamente lo que pasó al añadir `unit_class` (obligatorio
        # desde Home Assistant 2025.11): la carga masiva los arreglaba de
        # rebote y la actualización periódica no podía.
        async_add_external_statistics(self.hass, self._metadata, nuevas)
        if nuevas:
            _LOGGER.debug(
                "Importadas %d franjas horarias nuevas en la estadística externa %s",
                len(nuevas),
                self.statistic_id,
            )
        else:
            _LOGGER.debug(
                "Sin franjas nuevas que importar; metadatos de %s refrescados",
                self.statistic_id,
            )
        return len(nuevas)

    async def async_reimport_window(self, readings: list[dict], baseline: float) -> int:
        """
        Recalcula y SOBRESCRIBE (upsert) la estadística horaria para
        exactamente los días de `readings` (cada uno con su fecha y su
        litros ya consultados por `async_get_daily_readings`), partiendo
        de `baseline` como acumulado justo ANTES del primer día de la
        ventana.

        A diferencia de `async_import_hourly_statistics` (que solo AÑADE
        lo nuevo tras el último punto ya importado, para no multiplicar
        peticiones en cada actualización periódica), este método siempre
        vuelve a pedir y a escribir el tramo completo, sin mirar ningún
        checkpoint — pensado para la carga masiva (automática la primera
        vez, o manual desde las opciones): como la ventana SIEMPRE
        termina hoy, nunca hay ya un punto más reciente en el recorder
        con el que esto pueda entrar en conflicto, así que sobrescribir
        sin más es seguro, y de paso arregla cualquier hueco de una carga
        anterior que hubiera fallado a medias (un día que diera error se
        vuelve a intentar, en vez de quedar saltado para siempre).

        `baseline` debe ser el acumulado real justo antes del primer día
        de `readings` (típicamente `total_acumulado_del_contador -
        sum(litros de readings)`, calculado por el llamante a partir del
        propio contador de `coordinator.py`, que no tiene huecos).

        Devuelve cuántas franjas horarias se han escrito. Puede lanzar
        `EmasesaTwoFactorRequired` igual que el resto de la integración.
        """
        acumulado = baseline
        nuevas: list[StatisticData] = []
        dias_fallidos = 0
        for r in sorted(readings, key=lambda x: x["date"]):
            dia = r["date"]
            try:
                franjas = await self.api.async_get_hourly_readings(dia)
            except EmasesaTwoFactorRequired:
                raise
            except EmasesaApiError as err:
                dias_fallidos += 1
                _LOGGER.warning(
                    "No se ha podido recargar el desglose horario de %s: %s", dia, err
                )
                continue

            for franja in franjas:
                inicio = datetime(
                    dia.year, dia.month, dia.day, franja["hora_inicio"], tzinfo=ZONA_EMASESA
                )
                acumulado += franja["litros"]
                nuevas.append(StatisticData(start=inicio, state=franja["litros"], sum=acumulado))

        # Igual que en la importación incremental: la llamada va siempre,
        # para que los metadatos queden al día aunque el tramo pedido no
        # haya devuelto ninguna franja.
        async_add_external_statistics(self.hass, self._metadata, nuevas)
        _LOGGER.debug(
            "Carga masiva: reescritas %d franjas horarias (%d día(s) pedido(s), "
            "%d fallido(s)) en la estadística externa %s",
            len(nuevas),
            len(readings),
            dias_fallidos,
            self.statistic_id,
        )
        return len(nuevas)
