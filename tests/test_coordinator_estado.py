"""
Tests del estado que el coordinator entrega a los sensores.

`_build_data` es lógica propia sobre diccionarios (no depende del
comportamiento de Home Assistant), así que se prueba llamándola con un
objeto mínimo en lugar de construir el coordinator entero.

Lo que cubre: que una actualización sin lecturas NO borre el último dato
válido. Si devolviera `latest: None`, los sensores de último día y de
última fecha de lectura pasarían a "desconocido" en cada actualización
que EMASESA no traiga un día nuevo.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from emasesa.coordinator import EmasesaCoordinator


def _construir(datos_previos: dict | None, total: float = 0.0) -> SimpleNamespace:
    """Lo mínimo que `_build_data` mira de `self`."""
    return SimpleNamespace(data=datos_previos, _cumulative_total=total)


def _build(coordinator: SimpleNamespace, readings: list[dict]) -> dict:
    return EmasesaCoordinator._build_data(coordinator, readings)


LECTURAS = [
    {"date": date(2026, 9, 14), "litros": 120.0},
    {"date": date(2026, 9, 15), "litros": 143.5},
]


def test_con_lecturas_nuevas_el_ultimo_dia_es_el_mas_reciente() -> None:
    datos = _build(_construir(None, total=1000.0), LECTURAS)

    assert datos["latest"] == LECTURAS[-1]
    assert datos["readings"] == LECTURAS
    assert datos["cumulative_total"] == 1000.0


def test_sin_lecturas_se_conserva_la_ultima_valida() -> None:
    """La regresión: antes esto dejaba los sensores en 'desconocido'."""
    previo = {
        "readings": LECTURAS,
        "latest": LECTURAS[-1],
        "cumulative_total": 1000.0,
    }

    datos = _build(_construir(previo, total=1000.0), [])

    assert datos["latest"] == LECTURAS[-1]
    assert datos["readings"] == LECTURAS


def test_sin_lecturas_y_sin_estado_previo_queda_vacio() -> None:
    """En el primer arranque sí es legítimo no tener nada que mostrar."""
    datos = _build(_construir(None), [])

    assert datos["latest"] is None
    assert datos["readings"] == []


def test_sin_lecturas_el_acumulado_sigue_siendo_el_actual() -> None:
    """El contador lo lleva la propia integración: no depende de la consulta."""
    previo = {"readings": LECTURAS, "latest": LECTURAS[-1], "cumulative_total": 1000.0}

    datos = _build(_construir(previo, total=1234.56), [])

    assert datos["cumulative_total"] == 1234.6


def test_una_lectura_nueva_sustituye_a_la_conservada() -> None:
    """Conservar no puede significar quedarse pegado al dato viejo."""
    previo = {"readings": LECTURAS, "latest": LECTURAS[-1], "cumulative_total": 1000.0}
    nueva = [{"date": date(2026, 9, 16), "litros": 98.0}]

    datos = _build(_construir(previo, total=1098.0), nueva)

    assert datos["latest"] == nueva[0]
    assert datos["readings"] == nueva


@pytest.mark.parametrize("previo", [None, {}, {"readings": None, "latest": None}])
def test_estado_previo_degenerado_no_rompe(previo: dict | None) -> None:
    datos = _build(_construir(previo), [])

    assert datos["latest"] is None
    assert datos["readings"] == []
