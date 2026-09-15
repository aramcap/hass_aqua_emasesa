"""
Tests de los helpers de parseo de `api.py`.

Son Python puro (sin red, sin Home Assistant): cubren exactamente lo
que se rompe cuando EMASESA cambia algo en el frontend, que es el
riesgo principal de esta integración — el resto del cliente (login,
cookies, reintentos) se prueba aparte, con el HTTP simulado.

Las fixtures de `tests/fixtures/` reproducen el formato real de las
respuestas del portal, incluido el detalle de que PrimeFaces escapa los
guiones de las etiquetas (`"M 01\\-sep"`, `"00\\-01"`).
"""

from __future__ import annotations

from datetime import date

import pytest

from conftest import leer_fixture
from emasesa.api import (
    EmasesaApiError,
    EmasesaAuthError,
    EmasesaTwoFactorRequired,
    _etiqueta_a_fecha,
    _etiqueta_a_hora,
    _extract_max_fecha,
    _extract_viewstate,
    _parse_chart_response,
)


def _grafico(data: str, ticks: str | None = None) -> str:
    """Respuesta mínima con la config de gráfico que genera PrimeFaces."""
    cfg = f"data:{data}" if ticks is None else f"data:{data},ticks:{ticks}"
    return f'PrimeFaces.cw("Chart","w",{{id:"form-date:chart",{cfg},shadow:false}});'


# ---------------------------------------------------------------------
# _parse_chart_response
# ---------------------------------------------------------------------


def test_parse_respuesta_diaria_real() -> None:
    pares = _parse_chart_response(leer_fixture("respuesta_diaria.xml"))

    assert len(pares) == 7
    # Los guiones escapados por PrimeFaces se desescapan.
    assert pares[0] == ("M 01-sep", 125.0)
    assert pares[-1] == ("L 07-sep", 164.8)
    # Un día sin consumo es 0, no un hueco.
    assert pares[2] == ("J 03-sep", 0)


def test_parse_respuesta_horaria_real() -> None:
    pares = _parse_chart_response(leer_fixture("respuesta_horaria.xml"))

    assert len(pares) == 24
    assert pares[0][0] == "00-01"
    assert pares[23][0] == "23-00"
    assert sum(litros for _, litros in pares) == pytest.approx(170.8)


def test_parse_empareja_por_indice_sin_invertir() -> None:
    """`ticks[i]` va con `data[i]`: invertirlo desplazaría todo el histórico."""
    pares = _parse_chart_response(_grafico("[[10,20,30]]", '["a","b","c"]'))

    assert pares == [("a", 10), ("b", 20), ("c", 30)]


def test_parse_sin_ticks_usa_indices() -> None:
    pares = _parse_chart_response(_grafico("[[10,20]]"))

    assert pares == [("0", 10), ("1", 20)]


@pytest.mark.parametrize(
    ("data", "ticks", "esperado"),
    [
        ("[[10,20,30]]", '["a","b"]', [("a", 10), ("b", 20)]),
        ("[[10]]", '["a","b","c"]', [("a", 10)]),
    ],
    ids=["sobran_valores", "sobran_etiquetas"],
)
def test_parse_trunca_al_mas_corto(data: str, ticks: str, esperado: list) -> None:
    """Si EMASESA devuelve listas descuadradas, se trunca en vez de reventar."""
    assert _parse_chart_response(_grafico(data, ticks)) == esperado


def test_parse_admite_valores_negativos() -> None:
    """El regex de `data` acepta el signo: un valor raro no debe tirar la consulta."""
    pares = _parse_chart_response(_grafico("[[-5,10.5]]", '["a","b"]'))

    assert pares == [("a", -5), ("b", 10.5)]


def test_parse_vacio_no_es_error() -> None:
    """Un rango sin lecturas devuelve el array vacío, no una excepción."""
    assert _parse_chart_response(_grafico("[[]]", "[]")) == []


def test_parse_pagina_de_login_es_error_de_auth() -> None:
    """La sesión ha caducado: el cliente debe reintentar el login, no parsear."""
    with pytest.raises(EmasesaAuthError) as err:
        _parse_chart_response(leer_fixture("pagina_login.html"))

    assert not isinstance(err.value, EmasesaTwoFactorRequired)


