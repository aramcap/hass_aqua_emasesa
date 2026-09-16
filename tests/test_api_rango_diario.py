"""
Tests del ajuste del rango de una consulta diaria (`api.py`).

Aquí vivía un fallo que dejaba la integración medio muerta sin dar la
cara: la actualización periódica pide una ventana de dos días contando
HOY, pero EMASESA publica con uno o dos días de retraso, así que al
recortar `hasta` al `maxDate` el rango se quedaba en un único día.
Y con `desde == hasta` EMASESA devuelve la vista HORARIA, cuyas
etiquetas descarta `async_get_daily_readings` — de modo que cada
actualización periódica terminaba con cero lecturas.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from emasesa.api import _ajusta_rango_diario

HOY = date(2026, 9, 16)


def _dias(n: int) -> date:
    return HOY - timedelta(days=n)


# ---------------------------------------------------------------------
# La regresión: la ventana de la actualización periódica
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("max_fecha", "motivo"),
    [
        (_dias(1), "EMASESA publicó ayer"),
        (_dias(2), "publicó anteayer"),
        (_dias(5), "lleva cinco días sin publicar"),
    ],
)
def test_la_ventana_periodica_nunca_colapsa_en_un_solo_dia(
    max_fecha: date, motivo: str
) -> None:
    """Con `desde == hasta` EMASESA devuelve la vista horaria: inservible aquí."""
    desde, hasta = _ajusta_rango_diario(_dias(1), HOY, max_fecha)

    assert desde < hasta, motivo
    assert hasta == max_fecha
    assert hasta <= max_fecha


def test_la_ventana_periodica_cubre_el_ultimo_dia_publicado() -> None:
    """El día recién publicado tiene que entrar en el rango consultado."""
    max_fecha = _dias(1)

    desde, hasta = _ajusta_rango_diario(_dias(1), HOY, max_fecha)

    assert desde <= max_fecha <= hasta


# ---------------------------------------------------------------------
# Recorte por maxDate
# ---------------------------------------------------------------------


def test_recorta_hasta_al_maximo_que_permite_emasesa() -> None:
    desde, hasta = _ajusta_rango_diario(_dias(30), HOY, _dias(2))

    assert hasta == _dias(2)
    assert desde == _dias(30)  # el inicio de una carga masiva no se toca


def test_no_toca_un_rango_que_ya_es_valido() -> None:
    desde, hasta = _ajusta_rango_diario(_dias(10), _dias(2), _dias(1))

    assert (desde, hasta) == (_dias(10), _dias(2))


def test_sin_maxdate_solo_se_garantizan_dos_dias() -> None:
    """Si no se reconoce el `maxDate` se sigue adelante sin recortar."""
    assert _ajusta_rango_diario(_dias(10), HOY, None) == (_dias(10), HOY)
    assert _ajusta_rango_diario(HOY, HOY, None) == (_dias(1), HOY)


# ---------------------------------------------------------------------
# Propiedades que deben cumplirse siempre
# ---------------------------------------------------------------------


@pytest.mark.parametrize("dias_desde", [0, 1, 2, 30, 90])
@pytest.mark.parametrize("dias_max", [0, 1, 2, 7, 120, None])
def test_el_resultado_siempre_es_consultable(
    dias_desde: int, dias_max: int | None
) -> None:
    """
    Sea cual sea la ventana pedida y el retraso de EMASESA: el rango
    resultante siempre abarca dos días distintos y nunca pide más allá
    del máximo permitido.
    """
    max_fecha = None if dias_max is None else _dias(dias_max)

    desde, hasta = _ajusta_rango_diario(_dias(dias_desde), HOY, max_fecha)

    assert desde < hasta
    if max_fecha is not None:
        assert hasta <= max_fecha


def test_desde_se_amplia_hacia_atras_no_hacia_delante() -> None:
    """
    Ampliar hacia delante pediría un día que EMASESA aún no publica, y
    el formulario rechazaría la consulta entera.
    """
    desde, hasta = _ajusta_rango_diario(_dias(1), HOY, _dias(1))

    assert desde == _dias(2)
    assert hasta == _dias(1)
