"""
Tests que atan el código con los ficheros de textos.

Una `translation_key` sin entrada en `strings.json` no rompe nada: la
entidad simplemente aparece en la interfaz con un nombre feo derivado del
`unique_id`, y eso no se nota hasta que alguien mira. Igual de silencioso
es que `es.json` se quede atrás respecto a `en.json` al añadir una
entidad nueva.

Se leen los ficheros y el código fuente directamente, sin importar nada
de Home Assistant.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import INTEGRACION

TEXTOS = ("strings.json", "translations/en.json", "translations/es.json")

# Cada plataforma es un módulo cuyo nombre coincide con la clave que usa
# Home Assistant dentro de `entity` en los ficheros de textos.
PLATAFORMAS = ("sensor", "button")


def _cargar(nombre: str) -> dict:
    return json.loads((INTEGRACION / nombre).read_text(encoding="utf-8"))


def _claves_en_codigo(plataforma: str) -> set[str]:
    fuente = (INTEGRACION / f"{plataforma}.py").read_text(encoding="utf-8")
    return set(re.findall(r'_attr_translation_key = "([^"]+)"', fuente))


@pytest.mark.parametrize("plataforma", PLATAFORMAS)
def test_cada_plataforma_declara_alguna_entidad(plataforma: str) -> None:
    """Si esto falla, el resto de tests de este fichero no probarían nada."""
    assert _claves_en_codigo(plataforma)


@pytest.mark.parametrize("fichero", TEXTOS)
@pytest.mark.parametrize("plataforma", PLATAFORMAS)
def test_toda_clave_del_codigo_tiene_nombre(fichero: str, plataforma: str) -> None:
    textos = _cargar(fichero).get("entity", {}).get(plataforma, {})

    for clave in _claves_en_codigo(plataforma):
        assert clave in textos, f"{clave} no está en {fichero}"
        assert textos[clave].get("name"), f"{clave} no tiene 'name' en {fichero}"


@pytest.mark.parametrize("fichero", TEXTOS)
@pytest.mark.parametrize("plataforma", PLATAFORMAS)
def test_no_sobran_textos_de_entidades_que_ya_no_existen(
    fichero: str, plataforma: str
) -> None:
    textos = _cargar(fichero).get("entity", {}).get(plataforma, {})

    assert set(textos) <= _claves_en_codigo(plataforma), f"claves de más en {fichero}"


def test_los_tres_ficheros_tienen_la_misma_estructura() -> None:
    """`es.json` no puede quedarse atrás al añadir una entidad nueva."""

    def estructura(datos: dict) -> set[str]:
        caminos: set[str] = set()

        def recorrer(nodo: object, camino: str) -> None:
            if isinstance(nodo, dict):
                for clave, valor in nodo.items():
                    recorrer(valor, f"{camino}.{clave}")
            else:
                caminos.add(camino)

        recorrer(datos, "")
        return caminos

    referencia = estructura(_cargar("strings.json"))
    for fichero in TEXTOS[1:]:
        assert estructura(_cargar(fichero)) == referencia, fichero


def test_los_identificadores_unicos_no_chocan() -> None:
    """
    Todas las entidades cuelgan del mismo `entry_id`, así que lo que las
    distingue es el sufijo: repetirlo haría que Home Assistant descartara
    una de ellas al registrarla.
    """
    sufijos: list[str] = []
    for fichero in INTEGRACION.glob("*.py"):
        fuente = fichero.read_text(encoding="utf-8")
        sufijos += re.findall(r'_attr_unique_id = f"\{entry\.entry_id\}([^"]*)"', fuente)

    assert sufijos, "no se ha encontrado ningún unique_id"
    assert len(sufijos) == len(set(sufijos)), f"sufijos repetidos: {sufijos}"


def test_el_boton_de_diagnostico_esta_registrado_como_plataforma() -> None:
    """Sin esto, el botón existe en el código pero no se crea nunca."""
    init = (INTEGRACION / "__init__.py").read_text(encoding="utf-8")

    assert "Platform.BUTTON" in init


def test_el_boton_lanza_la_misma_lectura_que_el_temporizador() -> None:
    """
    El botón debe reutilizar el refresco del coordinator, que es el mismo
    camino que dispara el temporizador, y no una consulta propia.
    """
    fuente = (INTEGRACION / "button.py").read_text(encoding="utf-8")

    assert "self.coordinator.async_refresh()" in fuente
    assert "async_get_daily_readings" not in fuente
