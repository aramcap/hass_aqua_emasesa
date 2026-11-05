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

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from emasesa.coordinator import (
    _MAX_LECTURAS_RECORDADAS,
    EmasesaCoordinator,
    _deserializar_lecturas,
    _serializar_lecturas,
)


def _construir(
    datos_previos: dict | None,
    total: float = 0.0,
    persistidas: list[dict] | None = None,
) -> SimpleNamespace:
    """Lo mínimo que `_build_data` mira de `self`."""
    return SimpleNamespace(
        data=datos_previos,
        _cumulative_total=total,
        _ultimas_lecturas=persistidas or [],
    )


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


# ---------------------------------------------------------------------
# Persistencia de la última lectura válida
#
# "Conservar la última lectura" solo en memoria no basta: al reiniciar
# Home Assistant no hay nada que conservar, y si EMASESA no devuelve
# lecturas en ese primer refresco, los sensores de último día y de última
# fecha de lectura arrancan en "desconocido" — y se quedan así hasta que
# EMASESA vuelva a publicar. Cualquier plantilla o automatización que los
# use se rompe con ello (p. ej. `as_datetime` de 'unknown' da None).
# ---------------------------------------------------------------------


def test_tras_reiniciar_sin_lecturas_se_usan_las_persistidas() -> None:
    """La regresión: arranque en frío (self.data = None) y EMASESA sin datos."""
    coordinator = _construir(None, persistidas=LECTURAS)

    datos = _build(coordinator, [])

    assert datos["latest"] == LECTURAS[-1]
    assert datos["readings"] == LECTURAS


def test_lo_que_hay_en_memoria_tiene_prioridad_sobre_lo_persistido() -> None:
    en_memoria = [{"date": date(2026, 9, 20), "litros": 77.0}]
    previo = {"readings": en_memoria, "latest": en_memoria[-1], "cumulative_total": 0.0}

    datos = _build(_construir(previo, persistidas=LECTURAS), [])

    assert datos["latest"] == en_memoria[-1]


def test_lecturas_nuevas_ganan_siempre_a_las_persistidas() -> None:
    nueva = [{"date": date(2026, 9, 30), "litros": 12.0}]

    datos = _build(_construir(None, persistidas=LECTURAS), nueva)

    assert datos["latest"] == nueva[0]


def test_serializar_y_deserializar_es_un_viaje_de_ida_y_vuelta() -> None:
    crudo = _serializar_lecturas(LECTURAS)

    assert crudo[0] == {"date": "2026-09-14", "litros": 120.0}  # JSON puro
    assert _deserializar_lecturas(crudo) == LECTURAS


@pytest.mark.parametrize("crudo", [None, "basura", 42, {}, []])
def test_deserializar_lo_que_no_es_una_lista_devuelve_vacio(crudo: object) -> None:
    assert _deserializar_lecturas(crudo) == []


def test_deserializar_ignora_solo_las_entradas_corruptas() -> None:
    """Un fichero a medio escribir no debe impedir que arranque la integración."""
    crudo = [
        {"date": "2026-09-14", "litros": 120.0},
        {"date": "no-es-fecha", "litros": 1.0},
        {"litros": 5.0},
        {"date": "2026-09-15", "litros": "no-es-numero"},
        "ni-siquiera-un-dict",
        {"date": "2026-09-16", "litros": 99},
    ]

    resultado = _deserializar_lecturas(crudo)

    assert [r["date"] for r in resultado] == [date(2026, 9, 14), date(2026, 9, 16)]
    assert resultado[1]["litros"] == 99.0


def test_deserializar_devuelve_las_lecturas_ordenadas() -> None:
    crudo = [
        {"date": "2026-09-16", "litros": 3.0},
        {"date": "2026-09-14", "litros": 1.0},
    ]

    assert [r["date"] for r in _deserializar_lecturas(crudo)] == [
        date(2026, 9, 14),
        date(2026, 9, 16),
    ]


class _AlmacenFalso:
    def __init__(self, contenido: dict | None = None) -> None:
        self.contenido = contenido
        self.guardado: dict | None = None

    async def async_load(self) -> dict | None:
        return self.contenido

    async def async_save(self, datos: dict) -> None:
        self.guardado = datos


def _coordinator_con_almacen(almacen: _AlmacenFalso) -> SimpleNamespace:
    return SimpleNamespace(
        _store=almacen,
        _cumulative_total=0.0,
        _counted_dates=set(),
        _ultimas_lecturas=[],
        _storage_loaded=False,
    )


def test_la_carga_restaura_las_lecturas_guardadas() -> None:
    almacen = _AlmacenFalso(
        {
            "total": 1234.5,
            "counted_dates": ["2026-09-14", "2026-09-15"],
            "ultimas_lecturas": _serializar_lecturas(LECTURAS),
        }
    )
    coordinator = _coordinator_con_almacen(almacen)

    asyncio.run(EmasesaCoordinator.async_load_storage(coordinator))

    assert coordinator._ultimas_lecturas == LECTURAS
    assert coordinator._cumulative_total == 1234.5


