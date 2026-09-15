"""
Tests de los metadatos que se envían al recorder (`statistics.py`).

`StatisticMetaData` ha ido cambiando de forma entre versiones de Home
Assistant (`has_mean` → `mean_type` en 2025.5, `unit_class` obligatorio
en 2025.11), y la integración monta el diccionario campo a campo para
seguir arrancando en instalaciones antiguas. Esa lógica de
compatibilidad es la única parte "lista" del módulo, así que aquí se
comprueba en los dos escenarios, recargando el módulo con los stubs de
Home Assistant recortados (ver `conftest.py`).

Importa porque el recorder monta la fila con `StatisticsMeta(**metadata)`:
una clave de más en una versión que no la conoce no es un aviso, es un
`TypeError` al escribir la estadística.
"""

from __future__ import annotations

import importlib
import sys

import pytest

import emasesa.statistics as statistics

MODELS = "homeassistant.components.recorder.models"


@pytest.fixture
def recargar_statistics(monkeypatch: pytest.MonkeyPatch):
    """
    Recarga `emasesa.statistics` simulando una versión de Home Assistant.

    `moderno=True` deja los stubs tal cual (Home Assistant >= 2025.11);
    `moderno=False` les quita `StatisticMeanType` y las anotaciones de
    `StatisticMetaData`, como en una versión anterior a 2025.5.
    """

    def _recargar(*, moderno: bool):
        models = sys.modules[MODELS]
        if not moderno:
            monkeypatch.delattr(models, "StatisticMeanType")
            monkeypatch.setattr(models, "StatisticMetaData", dict)
        return importlib.reload(statistics)

    yield _recargar

    # Deshacer los stubs recortados y dejar el módulo como estaba, para
    # no arrastrar el estado al resto de la suite.
    monkeypatch.undo()
    importlib.reload(statistics)


def _metadatos(modulo, entry_id: str = "abc123", titulo: str = "EMASESA") -> dict:
    importador = modulo.EmasesaHourlyStatisticsImporter(None, entry_id, titulo, None)
    return dict(importador._metadata)


def test_metadatos_en_home_assistant_actual(recargar_statistics) -> None:
    modulo = recargar_statistics(moderno=True)
    meta = _metadatos(modulo)

    # El consumo por hora se acumula, no se promedia.
    assert meta["mean_type"] == sys.modules[MODELS].StatisticMeanType.NONE
    assert meta["has_sum"] is True
    # Litros: la clase de conversión es volumen (igual que `opower`).
    assert meta["unit_class"] == "volume"
    assert meta["unit_of_measurement"] == "L"
    # `has_mean` está obsoleto: no debe enviarse si hay `mean_type`.
    assert "has_mean" not in meta


def test_metadatos_en_home_assistant_antiguo(recargar_statistics) -> None:
    modulo = recargar_statistics(moderno=False)
    meta = _metadatos(modulo)

    assert meta["has_mean"] is False
    assert meta["has_sum"] is True
    assert "mean_type" not in meta
    # La clave de más provocaría un TypeError al escribir la fila.
    assert "unit_class" not in meta


def test_metadatos_identifican_la_serie(recargar_statistics) -> None:
    modulo = recargar_statistics(moderno=True)
    meta = _metadatos(modulo, entry_id="abc123", titulo="Casa")

    assert meta["statistic_id"] == "emasesa:abc123_consumo_horario"
    assert meta["source"] == "emasesa"
    assert meta["name"] == "Casa consumo por hora"


def test_el_modulo_queda_como_estaba_tras_los_tests() -> None:
    """La recarga de los tests anteriores no debe contaminar la suite."""
    assert statistics._SOPORTA_UNIT_CLASS is True
    assert statistics.StatisticMeanType is not None