def test_parse_pagina_2fa_pide_reautenticacion() -> None:
    """EMASESA ha revocado la confianza a mitad de sesión → reauth en HA."""
    with pytest.raises(EmasesaTwoFactorRequired):
        _parse_chart_response(leer_fixture("pagina_2fa.html"))


def test_parse_respuesta_sin_grafico_es_error_generico() -> None:
    """Ni login ni 2FA: o el rango no tiene datos, o cambió el formato."""
    with pytest.raises(EmasesaApiError) as err:
        _parse_chart_response(leer_fixture("respuesta_sin_grafico.xml"))

    assert not isinstance(err.value, EmasesaAuthError)


# ---------------------------------------------------------------------
# _etiqueta_a_fecha
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("etiqueta", "hasta", "esperado"),
    [
        ("M 01-sep", date(2026, 9, 7), date(2026, 9, 1)),
        ("L 07-sep", date(2026, 9, 7), date(2026, 9, 7)),
        # Sin prefijo de día de la semana.
        ("01-sep", date(2026, 9, 7), date(2026, 9, 1)),
        # Espacios de sobra alrededor.
        ("  M 01-sep  ", date(2026, 9, 7), date(2026, 9, 1)),
        # Día de un solo dígito.
        ("J 1-ene", date(2026, 1, 15), date(2026, 1, 1)),
        # Todos los meses se reconocen en minúsculas...
        ("D 15-ago", date(2026, 9, 7), date(2026, 8, 15)),
        # ...y también si EMASESA los capitaliza.
        ("D 15-AGO", date(2026, 9, 7), date(2026, 8, 15)),
    ],
)
def test_etiqueta_a_fecha_casos_normales(
    etiqueta: str, hasta: date, esperado: date
) -> None:
    assert _etiqueta_a_fecha(etiqueta, hasta) == esperado


@pytest.mark.parametrize(
    ("etiqueta", "hasta", "esperado"),
    [
        # Carga masiva de 90 días lanzada en enero: diciembre es del año anterior.
        ("X 28-dic", date(2027, 1, 5), date(2026, 12, 28)),
        ("L 30-nov", date(2027, 1, 5), date(2026, 11, 30)),
        # El mismo mes que `hasta` sigue siendo del año de `hasta`.
        ("V 02-ene", date(2027, 1, 5), date(2027, 1, 2)),
        # Frontera exacta: el mes SIGUIENTE al de `hasta` ya es del año
        # anterior (la ventana siempre mira hacia atrás, nunca al futuro).
        ("M 10-feb", date(2027, 1, 5), date(2026, 2, 10)),
        ("D 08-oct", date(2026, 9, 7), date(2025, 10, 8)),
    ],
)
def test_etiqueta_a_fecha_cruce_de_ano(
    etiqueta: str, hasta: date, esperado: date
) -> None:
    """El año no viene en la etiqueta: se deduce del `hasta` consultado."""
    assert _etiqueta_a_fecha(etiqueta, hasta) == esperado


def test_etiqueta_a_fecha_29_de_febrero_bisiesto() -> None:
    assert _etiqueta_a_fecha("S 29-feb", date(2028, 3, 5)) == date(2028, 2, 29)


@pytest.mark.parametrize(
    ("etiqueta", "motivo"),
    [
        ("13-14", "franja horaria, no fecha"),
        ("23-00", "franja horaria de medianoche"),
        ("01-xyz", "mes inexistente"),
        ("31-feb", "día imposible para ese mes"),
        ("", "etiqueta vacía"),
        ("sep", "sin día"),
        ("2026-09-01", "formato ISO, no el del portal"),
    ],
)
def test_etiqueta_a_fecha_descarta_lo_que_no_entiende(
    etiqueta: str, motivo: str
) -> None:
    """Devuelve None (la lectura se descarta) en vez de inventarse una fecha."""
    assert _etiqueta_a_fecha(etiqueta, date(2026, 9, 7)) is None, motivo


# ---------------------------------------------------------------------
# _etiqueta_a_hora
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("etiqueta", "esperado"),
    [
        ("00-01", 0),
        ("13-14", 13),
        ("23-00", 23),  # la última franja del día vuelve a 00
        ("1-2", 1),  # un solo dígito
        ("  09-10  ", 9),
    ],
)
def test_etiqueta_a_hora_casos_normales(etiqueta: str, esperado: int) -> None:
    assert _etiqueta_a_hora(etiqueta) == esperado


