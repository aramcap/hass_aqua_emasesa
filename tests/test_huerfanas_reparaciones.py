"""
Tests del borrado de series huérfanas desde Reparaciones.

Borrar la serie es IRREVERSIBLE y el recorder borra por `statistic_id` sin
mirar de quién es, así que el riesgo que importa aquí no es que no se borre,
sino que se borre lo que no toca: la serie viva, o algo de otra integración.
De ahí que la mayoría de los tests sean sobre cuándo NO se debe tocar el
recorder.

Home Assistant se simula (ver `conftest.py`): se ejercita la decisión de
qué borrar y cuándo, no el comportamiento de Reparaciones. El recorder falso
avisa de que ha terminado desde OTRO HILO, como el real, para comprobar que
la espera es segura entre hilos.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

import emasesa.huerfanas as huerfanas
import emasesa.repairs as repairs
from emasesa.const import ZONA_EMASESA
from emasesa.huerfanas import (
    async_avisar_de_series_huerfanas,
    async_borrar_serie,
    incidencia_de,
    placeholders_de_huerfana,
)

ENTRY_VIVA = "01M2MD0GQM5ZJAY3ZPSV8DYFYG"
VIVA = "emasesa:01m2md0gqm5zjay3zpsv8dyfyg_consumo_horario"
ANTIGUA = "emasesa:01jbq8z3k4m5n6p7q8r9s0t1u2_consumo_horario"
ENTRADA = SimpleNamespace(entry_id=ENTRY_VIVA)


def _serie(statistic_id: str, name: str = "EMASESA consumo por hora", source: str = "emasesa") -> dict:
    return {"statistic_id": statistic_id, "name": name, "source": source}


class _RegistroDeIncidencias:
    """Sustituye al registro de incidencias de Home Assistant."""

    IssueSeverity = SimpleNamespace(WARNING="warning")

    def __init__(self) -> None:
        self.creadas: list[tuple[str, dict]] = []
        self.borradas: list[str] = []

    def async_create_issue(self, hass, domain, issue_id, **kwargs) -> None:
        self.creadas.append((issue_id, kwargs))

    def async_delete_issue(self, hass, domain, issue_id) -> None:
        self.borradas.append(issue_id)


class _RecorderFalso:
    def __init__(self, confirma: bool = True) -> None:
        self.confirma = confirma
        self.ordenes_de_borrado: list[list[str]] = []

    def async_clear_statistics(self, ids, *, on_done=None) -> None:
        self.ordenes_de_borrado.append(list(ids))
        if self.confirma and on_done:
            # El recorder real avisa desde su propio hilo.
            threading.Thread(target=lambda: (time.sleep(0.01), on_done())).start()

    async def async_add_executor_job(self, funcion, *args):
        return funcion(*args)


class _BucleEspia:
    """Delega en el bucle real y anota cuántas veces se usa la vía segura entre hilos."""

    def __init__(self, real) -> None:
        self._real = real
        self.llamadas_threadsafe = 0

    def call_soon_threadsafe(self, funcion, *args):
        self.llamadas_threadsafe += 1
        return self._real.call_soon_threadsafe(funcion, *args)


@pytest.fixture
def entorno(monkeypatch: pytest.MonkeyPatch):
    """
    Devuelve una función que monta el entorno. `existentes` es lo que lista
    el recorder; `cuentas` los entry_id de las cuentas configuradas.
    """

    def _montar(existentes: list[dict], cuentas: tuple[str, ...] = (ENTRY_VIVA,), confirma: bool = True):
        incidencias = _RegistroDeIncidencias()
        recorder = _RecorderFalso(confirma=confirma)
        estado = {"existentes": existentes}

        async def _listar(hass, *args, **kwargs):
            return estado["existentes"]

        def _ultimas(hass, numero, statistic_id, convertir, tipos):
            inicio = datetime(2026, 9, 15, 23, 0, tzinfo=ZONA_EMASESA).timestamp()
            return {statistic_id: [{"start": inicio, "sum": 3427.0}]}

        for modulo in (huerfanas, repairs):
            monkeypatch.setattr(modulo, "ir", incidencias)
        monkeypatch.setattr(huerfanas, "async_list_statistic_ids", _listar)
        monkeypatch.setattr(huerfanas, "get_last_statistics", _ultimas)
        monkeypatch.setattr(huerfanas, "get_instance", lambda hass: recorder)
        monkeypatch.setattr(huerfanas, "SEGUNDOS_ESPERA_BORRADO", 0.2)

        bucles: list[_BucleEspia] = []

        def _hass() -> SimpleNamespace:
            entradas = [SimpleNamespace(entry_id=e) for e in cuentas]
            bucles.append(_BucleEspia(asyncio.get_running_loop()))
            return SimpleNamespace(
                loop=bucles[-1],
                data={},
                config_entries=SimpleNamespace(async_entries=lambda dominio: entradas),
            )

        return SimpleNamespace(
            incidencias=incidencias, recorder=recorder, estado=estado, hass=_hass, bucles=bucles
        )

    return _montar


def _flujo(hass, statistic_id: str | None = ANTIGUA, issue_id: str | None = None):
    flujo = repairs.BorrarSerieHuerfanaFlow()
    flujo.hass = hass
    flujo.issue_id = issue_id or (incidencia_de(statistic_id) if statistic_id else "serie_huerfana_x")
    flujo.data = {"statistic_id": statistic_id} if statistic_id is not None else None
    return flujo


# ---------------------------------------------------------------------
# async_borrar_serie: las salvaguardas
# ---------------------------------------------------------------------


def test_borra_exactamente_la_serie_pedida_y_espera_a_que_termine(entorno) -> None:
    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])

        await async_borrar_serie(e.hass(), ANTIGUA)

        assert e.recorder.ordenes_de_borrado == [[ANTIGUA]]

    asyncio.run(prueba())


def test_el_aviso_del_recorder_cruza_de_hilo_por_la_via_segura(entorno) -> None:
    """
    El recorder avisa de que ha terminado desde su propio hilo. Tocar el
    `Event` directamente desde ahí no despierta al bucle: la espera se
    alargaría hasta el timeout aunque el resultado fuera el mismo, así que
    se comprueba el mecanismo y no el tiempo.
    """

    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])
        hass = e.hass()

        await async_borrar_serie(hass, ANTIGUA)

        assert hass.loop.llamadas_threadsafe == 1

    asyncio.run(prueba())


def test_nunca_borra_la_serie_de_una_cuenta_configurada(entorno) -> None:
    """LA propiedad de este módulo: la serie viva no se toca ni aunque se pida."""

    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])

        with pytest.raises(ValueError, match="cuenta configurada"):
            await async_borrar_serie(e.hass(), VIVA)

        assert e.recorder.ordenes_de_borrado == []

    asyncio.run(prueba())


def test_la_serie_de_una_cuenta_deshabilitada_tampoco_se_borra(entorno) -> None:
    """`async_entries` devuelve también las deshabilitadas, y su serie se reanuda al habilitarla."""

    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)], cuentas=(ENTRY_VIVA, "01jbq8z3k4m5n6p7q8r9s0t1u2"))

        with pytest.raises(ValueError):
            await async_borrar_serie(e.hass(), ANTIGUA)

        assert e.recorder.ordenes_de_borrado == []

    asyncio.run(prueba())


@pytest.mark.parametrize(
    "ajeno",
    [
        "sensor.consumo_agua",  # estadística de una entidad
        "opower:cuenta_consumo_horario",  # otra integración, mismo sufijo
        "emasesa:abc_otra_cosa",  # nuestra pero no horaria
        "emasesa:",  # degenerado
        "",
    ],
)
def test_rechaza_ids_que_no_son_de_esta_integracion(entorno, ajeno: str) -> None:
    """El recorder borraría cualquier id sin preguntar: el filtro tiene que estar aquí."""

    async def prueba() -> None:
        e = entorno([_serie(VIVA)])

        with pytest.raises(ValueError):
            await async_borrar_serie(e.hass(), ajeno)

        assert e.recorder.ordenes_de_borrado == []

    asyncio.run(prueba())


def test_si_el_recorder_no_confirma_a_tiempo_lo_dice(entorno) -> None:
    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)], confirma=False)

        with pytest.raises(TimeoutError):
            await async_borrar_serie(e.hass(), ANTIGUA)

        # La orden sí se envió: queda encolada y se ejecutará.
        assert e.recorder.ordenes_de_borrado == [[ANTIGUA]]

    asyncio.run(prueba())


# ---------------------------------------------------------------------
# Incidencias
# ---------------------------------------------------------------------


def test_crea_una_incidencia_reparable_por_cada_huerfana(entorno) -> None:
    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])

        await async_avisar_de_series_huerfanas(e.hass(), ENTRADA)

        assert [i for i, _ in e.incidencias.creadas] == [incidencia_de(ANTIGUA)]
        _, kw = e.incidencias.creadas[0]
        assert kw["is_fixable"] is True
        assert kw["translation_key"] == "serie_huerfana"
        # Los datos que recibirá el flujo: el id de la serie, nada más.
        assert kw["data"] == {"statistic_id": ANTIGUA}
        # Home Assistant exige que todos los placeholders sean str.
        assert all(isinstance(v, str) for v in kw["translation_placeholders"].values())
        assert kw["translation_placeholders"]["ultimo_dato"].startswith("Su último dato es del 15/09/2026 23:00")

    asyncio.run(prueba())


def test_sin_huerfanas_no_se_crea_ninguna_incidencia(entorno) -> None:
    async def prueba() -> None:
        e = entorno([_serie(VIVA)])

        await async_avisar_de_series_huerfanas(e.hass(), ENTRADA)

        assert e.incidencias.creadas == []

    asyncio.run(prueba())


def test_retira_las_incidencias_que_ya_no_aplican(entorno) -> None:
    """Si la serie desaparece por otra vía con Home Assistant en marcha."""

    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])
        hass = e.hass()
        await async_avisar_de_series_huerfanas(hass, ENTRADA)

        e.estado["existentes"] = [_serie(VIVA)]  # alguien la borró
        await async_avisar_de_series_huerfanas(hass, ENTRADA)

        assert e.incidencias.borradas == [incidencia_de(ANTIGUA)]

    asyncio.run(prueba())


def test_no_retira_una_incidencia_que_sigue_vigente(entorno) -> None:
    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])
        hass = e.hass()

        await async_avisar_de_series_huerfanas(hass, ENTRADA)
        await async_avisar_de_series_huerfanas(hass, ENTRADA)

        assert e.incidencias.borradas == []

    asyncio.run(prueba())


def test_un_fallo_del_recorder_no_rompe_el_arranque_ni_toca_incidencias(entorno, monkeypatch) -> None:
    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])

        async def _falla(hass, *a, **k):
            raise RuntimeError("recorder no disponible")

        monkeypatch.setattr(huerfanas, "async_list_statistic_ids", _falla)

        await async_avisar_de_series_huerfanas(e.hass(), ENTRADA)

        assert e.incidencias.creadas == [] and e.incidencias.borradas == []

    asyncio.run(prueba())


def test_el_id_corto_distingue_dos_huerfanas_en_el_titulo() -> None:
    a = placeholders_de_huerfana(_serie(ANTIGUA), [], "")
    b = placeholders_de_huerfana(_serie("emasesa:ffff0000aaaa1111_consumo_horario"), [], "")

    assert a["id_corto"] == "01jbq8z3"
    assert a["id_corto"] != b["id_corto"]


# ---------------------------------------------------------------------
# El flujo de reparación
# ---------------------------------------------------------------------


def test_mostrar_la_confirmacion_no_borra_nada(entorno) -> None:
    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])

        resultado = await _flujo(e.hass()).async_step_init()

        assert resultado["type"] == "form" and resultado["step_id"] == "confirm"
        assert e.recorder.ordenes_de_borrado == []
        # Y la confirmación dice qué se va a borrar y cuál es la buena.
        textos = resultado["description_placeholders"]
        assert textos["statistic_id"] == ANTIGUA
        assert VIVA in textos["activa"]
        assert "15/09/2026" in textos["ultimo_dato"]

    asyncio.run(prueba())


def test_confirmar_borra_la_serie_y_termina_el_flujo(entorno) -> None:
    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])

        resultado = await _flujo(e.hass()).async_step_confirm({})

        assert resultado["type"] == "create_entry"
        assert e.recorder.ordenes_de_borrado == [[ANTIGUA]]
        # Terminar sin abortar hace que Reparaciones retire la incidencia sola:
        # no hay que borrarla aquí.
        assert e.incidencias.borradas == []

    asyncio.run(prueba())


def test_si_la_serie_ya_no_es_huerfana_no_se_borra_y_se_retira_la_incidencia(entorno) -> None:
    """Se vuelve a añadir la cuenta mientras la incidencia seguía abierta."""

    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)], cuentas=(ENTRY_VIVA, "01jbq8z3k4m5n6p7q8r9s0t1u2"))

        resultado = await _flujo(e.hass()).async_step_confirm({})

        assert resultado["type"] == "abort" and resultado["reason"] == "ya_no_es_huerfana"
        assert e.recorder.ordenes_de_borrado == []
        # Abortar NO retira la incidencia por sí solo: hay que hacerlo a mano.
        assert e.incidencias.borradas == [incidencia_de(ANTIGUA)]

    asyncio.run(prueba())


def test_si_la_serie_ya_no_existe_no_hay_nada_que_borrar(entorno) -> None:
    async def prueba() -> None:
        e = entorno([_serie(VIVA)])  # la antigua ya se borró por otra vía

        resultado = await _flujo(e.hass()).async_step_confirm({})

        assert resultado["reason"] == "ya_no_es_huerfana"
        assert e.recorder.ordenes_de_borrado == []

    asyncio.run(prueba())


def test_la_serie_viva_no_se_borra_ni_aunque_alguien_forje_la_incidencia(entorno) -> None:
    """Un `data` con el id de la serie viva (bug futuro, edición del registro...)."""

    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])

        resultado = await _flujo(e.hass(), statistic_id=VIVA).async_step_confirm({})

        assert resultado["type"] == "abort"
        assert e.recorder.ordenes_de_borrado == []

    asyncio.run(prueba())


@pytest.mark.parametrize("datos", [None, {}, {"statistic_id": ""}, {"statistic_id": 42}])
def test_una_incidencia_sin_datos_validos_se_retira_sin_tocar_el_recorder(entorno, datos) -> None:
    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])
        flujo = _flujo(e.hass())
        flujo.data = datos

        resultado = await flujo.async_step_confirm({})

        assert resultado["reason"] == "datos_invalidos"
        assert e.recorder.ordenes_de_borrado == []
        assert e.incidencias.borradas == [flujo.issue_id]

    asyncio.run(prueba())


def test_si_el_recorder_no_confirma_el_borrado_la_incidencia_se_conserva(entorno) -> None:
    """No se da por hecho lo que no está confirmado, y se puede reintentar."""

    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)], confirma=False)

        resultado = await _flujo(e.hass()).async_step_confirm({})

        assert resultado["type"] == "abort" and resultado["reason"] == "tiempo_agotado"
        assert e.incidencias.borradas == []

    asyncio.run(prueba())


def test_si_el_recorder_falla_al_consultar_no_se_borra(entorno, monkeypatch) -> None:
    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])

        async def _falla(hass, *a, **k):
            raise RuntimeError("recorder no disponible")

        monkeypatch.setattr(huerfanas, "async_list_statistic_ids", _falla)

        resultado = await _flujo(e.hass()).async_step_confirm({})

        assert resultado["reason"] == "recorder_no_disponible"
        assert e.recorder.ordenes_de_borrado == []
        assert e.incidencias.borradas == []  # no sabemos nada: la incidencia se queda

    asyncio.run(prueba())


# ---------------------------------------------------------------------
# La plataforma `repairs`
# ---------------------------------------------------------------------


def test_crea_el_flujo_para_las_incidencias_de_series_huerfanas() -> None:
    flujo = asyncio.run(repairs.async_create_fix_flow(None, incidencia_de(ANTIGUA), {}))

    assert isinstance(flujo, repairs.BorrarSerieHuerfanaFlow)


def test_rechaza_incidencias_que_no_conoce() -> None:
    with pytest.raises(ValueError):
        asyncio.run(repairs.async_create_fix_flow(None, "otra_cosa", {}))


def test_no_poder_leer_el_ultimo_dato_no_impide_confirmar(entorno, monkeypatch) -> None:
    """Es información de apoyo para decidir: si falla, se dice y el flujo sigue."""

    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])

        def _falla(*args):
            raise RuntimeError("lectura imposible")

        monkeypatch.setattr(huerfanas, "get_last_statistics", _falla)

        resultado = await _flujo(e.hass()).async_step_init()

        assert resultado["type"] == "form"
        assert resultado["description_placeholders"]["ultimo_dato"] == "No se ha podido leer su último dato."

    asyncio.run(prueba())


def test_una_serie_sin_puntos_lo_dice(entorno, monkeypatch) -> None:
    async def prueba() -> None:
        e = entorno([_serie(VIVA), _serie(ANTIGUA)])
        monkeypatch.setattr(huerfanas, "get_last_statistics", lambda *a: {})

        resultado = await _flujo(e.hass()).async_step_init()

        assert resultado["description_placeholders"]["ultimo_dato"] == "No contiene ningún punto."

    asyncio.run(prueba())
