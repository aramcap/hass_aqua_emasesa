"""
Config flow para la integración EMASESA.

El alta tiene dos pasos porque EMASESA exige un código de verificación
(SMS/email) la primera vez que ve el `device_id` que genera esta
integración (ver el docstring de `api.py` para el detalle del mecanismo
de "dispositivo de confianza"):

  1. `async_step_user`: pide usuario/contraseña, genera un `device_id`
     nuevo y lanza `async_login()`. Si EMASESA responde directamente
     (poco probable la primera vez, pero posible si el usuario reusa un
     `device_id` que ya conocía), se crea la entrada. Si EMASESA pide
     verificación, se pasa al paso 2.
  2. `async_step_2fa`: pide el código recibido, lo envía con la casilla
     "dispositivo de confianza" activada, y crea la entrada guardando
     usuario/contraseña/device_id. A partir de aquí, el coordinator
     reutiliza siempre ese mismo device_id y no debería volver a
     necesitar el paso 2 — salvo que EMASESA revoque la confianza en él,
     en cuyo caso Home Assistant lanza un reauth (`async_step_reauth`)
     que repite exactamente este mismo baile de dos pasos.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import (
    EmasesaApiClient,
    EmasesaApiError,
    EmasesaAuthError,
    EmasesaTwoFactorRequired,
    generate_device_id,
)
from .const import (
    CARGA_MASIVA_DIAS_DEFECTO,
    CARGA_MASIVA_MAX_DIAS,
    CARGA_MASIVA_MIN_DIAS,
    CONF_CARGA_MASIVA_DIAS,
    CONF_DEVICE_ID,
    CONF_NAME_DEFAULT,
    CONF_SCAN_INTERVAL_MINUTES,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)


class EmasesaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Alta de una cuenta de EMASESA (con verificación SMS/email)."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._name: str = CONF_NAME_DEFAULT
        self._device_id: str | None = None
        self._session = None
        self._api: EmasesaApiClient | None = None
        # Solo relevante durante un reauth: entrada ya existente que se
        # está reautenticando (en vez de crear una nueva).
        self._reauth_entry: config_entries.ConfigEntry | None = None

    # -- alta normal -------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD]
            self._name = user_input.get(CONF_NAME) or CONF_NAME_DEFAULT

            await self.async_set_unique_id(self._username)
            self._abort_if_unique_id_configured()

            errors = await self._async_try_login()
            if errors:
                return self.async_show_form(
                    step_id="user", data_schema=self._user_schema(), errors=errors
                )

            if self._api is not None and getattr(self._api, "_logged_in", False):
                # No hacía falta verificación (device_id ya de confianza,
                # o EMASESA no la exige para esta cuenta): alta directa.
                return await self._async_finish()

            return await self.async_step_2fa()

        return self.async_show_form(step_id="user", data_schema=self._user_schema(), errors=errors)

    def _user_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=self._username or vol.UNDEFINED): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_NAME, default=self._name): str,
            }
        )

    async def _async_try_login(self) -> dict[str, str]:
        """
        Intenta el login con un device_id nuevo. Devuelve un dict de
        errores (vacío si ha ido bien o si lo que toca es pasar al paso
        de verificación SMS, que se señaliza dejando `self._api` con una
        verificación pendiente en vez de con un error).
        """
        self._device_id = generate_device_id()
        if self._session is None:
            # `async_create_clientsession` registra su propio cierre (al
            # terminar el flujo/HA se apaga); no la cerramos a mano, así
            # que la reutilizamos entre reintentos en vez de crear una
            # sesión nueva cada vez que el usuario falla el login.
            self._session = async_create_clientsession(self.hass)
        self._api = EmasesaApiClient(
            self._session, self._username, self._password, self._device_id
        )
        try:
            await self._api.async_login()
        except EmasesaTwoFactorRequired:
            return {}
        except EmasesaAuthError:
            return {"base": "invalid_auth"}
        except EmasesaApiError:
            return {"base": "cannot_connect"}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error inesperado validando credenciales de EMASESA")
            return {"base": "unknown"}
        return {}

    # -- verificación SMS/email --------------------------------------------

    async def async_step_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            assert self._api is not None
            try:
                await self._api.async_submit_2fa_code(user_input["code"], trust_device=True)
            except EmasesaAuthError:
                errors["base"] = "invalid_2fa_code"
            except EmasesaApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error inesperado verificando el código de EMASESA")
                errors["base"] = "unknown"

            if not errors:
                return await self._async_finish()

        schema = vol.Schema({vol.Required("code"): str})
        return self.async_show_form(step_id="2fa", data_schema=schema, errors=errors)

    async def _async_finish(self) -> config_entries.FlowResult:
        data = {
            CONF_USERNAME: self._username,
            CONF_PASSWORD: self._password,
            CONF_DEVICE_ID: self._device_id,
        }
        if self._reauth_entry is not None:
            self.hass.config_entries.async_update_entry(self._reauth_entry, data=data)
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")
        return self.async_create_entry(title=self._name, data=data)

    # -- reauth (el device_id ha dejado de ser de confianza) ---------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.FlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        self._username = entry_data.get(CONF_USERNAME)
        self._password = entry_data.get(CONF_PASSWORD)
        self._name = self._reauth_entry.title if self._reauth_entry else CONF_NAME_DEFAULT
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            errors = await self._async_try_login()
            if errors:
                return self.async_show_form(
                    step_id="reauth_confirm",
                    data_schema=self._reauth_schema(),
                    errors=errors,
                )
            if self._api is not None and getattr(self._api, "_logged_in", False):
                return await self._async_finish()
            return await self.async_step_2fa()

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=self._reauth_schema(), errors=errors
        )

    def _reauth_schema(self) -> vol.Schema:
        return vol.Schema({vol.Required(CONF_PASSWORD): str})

    # -- opciones ------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "EmasesaOptionsFlow":
        return EmasesaOptionsFlow()


class EmasesaOptionsFlow(config_entries.OptionsFlow):
    """Permite ajustar la frecuencia de consulta y lanzar una carga masiva manual."""

    # No guardamos `config_entry` a mano: desde que Home Assistant lo
    # deprecó, asignarlo en __init__ termina lanzando un error (500 al
    # abrir "Configurar"). La clase base ya expone `self.config_entry`
    # resuelto a partir del flujo en curso.

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        return self.async_show_menu(step_id="init", menu_options=["ajustes", "carga_masiva"])

    # -- ajustes (lo que antes vivía en "init") -----------------------------

    async def async_step_ajustes(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES
        )
        schema = vol.Schema(
            {
                vol.Optional(CONF_SCAN_INTERVAL_MINUTES, default=current): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_SCAN_INTERVAL_MINUTES, max=MAX_SCAN_INTERVAL_MINUTES),
                )
            }
        )
        return self.async_show_form(step_id="ajustes", data_schema=schema)

    # -- carga masiva manual -------------------------------------------------

    async def async_step_carga_masiva(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """
        Vuelve a pedir a EMASESA (y a reescribir la estadística horaria
        de) los últimos N días, con N ajustable — pensado para rellenar
        el histórico si algo falló en una carga anterior. No cambia
        ninguna opción persistida: es una acción puntual, no una
        configuración, así que al terminar se cierra con `async_abort`
        en vez de `async_create_entry`.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            coordinator = self.hass.data[DOMAIN][self.config_entry.entry_id]["coordinator"]
            try:
                await coordinator.async_trigger_carga_masiva(user_input[CONF_CARGA_MASIVA_DIAS])
            except EmasesaTwoFactorRequired:
                errors["base"] = "reauth_required"
            except EmasesaAuthError:
                errors["base"] = "invalid_auth"
            except EmasesaApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error inesperado en la carga masiva manual de EMASESA")
                errors["base"] = "unknown"
            else:
                return self.async_abort(reason="carga_masiva_completada")

        schema = vol.Schema(
            {
                vol.Optional(CONF_CARGA_MASIVA_DIAS, default=CARGA_MASIVA_DIAS_DEFECTO): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=CARGA_MASIVA_MIN_DIAS, max=CARGA_MASIVA_MAX_DIAS),
                )
            }
        )
        return self.async_show_form(step_id="carga_masiva", data_schema=schema, errors=errors)
