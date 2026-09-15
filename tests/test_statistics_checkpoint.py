"""
Tests del checkpoint de la importación incremental (`statistics.py`).

La actualización periódica solo añade las franjas posteriores al último
punto ya importado, y para saber cuál es consulta `get_last_statistics`.
Ese punto llega como marca de tiempo UNIX (`float`), no como `datetime`:
tratarlo como `datetime` revienta con `AttributeError`, y como el
coordinator envuelve toda la importación en un `except Exception`, el
fallo no se ve — simplemente deja de añadirse consumo nuevo, y las
estadísticas solo crecen con las cargas masivas.

De ahí que aquí se ejercite el camino completo con el recorder simulado,
en vez de probar solo la conversión: es la única forma de que un cambio
así vuelva a delatarse.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

import emasesa.statistics as statistics

ZONA = statistics.ZONA_EMASESA


class _RecorderFalso:
    """Ejecuta el trabajo en el momento, sin hilo de por medio."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _ApiFalsa:
    """Devuelve el mismo desglose horario para cualquier día pedido."""

    def __init__(self, franjas: list[dict]) -> None:
        self.franjas = franjas
        self.dias_pedidos: list[date] = []

    async def async_get_hourly_readings(self, dia: date) -> list[dict]:
        self.dias_pedidos.append(dia)
        return list(self.franjas)


@pytest.fixture
def importador(monkeypatch: pytest.MonkeyPatch):
    """
    Devuelve (importador, escritas, api) con el recorder simulado.

    `escritas` acumula las franjas que se habrían escrito en la
    estadística externa; `filas` fija lo que contesta
    `get_last_statistics`.
    """

    def _construir(filas: list[dict] | None, franjas: list[dict] | None = None):
        api = _ApiFalsa(franjas or [])
        imp = statistics.EmasesaHourlyStatisticsImporter(None, "abc123", "Casa", api)

        monkeypatch.setattr(statistics, "get_instance", lambda hass: _RecorderFalso())
        monkeypatch.setattr(
            statistics,
            "get_last_statistics",
            lambda hass, numero, statistic_id, convertir, tipos: (
                {statistic_id: filas} if filas else {}
            ),
        )
        escritas: list[dict] = []
        monkeypatch.setattr(
            statistics,
            "async_add_external_statistics",
            lambda hass, metadata, puntos: escritas.extend(puntos),
        )
        return imp, escritas, api

    return _construir


# ---------------------------------------------------------------------
# _inicio_a_datetime
# ---------------------------------------------------------------------


def test_inicio_float_se_convierte_a_datetime_con_zona() -> None:
    """Es lo que devuelve el recorder: segundos desde el epoch."""
    # 2026-09-10 12:00:00 UTC = 14:00 en Sevilla (horario de verano).
    resultado = statistics._inicio_a_datetime(1789041600.0)

    assert resultado == datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    assert resultado.astimezone(ZONA).hour == 14


def test_inicio_datetime_se_deja_igual() -> None:
    ya_convertido = datetime(2026, 9, 10, 14, 0, tzinfo=ZONA)

    assert statistics._inicio_a_datetime(ya_convertido) is ya_convertido


# ---------------------------------------------------------------------
# _async_last_checkpoint
# ---------------------------------------------------------------------


def test_checkpoint_sin_datos_previos(importador) -> None:
    imp, _, _ = importador(filas=None)

    acumulado, ultimo = asyncio.run(imp._async_last_checkpoint())

    assert acumulado == 0.0
    assert ultimo is None


def test_checkpoint_devuelve_datetime_aunque_el_recorder_de_un_float(importador) -> None:
    """La regresión: con un float, `.astimezone()` reventaría."""
    imp, _, _ = importador(filas=[{"start": 1789041600.0, "sum": 1234.5}])

    acumulado, ultimo = asyncio.run(imp._async_last_checkpoint())

    assert acumulado == 1234.5
    assert isinstance(ultimo, datetime)
    assert ultimo.tzinfo is not None
    assert ultimo.astimezone(ZONA).date() == date(2026, 9, 10)


def test_checkpoint_con_suma_nula(importador) -> None:
    """Una fila sin `sum` no debe romper el acumulado."""
    imp, _, _ = importador(filas=[{"start": 1789041600.0, "sum": None}])

    acumulado, _ = asyncio.run(imp._async_last_checkpoint())

    assert acumulado == 0.0


# ---------------------------------------------------------------------
# async_import_hourly_statistics
# ---------------------------------------------------------------------


def _franjas(horas: dict[int, float]) -> list[dict]:
    return [{"hora_inicio": h, "litros": l} for h, l in sorted(horas.items())]


def test_importacion_incremental_solo_anade_lo_posterior(importador) -> None:
    """
    Con el último punto en las 14:00 del día 10 (hora de Sevilla), solo
    entran las franjas de las 15:00 en adelante, y el acumulado sigue
    desde la suma ya guardada.
    """
    imp, escritas, _ = importador(
        filas=[{"start": 1789041600.0, "sum": 1000.0}],  # 2026-09-10 14:00 en Sevilla
        franjas=_franjas({13: 5.0, 14: 7.0, 15: 11.0, 16: 13.0}),
    )

    anadidas = asyncio.run(imp.async_import_hourly_statistics([date(2026, 9, 10)]))

    assert anadidas == 2
    assert [p["state"] for p in escritas] == [11.0, 13.0]
    # El acumulado continúa desde 1000, no desde cero.
    assert [p["sum"] for p in escritas] == [1011.0, 1024.0]
    assert [p["start"].astimezone(ZONA).hour for p in escritas] == [15, 16]


def test_importacion_incremental_sin_nada_nuevo_no_escribe(importador) -> None:
    imp, escritas, _ = importador(
        filas=[{"start": 1789041600.0, "sum": 1000.0}],
        franjas=_franjas({13: 5.0, 14: 7.0}),
    )

    anadidas = asyncio.run(imp.async_import_hourly_statistics([date(2026, 9, 10)]))

    assert anadidas == 0
    assert escritas == []


def test_importacion_incremental_no_pide_dias_ya_cubiertos(importador) -> None:
    """Los días anteriores al checkpoint no se vuelven a consultar."""
    imp, _, api = importador(
        filas=[{"start": 1789041600.0, "sum": 1000.0}],
        franjas=_franjas({23: 3.0}),
    )

    asyncio.run(
        imp.async_import_hourly_statistics(
            [date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)]
        )
    )

    assert api.dias_pedidos == [date(2026, 9, 10), date(2026, 9, 11)]


def test_primera_importacion_sin_checkpoint_entra_entera(importador) -> None:
    imp, escritas, _ = importador(filas=None, franjas=_franjas({0: 2.0, 1: 3.0}))

    anadidas = asyncio.run(imp.async_import_hourly_statistics([date(2026, 9, 10)]))

    assert anadidas == 2
    assert [p["sum"] for p in escritas] == [2.0, 5.0]
