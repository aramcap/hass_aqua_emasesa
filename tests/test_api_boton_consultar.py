"""
Tests del descubrimiento del botón "Consultar" de la pantalla de consumo.

Síntoma real: desde principios de noviembre de 2026 TODAS las consultas
diarias, también las de 28-30 días, recibían la vista horaria de un solo
día ("no ha devuelto ninguna lectura diaria interpretable ... todas son
franjas horarias"), así que ni ampliar la ventana servía de nada.

El botón se enviaba con un identificador fijo (`form-date:j_idt169`). Esos
`j_idtNNN` los genera JSF según la posición del componente y cambian cuando
EMASESA retoca la pantalla (ya pasó con la verificación en dos pasos). Si el
POST nombra un botón que no existe, JSF no ejecuta la consulta y devuelve el
gráfico por defecto: el desglose horario del último día.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from conftest import leer_fixture
from emasesa.api import (
    FIELD_CONSULTAR_BTN,
    EmasesaApiClient,
    _descubrir_boton_consultar,
)

# Botón tal y como lo pinta PrimeFaces (`p:commandButton`): el texto va
# dentro de un <span> y el envío es AJAX desde el `onclick`.
_BOTON_PRIMEFACES = (
    '<button id="form-date:j_idt171" name="form-date:j_idt171" '
    'class="ui-button ui-widget ui-state-default ui-corner-all ui-button-text-only" '
    'onclick="PrimeFaces.ab({s:&quot;form-date:j_idt171&quot;,f:&quot;form-date&quot;});'
    'return false;" type="submit"><span class="ui-button-text ui-c">Consultar</span></button>'
)


def _pagina_con(boton_html: str) -> str:
    """La página de la fixture con su botón sustituido por `boton_html`."""
    original = (
        '<button id="form-date:j_idt169" name="form-date:j_idt169" '
        'type="submit">Consultar</button>'
    )
    pagina = leer_fixture("consumo_pagina.html")
    assert original in pagina
    return pagina.replace(original, boton_html)


def test_encuentra_el_boton_de_la_fixture() -> None:
    assert _descubrir_boton_consultar(leer_fixture("consumo_pagina.html")) == "form-date:j_idt169"


def test_encuentra_el_boton_renumerado() -> None:
    """La regresión: con el id fijo, el portal devolvía la vista por defecto."""
    assert _descubrir_boton_consultar(_pagina_con(_BOTON_PRIMEFACES)) == "form-date:j_idt171"


def test_prefiere_el_boton_consultar_a_otros_botones() -> None:
    otros = (
        '<button id="form-date:j_idt160" name="form-date:j_idt160" type="button">'
        '<span class="ui-button-icon-left ui-icon ui-c pi pi-calendar"></span></button>'
        '<button id="form-date:j_idt175" name="form-date:j_idt175" type="submit">'
        '<span class="ui-button-text ui-c">Descargar Excel</span></button>'
    )

    assert _descubrir_boton_consultar(_pagina_con(otros + _BOTON_PRIMEFACES)) == "form-date:j_idt171"


def test_un_unico_boton_con_otro_texto_se_usa() -> None:
    """Si EMASESA cambia la etiqueta (p. ej. "Buscar") pero solo hay un botón, es ese."""
    boton = '<input type="submit" name="form-date:j_idt180" value="Buscar" />'

    assert _descubrir_boton_consultar(_pagina_con(boton)) == "form-date:j_idt180"


def test_ignora_botones_de_otros_formularios() -> None:
    ajeno = '<button id="formMenu:j_idt20" name="formMenu:j_idt20" type="submit">Consultar</button>'

    assert _descubrir_boton_consultar(_pagina_con(ajeno + _BOTON_PRIMEFACES)) == "form-date:j_idt171"


def test_sin_boton_reconocible_cae_al_conocido_y_avisa(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        boton = _descubrir_boton_consultar(_pagina_con(""))

    assert boton == FIELD_CONSULTAR_BTN
    assert "No se ha encontrado el botón 'Consultar'" in caplog.text


# ---------------------------------------------------------------------
# El cliente envía el botón descubierto
# ---------------------------------------------------------------------


class _Respuesta:
    def __init__(self, texto: str) -> None:
        self._texto = texto

    async def __aenter__(self) -> "_Respuesta":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def text(self) -> str:
        return self._texto


class _SesionFalsa:
    """Sirve la página de consumo en el GET y apunta el cuerpo de cada POST."""

    def __init__(self, pagina: str) -> None:
        self.pagina = pagina
        self.posts: list[dict] = []

    def get(self, url: str, **kwargs: object) -> _Respuesta:
        return _Respuesta(self.pagina)

    def post(self, url: str, data: dict, **kwargs: object) -> _Respuesta:
        self.posts.append(data)
        return _Respuesta(leer_fixture("respuesta_diaria.xml"))


def _cliente(sesion: _SesionFalsa) -> EmasesaApiClient:
    c = EmasesaApiClient(sesion, "usuario", "clave", "dispositivo")  # type: ignore[arg-type]
    c._logged_in = True
    return c


def test_la_consulta_diaria_envia_el_boton_descubierto() -> None:
    sesion = _SesionFalsa(_pagina_con(_BOTON_PRIMEFACES))

    asyncio.run(_cliente(sesion).async_get_daily_readings(date(2026, 9, 1), date(2026, 9, 7)))

    assert sesion.posts, "no se ha hecho ninguna consulta"
    payload = sesion.posts[0]
    assert payload["javax.faces.source"] == "form-date:j_idt171"
    assert payload["form-date:j_idt171"] == "form-date:j_idt171"
    assert "form-date:j_idt169" not in payload


def test_la_consulta_horaria_envia_el_boton_descubierto() -> None:
    sesion = _SesionFalsa(_pagina_con(_BOTON_PRIMEFACES))

    asyncio.run(_cliente(sesion).async_get_hourly_readings(date(2026, 9, 6)))

    assert sesion.posts[0]["javax.faces.source"] == "form-date:j_idt171"


def test_si_aun_asi_llega_la_vista_horaria_el_aviso_vuelca_el_formulario(caplog) -> None:
    """Para diagnosticar el siguiente cambio del portal sin tener que capturar la página."""
    sesion = _SesionFalsa(_pagina_con(_BOTON_PRIMEFACES))
    sesion.post = lambda url, data, **kw: (  # type: ignore[method-assign]
        sesion.posts.append(data) or _Respuesta(leer_fixture("respuesta_horaria.xml"))
    )

    with caplog.at_level(logging.WARNING):
        lecturas = asyncio.run(
            _cliente(sesion).async_get_daily_readings(date(2026, 8, 9), date(2026, 9, 7))
        )

    assert lecturas == []
    assert "Botón usado: form-date:j_idt171" in caplog.text
    assert "form-date:button_input" in caplog.text
