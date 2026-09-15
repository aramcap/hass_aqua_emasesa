"""
Tests de `_slug` (`statistics.py`).

Parece una tontería, pero es lo que decide si el `statistic_id` de la
estadística externa es válido para el recorder: si no cumple su regex,
Home Assistant rechaza la importación entera y el Panel de Energía se
queda sin la fuente de agua.
"""

from __future__ import annotations

import re

import pytest

from emasesa.const import DOMAIN
from emasesa.statistics import _slug

# Mismo criterio que `valid_statistic_id()` del recorder de Home
# Assistant: `<dominio>:<id>`, y cada mitad solo con dígitos, minúsculas
# y guiones bajos, sin empezar ni acabar en guion bajo y sin dobles.
VALID_STATISTIC_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$")


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        # El caso real: `entry_id` de Home Assistant, hexadecimal en minúsculas.
        ("01JBQ8Z3K4M5N6P7Q8R9S0T1U2", "01jbq8z3k4m5n6p7q8r9s0t1u2"),
        ("ABCDEF", "abcdef"),
        ("con-guiones", "con_guiones"),
        ("con espacios", "con_espacios"),
        ("con.puntos", "con_puntos"),
        ("acentuada_ñ", "acentuada_ñ"),  # isalnum() acepta letras unicode
        ("doble--guion", "doble_guion"),
        ("triple---guion", "triple_guion"),
        ("_extremos_", "extremos"),
        ("--extremos--", "extremos"),
    ],
)
def test_slug_casos_normales(entrada: str, esperado: str) -> None:
    assert _slug(entrada) == esperado


@pytest.mark.parametrize("entrada", ["", "---", "___", "!!!", "   "])
def test_slug_entrada_degenerada_tiene_reserva(entrada: str) -> None:
    """Sin nada aprovechable se usa 'cuenta' en vez de dejar el id vacío."""
    assert _slug(entrada) == "cuenta"


@pytest.mark.parametrize(
    "entry_id",
    [
        "01jbq8z3k4m5n6p7q8r9s0t1u2",
        "ABCDEF123456",
        "con-guiones-y--dobles",
        "_raro_",
        "!!!",
        "",
        "mezcla RARA.de_todo--junto!",
    ],
)
def test_statistic_id_resultante_lo_acepta_el_recorder(entry_id: str) -> None:
    """La propiedad que de verdad importa, sea cual sea el `entry_id`."""
    statistic_id = f"{DOMAIN}:{_slug(entry_id)}_consumo_horario"

    assert VALID_STATISTIC_ID.match(statistic_id), statistic_id