@pytest.mark.parametrize(
    "etiqueta",
    ["24-25", "25-26", "01-sep", "M 01-sep", "", "13", "13-14-15", "ab-cd"],
)
def test_etiqueta_a_hora_descarta_lo_que_no_entiende(etiqueta: str) -> None:
    assert _etiqueta_a_hora(etiqueta) is None


def test_etiqueta_a_hora_cubre_el_dia_entero() -> None:
    """Las 24 franjas de un día se mapean a 0..23, sin huecos ni repetidas."""
    pares = _parse_chart_response(leer_fixture("respuesta_horaria.xml"))
    horas = [_etiqueta_a_hora(etiqueta) for etiqueta, _ in pares]

    assert horas == list(range(24))


# ---------------------------------------------------------------------
# _extract_max_fecha
# ---------------------------------------------------------------------


def test_extract_max_fecha_de_la_pagina_real() -> None:
    """El límite se lee del datepicker, no se asume un retraso fijo en días."""
    assert _extract_max_fecha(leer_fixture("consumo_pagina.html")) == date(2026, 9, 7)


@pytest.mark.parametrize(
    ("html", "motivo"),
    [
        ("<html>sin datepicker</html>", "no hay maxDate"),
        ('maxDate:"31\\/02\\/2026"', "fecha imposible"),
        ('maxDate:"07/09/2026"', "sin las barras escapadas que emite PrimeFaces"),
        ('maxDate:"7\\/9\\/2026"', "sin ceros a la izquierda"),
    ],
)
def test_extract_max_fecha_devuelve_none_si_no_lo_reconoce(
    html: str, motivo: str
) -> None:
    """Sin límite fiable se sigue adelante sin recortar, no se revienta."""
    assert _extract_max_fecha(html) is None, motivo


# ---------------------------------------------------------------------
# _extract_viewstate
# ---------------------------------------------------------------------


def test_extract_viewstate_de_la_pagina_real() -> None:
    assert _extract_viewstate(leer_fixture("consumo_pagina.html")) == "stateless-4242"


def test_extract_viewstate_con_los_atributos_al_reves() -> None:
    """JSF no garantiza el orden de los atributos; hay un segundo intento."""
    html = '<input value="abc123" type="hidden" name="javax.faces.ViewState" />'

    assert _extract_viewstate(html) == "abc123"


def test_extract_viewstate_ausente_avisa_de_cambio_de_frontend() -> None:
    with pytest.raises(EmasesaApiError, match="ViewState"):
        _extract_viewstate("<html><body>nada</body></html>")


# ---------------------------------------------------------------------
# Composición: lo que hace `async_get_daily_readings` una vez tiene la
# respuesta en la mano (sin red).
# ---------------------------------------------------------------------


def test_respuesta_diaria_completa_produce_lecturas_ordenadas() -> None:
    hasta = date(2026, 9, 7)
    pares = _parse_chart_response(leer_fixture("respuesta_diaria.xml"))

    lecturas = [
        {"date": fecha, "litros": float(litros)}
        for etiqueta, litros in pares
        if (fecha := _etiqueta_a_fecha(etiqueta, hasta)) is not None
    ]

    assert len(lecturas) == 7
    assert [r["date"] for r in lecturas] == sorted(r["date"] for r in lecturas)
    assert lecturas[0] == {"date": date(2026, 9, 1), "litros": 125.0}
    assert lecturas[-1] == {"date": hasta, "litros": 164.8}
    assert all(isinstance(r["litros"], float) for r in lecturas)


def test_respuesta_horaria_no_cuela_lecturas_diarias() -> None:
    """
    Si se pide un rango de un solo día, EMASESA devuelve la vista
    horaria: `async_get_daily_readings` debe descartar esas etiquetas
    enteras en vez de interpretarlas como fechas.
    """
    pares = _parse_chart_response(leer_fixture("respuesta_horaria.xml"))

    fechas = [_etiqueta_a_fecha(etiqueta, date(2026, 9, 7)) for etiqueta, _ in pares]

    assert fechas == [None] * 24
