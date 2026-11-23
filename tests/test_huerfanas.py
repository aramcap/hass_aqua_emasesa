"""
Tests del aviso de series huérfanas (`huerfanas.py`).

El riesgo de esta función no es no avisar, sino avisar de más: un falso
positivo en cada arranque enseña al usuario a ignorar el registro. De ahí
que casi todos los tests sean sobre cuándo NO debe avisar.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import emasesa.huerfanas as huerfanas
from emasesa.huerfanas import encontrar_huerfanas, mensaje_de_huerfana
from emasesa.statistics import EmasesaHourlyStatisticsImporter, statistic_id_de

MIA = "emasesa:01m2md0gqm5zjay3zpsv8dyfyg_consumo_horario"
ANTIGUA = "emasesa:01jbq8z3k4m5n6p7q8r9s0t1u2_consumo_horario"


def _serie(statistic_id: str, name: str = "EMASESA consumo por hora", source: str = "emasesa") -> dict:
    return {"statistic_id": statistic_id, "name": name, "source": source}


# ---------------------------------------------------------------------
# El id esperado tiene que ser EXACTAMENTE el que escribe el importador
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry_id",
    ["01M2MD0GQM5ZJAY3ZPSV8DYFYG", "abc123", "con-guiones-y--dobles", "_raro_", "!!!", ""],
)
def test_el_id_esperado_coincide_con_el_que_escribe_el_importador(entry_id: str) -> None:
    """
    Si divergieran, TODAS las series saldrían como huérfanas en cada
    arranque. Es la propiedad más importante de este módulo.
    """
    importador = EmasesaHourlyStatisticsImporter(None, entry_id, "Casa", None)

    assert statistic_id_de(entry_id) == importador.statistic_id


# ---------------------------------------------------------------------
# encontrar_huerfanas
# ---------------------------------------------------------------------


def test_detecta_la_serie_de_una_cuenta_que_ya_no_existe() -> None:
    existentes = [_serie(MIA), _serie(ANTIGUA)]

    resultado = encontrar_huerfanas(existentes, esperadas={MIA})

    assert [e["statistic_id"] for e in resultado] == [ANTIGUA]


def test_con_todas_las_series_esperadas_no_hay_huerfanas() -> None:
    assert encontrar_huerfanas([_serie(MIA)], esperadas={MIA}) == []


def test_sin_series_de_emasesa_no_hay_nada_que_avisar() -> None:
    assert encontrar_huerfanas([], esperadas={MIA}) == []


def test_varias_cuentas_configuradas_ninguna_es_huerfana() -> None:
    """Dos contratos con el mismo nombre: las dos series son legítimas."""
    otra = "emasesa:0123456789abcdef_consumo_horario"

    assert encontrar_huerfanas([_serie(MIA), _serie(otra)], esperadas={MIA, otra}) == []


@pytest.mark.parametrize(
    "ajena",
    [
        _serie("sensor.consumo_agua", source="recorder"),  # estadística de una entidad
        _serie("opower:cuenta_consumo_horario", source="opower"),  # otra integración
        _serie("emasesa:abc_otra_cosa"),  # nuestra, pero no una serie horaria
        _serie("emasesa:abc_consumo_horario", source="otra"),  # id parecido, otro origen
        _serie("otro:abc_consumo_horario"),  # dice ser nuestro pero el id es de otro dominio
    ],
    ids=["entidad", "otra_integracion", "otro_sufijo", "otro_origen", "id_de_otro_dominio"],
)
def test_ignora_lo_que_no_es_una_serie_horaria_de_esta_integracion(ajena: dict) -> None:
    assert encontrar_huerfanas([ajena], esperadas={MIA}) == []


def test_tolera_estadisticas_incompletas() -> None:
    """El recorder puede devolver entradas sin nombre; no debe romper la comprobación."""
    assert encontrar_huerfanas([{"statistic_id": ANTIGUA, "source": "emasesa"}], {MIA}) != []
    assert encontrar_huerfanas([{}, {"source": "emasesa"}], {MIA}) == []


def test_las_huerfanas_salen_ordenadas_por_id() -> None:
    b, a = "emasesa:b_consumo_horario", "emasesa:a_consumo_horario"

    resultado = encontrar_huerfanas([_serie(b), _serie(a)], esperadas=set())

    assert [e["statistic_id"] for e in resultado] == [a, b]


# ---------------------------------------------------------------------
# El mensaje
# ---------------------------------------------------------------------


def test_el_mensaje_permite_distinguir_las_dos_series_que_se_llaman_igual() -> None:
    texto = mensaje_de_huerfana(_serie(ANTIGUA), [_serie(MIA)])

    # Ambos identificadores: es lo único que las distingue.
    assert ANTIGUA in texto
    assert MIA in texto
    assert "recorder/clear_statistics" in texto
    # Y deja claro que la integración no ha borrado nada.
    assert "no borra nada" in texto


def test_el_mensaje_sin_serie_activa_no_inventa_una() -> None:
    texto = mensaje_de_huerfana(_serie(ANTIGUA), [])

    assert ANTIGUA in texto
    assert "aún no existe" in texto


# ---------------------------------------------------------------------
# La comprobación contra el recorder (con Home Assistant simulado)
# ---------------------------------------------------------------------


def _hass(*entry_ids: str) -> SimpleNamespace:
    entradas = [SimpleNamespace(entry_id=e) for e in entry_ids]
    return SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(async_entries=lambda dominio: entradas),
    )


def _con_recorder(monkeypatch: pytest.MonkeyPatch, existentes: list[dict]) -> None:
    async def _listar(hass, *args, **kwargs):
        return existentes

    monkeypatch.setattr(huerfanas, "async_list_statistic_ids", _listar)


ENTRADA = SimpleNamespace(entry_id="01M2MD0GQM5ZJAY3ZPSV8DYFYG")


def test_avisa_de_una_huerfana(monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    _con_recorder(monkeypatch, [_serie(MIA), _serie(ANTIGUA)])

    with caplog.at_level(logging.WARNING):
        asyncio.run(
            huerfanas.async_avisar_de_series_huerfanas(_hass("01M2MD0GQM5ZJAY3ZPSV8DYFYG"), ENTRADA)
        )

    avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(avisos) == 1
    assert ANTIGUA in avisos[0].getMessage()


def test_sin_huerfanas_no_avisa_de_nada(monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    _con_recorder(monkeypatch, [_serie(MIA)])

    with caplog.at_level(logging.WARNING):
        asyncio.run(
            huerfanas.async_avisar_de_series_huerfanas(_hass("01M2MD0GQM5ZJAY3ZPSV8DYFYG"), ENTRADA)
        )

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_no_repite_el_aviso_al_recargar_la_integracion(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Cambiar una opción recarga la integración: no debe volver a avisar de lo mismo."""
    _con_recorder(monkeypatch, [_serie(MIA), _serie(ANTIGUA)])
    hass = _hass("01M2MD0GQM5ZJAY3ZPSV8DYFYG")

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            asyncio.run(huerfanas.async_avisar_de_series_huerfanas(hass, ENTRADA))

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_una_cuenta_deshabilitada_no_deja_huerfana_su_serie(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`async_entries` devuelve también las deshabilitadas: su serie se reanuda al habilitarla."""
    _con_recorder(monkeypatch, [_serie(MIA), _serie(ANTIGUA)])
    hass = _hass("01M2MD0GQM5ZJAY3ZPSV8DYFYG", "01jbq8z3k4m5n6p7q8r9s0t1u2")

    with caplog.at_level(logging.WARNING):
        asyncio.run(huerfanas.async_avisar_de_series_huerfanas(hass, ENTRADA))

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_un_fallo_al_consultar_el_recorder_no_rompe_el_arranque(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _falla(hass, *args, **kwargs):
        raise RuntimeError("recorder no disponible")

    monkeypatch.setattr(huerfanas, "async_list_statistic_ids", _falla)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            huerfanas.async_avisar_de_series_huerfanas(_hass("01M2MD0GQM5ZJAY3ZPSV8DYFYG"), ENTRADA)
        )

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert "No se ha podido comprobar" in caplog.text


def test_el_arranque_ejecuta_la_comprobacion() -> None:
    """Sin esto la función existe pero no se llama nunca."""
    from conftest import INTEGRACION

    init = (INTEGRACION / "__init__.py").read_text(encoding="utf-8")

    assert "await async_avisar_de_series_huerfanas(hass, entry)" in init
