"""
Configuración común de los tests.

Estos tests son deliberadamente PUROS: prueban los helpers de parseo
(`api.py`) y de saneado de identificadores (`statistics.py`) sin
arrancar Home Assistant ni tocar la red, así que corren en milisegundos
y solo necesitan `pytest` y `aiohttp`.

Para conseguirlo hacen falta dos apaños, los dos acotados a este fichero:

1. Importar `custom_components.emasesa.api` ejecutaría el `__init__.py`
   de la integración, que importa Home Assistant entero. En su lugar se
   registra un paquete sintético `emasesa` que apunta al directorio de
   la integración, SIN ejecutar su `__init__.py`; los imports relativos
   de dentro (`from .api import ...`) siguen resolviendo bien. Los tests
   importan de ahí: `from emasesa.api import ...`.

2. `statistics.py` y `coordinator.py` importan Home Assistant en su
   cabecera. Se registran unos módulos `homeassistant.*` mínimos para
   que el import no falle; lo que se prueba de esos módulos sigue
   siendo lógica propia sobre diccionarios y fechas, no comportamiento
   de Home Assistant.
   Los stubs imitan una versión ACTUAL de Home Assistant (con
   `StatisticMeanType` y con `unit_class` en `StatisticMetaData`), que
   es contra lo que `test_statistics_metadata.py` comprueba qué campos
   se envían al recorder; ese test simula también una versión antigua
   recargando el módulo con los stubs recortados.

Todo lo que dependa DE VERDAD del comportamiento de Home Assistant
(entidades, coordinator, recorder) va en una suite aparte con
`pytest-homeassistant-custom-component`, que importa Home Assistant de
verdad: estos stubs no valen para eso y no deben crecer para intentarlo.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
INTEGRACION = RAIZ / "custom_components" / "emasesa"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _paquete(nombre: str, ruta: Path | None = None, **atributos: object) -> types.ModuleType:
    """Registra un módulo sintético en `sys.modules` (paquete si lleva ruta)."""
    mod = types.ModuleType(nombre)
    if ruta is not None:
        mod.__path__ = [str(ruta)]  # type: ignore[attr-defined]
    for clave, valor in atributos.items():
        setattr(mod, clave, valor)
    sys.modules[nombre] = mod
    return mod


def _stub_homeassistant() -> None:
    """Módulos `homeassistant.*` mínimos para poder importar `statistics.py`."""
    if "homeassistant" in sys.modules:
        return  # Home Assistant de verdad está instalado: no lo tapamos.

    class _Marcador:
        """Sustituto de cualquier símbolo de Home Assistant que no se usa aquí."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _UnitOfVolume:
        LITERS = "L"

    class _StatisticMeanType(IntEnum):
        NONE = 0
        ARITHMETIC = 1
        CIRCULAR = 2

    class _StatisticMetaData(dict):
        """
        Sustituto de la TypedDict real. Lo único que la integración mira
        de ella son sus anotaciones, para saber si esta versión de Home
        Assistant admite `unit_class`.
        """

        __annotations__ = {
            "mean_type": "StatisticMeanType",
            "has_sum": "bool",
            "name": "str | None",
            "source": "str",
            "statistic_id": "str",
            "unit_class": "str | None",
            "unit_of_measurement": "str | None",
        }

    class _VolumeConverter:
        UNIT_CLASS = "volume"

    class _Generico:
        """Base sustituta que admite `Clase[tipo]` al heredar de ella."""

        def __class_getitem__(cls, item: object) -> type:
            return cls

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _UpdateFailed(Exception):
        pass

    class _ConfigEntryAuthFailed(Exception):
        pass

    class _DtUtil:
        """Lo único que se usa de `homeassistant.util.dt` en el módulo."""

        UTC = timezone.utc

        @staticmethod
        def utc_from_timestamp(timestamp: float) -> datetime:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)

        @staticmethod
        def utcnow() -> datetime:
            return datetime.now(tz=timezone.utc)

    # Los intermedios necesitan `__path__` para que `from x.y import z`
    # los trate como paquetes; se les da una ruta que no existe, porque
    # no se va a cargar nada real desde ellos.
    inexistente = RAIZ / "tests" / "_no_existe"
    _paquete("homeassistant", inexistente)
    _paquete("homeassistant.components", inexistente)
    _paquete("homeassistant.components.recorder", inexistente, get_instance=_Marcador)
    _paquete("homeassistant.const", UnitOfVolume=_UnitOfVolume)
    _paquete("homeassistant.core", HomeAssistant=_Marcador)
    _paquete(
        "homeassistant.components.recorder.models",
        StatisticData=dict,
        StatisticMetaData=_StatisticMetaData,
        StatisticMeanType=_StatisticMeanType,
    )
    _paquete("homeassistant.util", inexistente, dt=_DtUtil)
    sys.modules["homeassistant.util.dt"] = _DtUtil  # type: ignore[assignment]
    _paquete("homeassistant.util.unit_conversion", VolumeConverter=_VolumeConverter)
    _paquete(
        "homeassistant.components.recorder.statistics",
        async_add_external_statistics=_Marcador,
        get_last_statistics=_Marcador,
    )
    # Lo que necesita `coordinator.py` por encima de lo anterior.
    _paquete("homeassistant.config_entries", ConfigEntry=_Marcador)
    _paquete("homeassistant.exceptions", ConfigEntryAuthFailed=_ConfigEntryAuthFailed)
    _paquete("homeassistant.helpers", inexistente)
    _paquete("homeassistant.helpers.storage", Store=_Marcador)
    _paquete(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_Generico,
        UpdateFailed=_UpdateFailed,
    )


def _registrar_integracion() -> None:
    """Hace importable la integración como `emasesa.<módulo>`."""
    if "emasesa" not in sys.modules:
        _paquete("emasesa", INTEGRACION)


_stub_homeassistant()
_registrar_integracion()


def leer_fixture(nombre: str) -> str:
    """Devuelve el contenido de `tests/fixtures/<nombre>`."""
    return (FIXTURES / nombre).read_text(encoding="utf-8")