def test_la_carga_de_un_fichero_antiguo_sin_lecturas_no_falla() -> None:
    """Instalaciones anteriores a esta versión: el campo no existe todavía."""
    almacen = _AlmacenFalso({"total": 50.0, "counted_dates": ["2026-09-14"]})
    coordinator = _coordinator_con_almacen(almacen)

    asyncio.run(EmasesaCoordinator.async_load_storage(coordinator))

    assert coordinator._ultimas_lecturas == []
    assert coordinator._cumulative_total == 50.0
    assert coordinator._counted_dates == {"2026-09-14"}


def test_el_guardado_incluye_las_lecturas() -> None:
    almacen = _AlmacenFalso()
    coordinator = _coordinator_con_almacen(almacen)
    coordinator._ultimas_lecturas = list(LECTURAS)
    coordinator._cumulative_total = 99.0

    asyncio.run(EmasesaCoordinator._async_save_storage(coordinator))

    assert almacen.guardado["ultimas_lecturas"] == _serializar_lecturas(LECTURAS)
    assert almacen.guardado["total"] == 99.0


def test_recordar_lecturas_avisa_de_si_ha_cambiado_algo() -> None:
    coordinator = _construir(None)

    assert EmasesaCoordinator._recordar_lecturas(coordinator, LECTURAS) is True
    assert coordinator._ultimas_lecturas == LECTURAS
    # Lo mismo otra vez: nada que persistir (evita escribir en disco de balde).
    assert EmasesaCoordinator._recordar_lecturas(coordinator, LECTURAS) is False


def test_recordar_lecturas_vacias_no_borra_lo_memorizado() -> None:
    coordinator = _construir(None, persistidas=list(LECTURAS))

    assert EmasesaCoordinator._recordar_lecturas(coordinator, []) is False
    assert coordinator._ultimas_lecturas == LECTURAS


def test_recordar_lecturas_se_queda_con_las_mas_recientes() -> None:
    inicio = date(2026, 1, 1)
    muchas = [
        {"date": inicio + timedelta(days=i), "litros": float(i)}
        for i in range(_MAX_LECTURAS_RECORDADAS + 10)
    ]
    coordinator = _construir(None)

    EmasesaCoordinator._recordar_lecturas(coordinator, muchas)

    assert len(coordinator._ultimas_lecturas) == _MAX_LECTURAS_RECORDADAS
    assert coordinator._ultimas_lecturas[-1] == muchas[-1]
    assert coordinator._ultimas_lecturas[0] == muchas[10]


# ---------------------------------------------------------------------
# Cuándo se escribe en disco
#
# El caso que importa es el de la ACTUALIZACIÓN: un contador ya tiene todos
# sus días contados, así que la primera lectura tras instalar esta versión
# cambia las lecturas memorizadas pero NO el contador. Si solo se guardara
# cuando cambia el contador, lo memorizado no llegaría a disco hasta que
# apareciera un día nuevo, justo cuando más falta hace.
# ---------------------------------------------------------------------


def _coordinator_real(
    almacen: _AlmacenFalso, contados: list[date], persistidas: list[dict] | None = None
) -> EmasesaCoordinator:
    """Un coordinator de verdad (sin pasar por `__init__`) con el almacén simulado."""
    c = EmasesaCoordinator.__new__(EmasesaCoordinator)
    c._store = almacen
    c._cumulative_total = 100.0
    c._counted_dates = {d.isoformat() for d in contados}
    c._ultimas_lecturas = list(persistidas or [])
    return c


def test_se_guarda_aunque_solo_cambien_las_lecturas_y_no_el_contador() -> None:
    almacen = _AlmacenFalso()
    c = _coordinator_real(almacen, contados=[r["date"] for r in LECTURAS])

    asyncio.run(c._async_registrar_lecturas(LECTURAS))

    assert almacen.guardado is not None
    assert almacen.guardado["ultimas_lecturas"] == _serializar_lecturas(LECTURAS)
    assert almacen.guardado["total"] == 100.0  # el contador no se ha tocado


def test_no_se_escribe_en_disco_si_no_ha_cambiado_nada() -> None:
    almacen = _AlmacenFalso()
    c = _coordinator_real(
        almacen, contados=[r["date"] for r in LECTURAS], persistidas=LECTURAS
    )

    asyncio.run(c._async_registrar_lecturas(LECTURAS))

    assert almacen.guardado is None


def test_una_actualizacion_sin_lecturas_no_escribe_ni_borra() -> None:
    almacen = _AlmacenFalso()
    c = _coordinator_real(
        almacen, contados=[r["date"] for r in LECTURAS], persistidas=LECTURAS
    )

    asyncio.run(c._async_registrar_lecturas([]))

    assert almacen.guardado is None
    assert c._ultimas_lecturas == LECTURAS


def test_un_dia_nuevo_suma_al_contador_y_se_guarda() -> None:
    almacen = _AlmacenFalso()
    c = _coordinator_real(
        almacen, contados=[r["date"] for r in LECTURAS], persistidas=LECTURAS
    )
    nueva = LECTURAS + [{"date": date(2026, 9, 16), "litros": 50.0}]

    asyncio.run(c._async_registrar_lecturas(nueva))

    assert almacen.guardado["total"] == 150.0
    assert almacen.guardado["ultimas_lecturas"][-1]["date"] == "2026-09-16"
