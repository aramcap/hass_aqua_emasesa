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
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace

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
    Monta el importador con el recorder simulado.

    Devuelve un objeto con el importador (`imp`), la api falsa (`api`) y
    la lista de llamadas al recorder (`llamadas`), cada una con sus
    metadatos y sus puntos. `filas` fija lo que contesta
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
        llamadas: list[tuple[dict, list[dict]]] = []
        monkeypatch.setattr(
            statistics,
            "async_add_external_statistics",
            lambda hass, metadata, puntos: llamadas.append(
                (dict(metadata), list(puntos))
            ),
        )
        return SimpleNamespace(imp=imp, api=api, llamadas=llamadas)

    return _construir


def _escritas(ctx: SimpleNamespace) -> list[dict]:
    """Todas las franjas escritas, juntando las llamadas que hubiera."""
    return [punto for _, puntos in ctx.llamadas for punto in puntos]


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
    ctx = importador(filas=None)

    acumulado, ultimo = asyncio.run(ctx.imp._async_last_checkpoint())

    assert acumulado == 0.0
    assert ultimo is None


def test_checkpoint_devuelve_datetime_aunque_el_recorder_de_un_float(importador) -> None:
    """La regresión: con un float, `.astimezone()` reventaría."""
    ctx = importador(filas=[{"start": 1789041600.0, "sum": 1234.5}])

    acumulado, ultimo = asyncio.run(ctx.imp._async_last_checkpoint())

    assert acumulado == 1234.5
    assert isinstance(ultimo, datetime)
    assert ultimo.tzinfo is not None
    assert ultimo.astimezone(ZONA).date() == date(2026, 9, 10)


def test_checkpoint_con_suma_nula(importador) -> None:
    """Una fila sin `sum` no debe romper el acumulado."""
    ctx = importador(filas=[{"start": 1789041600.0, "sum": None}])

    acumulado, _ = asyncio.run(ctx.imp._async_last_checkpoint())

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
    ctx = importador(
        filas=[{"start": 1789041600.0, "sum": 1000.0}],  # 2026-09-10 14:00 en Sevilla
        franjas=_franjas({13: 5.0, 14: 7.0, 15: 11.0, 16: 13.0}),
    )

    anadidas = asyncio.run(ctx.imp.async_import_hourly_statistics([date(2026, 9, 10)]))

    assert anadidas == 2
    assert [p["state"] for p in _escritas(ctx)] == [11.0, 13.0]
    # El acumulado continúa desde 1000, no desde cero.
    assert [p["sum"] for p in _escritas(ctx)] == [1011.0, 1024.0]
    assert [p["start"].astimezone(ZONA).hour for p in _escritas(ctx)] == [15, 16]


def test_importacion_incremental_sin_nada_nuevo_no_escribe(importador) -> None:
    ctx = importador(
        filas=[{"start": 1789041600.0, "sum": 1000.0}],
        franjas=_franjas({13: 5.0, 14: 7.0}),
    )

    anadidas = asyncio.run(ctx.imp.async_import_hourly_statistics([date(2026, 9, 10)]))

    assert anadidas == 0
    assert _escritas(ctx) == []


def test_importacion_incremental_no_pide_dias_ya_cubiertos(importador) -> None:
    """Los días anteriores al checkpoint no se vuelven a consultar."""
    ctx = importador(
        filas=[{"start": 1789041600.0, "sum": 1000.0}],
        franjas=_franjas({23: 3.0}),
    )

    asyncio.run(
        ctx.imp.async_import_hourly_statistics(
            [date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)]
        )
    )

    assert ctx.api.dias_pedidos == [date(2026, 9, 10), date(2026, 9, 11)]


def test_primera_importacion_sin_checkpoint_entra_entera(importador) -> None:
    ctx = importador(filas=None, franjas=_franjas({0: 2.0, 1: 3.0}))

    anadidas = asyncio.run(ctx.imp.async_import_hourly_statistics([date(2026, 9, 10)]))

    assert anadidas == 2
    assert [p["sum"] for p in _escritas(ctx)] == [2.0, 5.0]


# ---------------------------------------------------------------------
# Refresco de los metadatos
#
# `async_add_external_statistics` no solo escribe puntos: también
# reescribe la fila de metadatos de la serie. Si únicamente se llamara
# cuando hay datos nuevos, una instalación al día no volvería a
# escribirla nunca y los metadatos se quedarían congelados con la forma
# que tuvieran el día que se creó la serie. Es lo que pasó al añadir
# `unit_class`: la carga masiva lo arreglaba de rebote (reescribe el
# tramo entero, así que siempre llama) y la actualización periódica no
# podía. Qué campos concretos se envían se prueba en
# `test_statistics_metadata.py`.
# ---------------------------------------------------------------------


