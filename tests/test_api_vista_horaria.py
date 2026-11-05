"""
Tests de la consulta diaria cuando EMASESA responde con la vista horaria.

Síntoma real: consultas de uno a tres días devolvían 24 etiquetas tipo
"13-14" en lugar de fechas, la consulta diaria las descartaba todas y la
actualización terminaba sin lecturas ("no ha devuelto ninguna lectura
diaria interpretable").

No se sabe con certeza qué hace el portal para decidirlo; lo que se prueba
aquí es la reacción de la integración, que no depende de la causa: ante
una respuesta horaria a una consulta diaria, reintentar con una ventana
más ancha hacia atrás, de forma acotada.

El cliente se prueba sin red: se sustituyen las tres funciones que tocan
HTTP y se simula un portal que contesta en horario cuando el rango pedido
es estrecho.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

import pytest

from conftest import leer_fixture
from emasesa.api import (
    EmasesaApiClient,
    EmasesaApiError,
    EmasesaAuthError,
    _es_vista_horaria,
    _pares_a_lecturas,
)

# La página de la fixture anuncia `maxDate` = 07/09/2026.
HASTA = date(2026, 9, 7)
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _respuesta_diaria(desde: date, hasta: date) -> str:
    dias = [desde + timedelta(days=i) for i in range((hasta - desde).days + 1)]
    ticks = ",".join(f'"X {d.day:02d}\\-{MESES[d.month - 1]}"' for d in dias)
    valores = ",".join(str(100 + i) for i in range(len(dias)))
    return f'PrimeFaces.cw("Chart","w",{{id:"c",data:[[{valores}]],ticks:[{ticks}],shadow:false}});'


def _respuesta_horaria() -> str:
    ticks = ",".join(f'"{h:02d}\\-{(h + 1) % 24:02d}"' for h in range(24))
    valores = ",".join("5" for _ in range(24))
    return f'PrimeFaces.cw("Chart","w",{{id:"c",data:[[{valores}]],ticks:[{ticks}],shadow:false}});'


class _PortalFalso:
    """Contesta en horario si el rango pedido tiene menos de `dias_minimos` días."""

    def __init__(self, dias_minimos: int = 0, fallar_en: dict[int, Exception] | None = None) -> None:
        self.dias_minimos = dias_minimos
        self.fallar_en = fallar_en or {}
        self.consultas: list[tuple[date, date]] = []

    async def post(self, desde_str: str, hasta_str: str, viewstate: str) -> str:
        desde = date(int(desde_str[6:]), int(desde_str[3:5]), int(desde_str[:2]))
        hasta = date(int(hasta_str[6:]), int(hasta_str[3:5]), int(hasta_str[:2]))
        self.consultas.append((desde, hasta))
        numero = len(self.consultas)
        if numero in self.fallar_en:
            raise self.fallar_en[numero]
        if (hasta - desde).days + 1 < self.dias_minimos:
            return _respuesta_horaria()
        return _respuesta_diaria(desde, hasta)


@pytest.fixture
def cliente(monkeypatch: pytest.MonkeyPatch):
    def _construir(portal: _PortalFalso) -> EmasesaApiClient:
        c = EmasesaApiClient(None, "usuario", "clave", "dispositivo")

        async def _sin_login() -> None:
            return None

        async def _pagina() -> str:
            return leer_fixture("consumo_pagina.html")

        monkeypatch.setattr(c, "_ensure_login", _sin_login)
        monkeypatch.setattr(c, "_get_consumo_page", _pagina)
        monkeypatch.setattr(c, "_post_consulta", portal.post)
        return c

    return _construir


# ---------------------------------------------------------------------
# Helpers puros
# ---------------------------------------------------------------------


def test_es_vista_horaria_con_las_24_franjas() -> None:
    pares = [(f"{h:02d}-{(h + 1) % 24:02d}", 5.0) for h in range(24)]

    assert _es_vista_horaria(pares) is True


@pytest.mark.parametrize(
    "pares",
    [
        [],
        [("M 01-sep", 1.0), ("X 02-sep", 2.0)],
        [("13-14", 1.0), ("M 01-sep", 2.0)],  # mezcla: no es una vista horaria limpia
    ],
    ids=["vacia", "diaria", "mezclada"],
)
def test_no_es_vista_horaria(pares: list) -> None:
    assert _es_vista_horaria(pares) is False


def test_pares_a_lecturas_descarta_lo_horario_y_ordena() -> None:
    pares = [("X 02-sep", 2.0), ("13-14", 9.0), ("L 01-sep", 1.0)]

    lecturas = _pares_a_lecturas(pares, date(2026, 9, 7))

    assert lecturas == [
        {"date": date(2026, 9, 1), "litros": 1.0},
        {"date": date(2026, 9, 2), "litros": 2.0},
    ]


# ---------------------------------------------------------------------
# Comportamiento del cliente
# ---------------------------------------------------------------------


def test_consulta_normal_no_hace_peticiones_de_mas(cliente) -> None:
    """Si la vista diaria llega bien, UNA petición: el reintento es solo para el fallo."""
    portal = _PortalFalso(dias_minimos=0)
    c = cliente(portal)

    lecturas = asyncio.run(c.async_get_daily_readings(HASTA - timedelta(days=1), HASTA))

    assert len(lecturas) == 2
    assert len(portal.consultas) == 1


def test_respuesta_horaria_se_reintenta_con_una_ventana_de_una_semana(cliente) -> None:
    """La regresión: antes esto terminaba con cero lecturas."""
    portal = _PortalFalso(dias_minimos=3)  # el rango de 2 días sale en horario
    c = cliente(portal)

    lecturas = asyncio.run(c.async_get_daily_readings(HASTA - timedelta(days=1), HASTA))

    assert len(lecturas) == 7
    assert lecturas[-1]["date"] == HASTA
    assert len(portal.consultas) == 2
    assert portal.consultas[1] == (HASTA - timedelta(days=6), HASTA)


def test_si_la_semana_no_basta_se_prueba_con_un_mes(cliente) -> None:
    portal = _PortalFalso(dias_minimos=10)
    c = cliente(portal)

    lecturas = asyncio.run(c.async_get_daily_readings(HASTA - timedelta(days=1), HASTA))

    assert len(lecturas) == 30
    assert [(d, h) for d, h in portal.consultas] == [
        (HASTA - timedelta(days=1), HASTA),
        (HASTA - timedelta(days=6), HASTA),
        (HASTA - timedelta(days=29), HASTA),
    ]


def test_los_reintentos_estan_acotados(cliente) -> None:
    """Pase lo que pase: la petición inicial más, como mucho, dos reintentos."""
    portal = _PortalFalso(dias_minimos=10_000)  # siempre horario
    c = cliente(portal)

    lecturas = asyncio.run(c.async_get_daily_readings(HASTA - timedelta(days=1), HASTA))

    assert lecturas == []
    assert len(portal.consultas) == 3


def test_una_consulta_ya_ancha_no_se_reintenta(cliente) -> None:
    """Una carga masiva de 30 días ya es la ventana más ancha: ampliar no aporta nada."""
    portal = _PortalFalso(dias_minimos=10_000)
    c = cliente(portal)

    asyncio.run(c.async_get_daily_readings(HASTA - timedelta(days=29), HASTA))

    assert len(portal.consultas) == 1


def test_el_reintento_nunca_amplia_hacia_delante(cliente) -> None:
    portal = _PortalFalso(dias_minimos=3)
    c = cliente(portal)

    asyncio.run(c.async_get_daily_readings(HASTA - timedelta(days=1), HASTA))

    assert all(hasta <= HASTA for _, hasta in portal.consultas)


def test_un_error_de_autenticacion_en_el_reintento_se_propaga(cliente) -> None:
    """Incluye la verificación por SMS: lo tiene que ver el coordinator para pedir reauth."""
    portal = _PortalFalso(dias_minimos=3, fallar_en={2: EmasesaAuthError("sesión caducada")})
    c = cliente(portal)

    with pytest.raises(EmasesaAuthError):
        asyncio.run(c.async_get_daily_readings(HASTA - timedelta(days=1), HASTA))


def test_un_error_generico_en_el_reintento_no_tumba_la_actualizacion(cliente) -> None:
    """Los sensores siguen vivos: sin lecturas, pero sin excepción."""
    portal = _PortalFalso(dias_minimos=3, fallar_en={2: EmasesaApiError("formato raro")})
    c = cliente(portal)

    lecturas = asyncio.run(c.async_get_daily_readings(HASTA - timedelta(days=1), HASTA))

    assert lecturas == []
    assert len(portal.consultas) == 2  # no sigue insistiendo tras el fallo


def test_el_aviso_dice_que_eran_franjas_horarias_y_muestra_ejemplos(
    cliente, caplog: pytest.LogCaptureFixture
) -> None:
    """Sin esto, el log solo cuenta 'N etiquetas descartadas' y no permite diagnosticar."""
    portal = _PortalFalso(dias_minimos=10_000)
    c = cliente(portal)

    with caplog.at_level(logging.WARNING):
        asyncio.run(c.async_get_daily_readings(HASTA - timedelta(days=1), HASTA))

    assert "todas son franjas horarias" in caplog.text
    assert "'00-01'" in caplog.text