def test_sin_franjas_nuevas_igualmente_se_refrescan_los_metadatos(importador) -> None:
    """La regresión: antes esto no llamaba al recorder en absoluto."""
    ctx = importador(
        filas=[{"start": 1789041600.0, "sum": 1000.0}],
        franjas=_franjas({13: 5.0, 14: 7.0}),  # todas anteriores al checkpoint
    )

    anadidas = asyncio.run(ctx.imp.async_import_hourly_statistics([date(2026, 9, 10)]))

    assert anadidas == 0
    assert len(ctx.llamadas) == 1
    metadatos, puntos = ctx.llamadas[0]
    assert puntos == []
    assert metadatos["statistic_id"] == ctx.imp.statistic_id


def test_con_franjas_nuevas_se_escriben_junto_a_los_metadatos(importador) -> None:
    """El caso normal no debe convertirse en dos llamadas distintas."""
    ctx = importador(
        filas=[{"start": 1789041600.0, "sum": 1000.0}],
        franjas=_franjas({15: 11.0, 16: 13.0}),
    )

    asyncio.run(ctx.imp.async_import_hourly_statistics([date(2026, 9, 10)]))

    assert len(ctx.llamadas) == 1
    metadatos, puntos = ctx.llamadas[0]
    assert len(puntos) == 2
    assert metadatos["statistic_id"] == ctx.imp.statistic_id


def test_sin_dias_que_consultar_no_se_llama_al_recorder(importador) -> None:
    """
    Sin ventana que mirar no hay nada que hacer, ni siquiera tocar los
    metadatos: la siguiente actualización con días ya se encarga.
    """
    ctx = importador(filas=None)

    assert asyncio.run(ctx.imp.async_import_hourly_statistics([])) == 0
    assert ctx.llamadas == []


def test_la_carga_masiva_refresca_los_metadatos_aunque_no_traiga_nada(
    importador,
) -> None:
    """Mismo criterio en el otro camino, para que no vuelvan a divergir."""
    ctx = importador(filas=None, franjas=[])

    escritas = asyncio.run(
        ctx.imp.async_reimport_window([{"date": date(2026, 9, 10), "litros": 0.0}], 0.0)
    )

    assert escritas == 0
    assert len(ctx.llamadas) == 1
    assert ctx.llamadas[0][1] == []


# ---------------------------------------------------------------------
# Registro de depuración
#
# El registro es la única ventana a lo que pasa en una instalación real:
# si se degrada, el diagnóstico vuelve a ser adivinar. Por eso se fija
# aquí lo que no puede faltar en él.
# ---------------------------------------------------------------------


def test_resumen_de_puntos_describe_tramo_y_sumas() -> None:
    puntos = [
        {"start": datetime(2026, 9, 16, 0, 0, tzinfo=ZONA), "state": 2.0, "sum": 100.0},
        {"start": datetime(2026, 9, 16, 23, 0, tzinfo=ZONA), "state": 3.0, "sum": 180.0},
    ]

    resumen = statistics._resumen_puntos(puntos)

    assert "2 punto(s)" in resumen
    assert "16/09 00:00" in resumen and "16/09 23:00" in resumen
    # El incremento de la suma es lo que dibuja el Panel de Energía.
    assert "100.0 -> 180.0 L" in resumen


def test_resumen_de_puntos_vacio() -> None:
    assert statistics._resumen_puntos([]) == "ningún punto"


def test_el_registro_dice_cual_es_el_ultimo_punto_importado(
    importador, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = importador(filas=[{"start": 1789041600.0, "sum": 1234.5}])

    with caplog.at_level(logging.DEBUG):
        asyncio.run(ctx.imp._async_last_checkpoint())

    assert "10/09/2026 14:00" in caplog.text
    assert "1234.5 L" in caplog.text


def test_el_registro_dice_cuantas_franjas_eran_nuevas_y_cuantas_ya_estaban(
    importador, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Es la línea que distingue 'EMASESA no ha publicado nada' de 'algo va
    mal': sin ella hay que adivinar por qué no se escribió nada.
    """
    ctx = importador(
        filas=[{"start": 1789041600.0, "sum": 1000.0}],
        franjas=_franjas({13: 5.0, 14: 7.0, 15: 11.0}),
    )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(ctx.imp.async_import_hourly_statistics([date(2026, 9, 10)]))

    assert "1 nueva(s)" in caplog.text
    assert "2 ya cubierta(s)" in caplog.text


def test_el_registro_explica_por_que_no_se_escribe_nada(
    importador, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = importador(
        filas=[{"start": 1789041600.0, "sum": 1000.0}],
        franjas=_franjas({13: 5.0}),
    )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(ctx.imp.async_import_hourly_statistics([date(2026, 9, 10)]))

    # Que diga que no hay nada nuevo Y que recuerde el límite del método.
    assert "Sin franjas nuevas" in caplog.text
    assert "carga masiva" in caplog.text


def test_el_registro_de_la_carga_masiva_dice_el_tramo_y_el_baseline(
    importador, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = importador(filas=None, franjas=_franjas({0: 10.0, 1: 20.0}))

    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            ctx.imp.async_reimport_window(
                [{"date": date(2026, 9, 10), "litros": 30.0}], 500.0
            )
        )

    assert "Carga masiva" in caplog.text
    assert "510.0 -> 530.0 L" in caplog.text
    assert "500.0 L" in caplog.text
