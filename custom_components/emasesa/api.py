"""
Cliente HTTP asíncrono para la Oficina Online de EMASESA.

Es el mismo scraping que `emasesa_consumo.py` (el script standalone),
adaptado a `aiohttp` para poder usarse dentro del bucle de eventos de
Home Assistant sin bloquear. La lógica de peticiones/parseo es la MISMA,
ya verificada contra la cuenta real del usuario, con una pieza añadida:
el segundo factor (SMS) que EMASESA exige quaisquer vez que el `deviceId`
enviado en el login no es uno que el servidor ya haya marcado como
"dispositivo de confianza".

------------------------------------------------------------------------
Cómo funciona el segundo factor (verificado en vivo con DevTools)
------------------------------------------------------------------------
El propio portal usa un fichero `trusted-device-utils.js` que:
  1. En el navegador, genera (la primera vez) un UUID aleatorio y lo
     guarda en `localStorage['device_id']`.
  2. En cada login, rellena el campo oculto `formLogin:deviceId` del
     formulario con ese UUID antes de enviarlo.
  3. Si el servidor no reconoce ese UUID como de confianza, en vez de
     llevarte a `/home/` te lleva a `/twoFactor/`: una pantalla que pide
     un código enviado por SMS (o email), con una casilla "Dispositivo
     de confianza" marcada por defecto. Al verificar el código con esa
     casilla marcada, el servidor asocia ese UUID a la cuenta como
     dispositivo de confianza — logins futuros con el MISMO UUID ya no
     piden SMS.

Este cliente hace lo mismo pero con un UUID que genera y persiste el
propio Home Assistant (ver `config_flow.py`/`coordinator.py`): la
primera vez que se configura la integración, EMASESA pedirá el código
SMS una vez (el config flow tiene un segundo paso para introducirlo); a
partir de ahí, todas las actualizaciones automáticas reutilizan ese
mismo `device_id`, ya de confianza, y no vuelven a pedirlo — salvo que
EMASESA revoque la confianza en ese dispositivo por su cuenta (caducidad,
cambio de contraseña, etc.), en cuyo caso hay que volver a pasar por el
SMS una vez (ver `EmasesaTwoFactorRequired` / reauth en `config_flow.py`).

------------------------------------------------------------------------
Resto del flujo (ya documentado y verificado antes)
------------------------------------------------------------------------
  - Login por formulario (`formLogin`, campos `user`/`password`, más el
    campo oculto `hiddenDisp`, que se sigue enviando vacío — no forma
    parte del mecanismo de confianza, es `deviceId` el que importa).
  - Petición AJAX de PrimeFaces a `/consumos/miConsumo/` para consultar
    un rango de fechas.
  - El HTML de respuesta contiene `PrimeFaces.cw("Chart", ..., {data:
    [[...]], ticks:[...]})`; se extraen `data` y `ticks` con regex (no
    es JSON estricto) y se EMPAREJAN DIRECTAMENTE por índice —
    `ticks[i]` con `data[i]`, SIN invertir.

------------------------------------------------------------------------
Consumo por horas (para las estadísticas del Panel de Energía)
------------------------------------------------------------------------
La misma petición AJAX, cuando `desde` y `hasta` son EL MISMO día, hace
que EMASESA devuelva el consumo desglosado por franjas horarias en vez
de por días: mismos nombres de campo, mismo formato de respuesta, pero
con `ticks` del tipo `"00-01"`, `"01-02"`, ... `"23-00"` (24 franjas)
en vez de `"01-sep"`. Verificado en vivo inspeccionando
`PrimeFaces.widgets[...].cfg`, confirmando que la suma de las 24
franjas coincide con el total diario mostrado en pantalla.

Importante: esto sigue siendo consumo YA registrado, con el mismo
retraso de publicación que el resto de la telelectura — no hay forma de
obtener un caudal instantáneo en tiempo real desde este portal, EMASESA
no lo publica en ningún sitio. Lo que sí permite es importar el consumo
con granularidad horaria en vez de diaria (ver `statistics.py`).

Si EMASESA cambia el frontend, esto es lo primero que hay que revisar
(ver el docstring de emasesa_consumo.py para más detalle sobre cómo
recapturar la petición real desde las DevTools del navegador).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.emasesaonline.com/oficina-online/web"
LOGIN_URL = f"{BASE_URL}/login"
TWO_FACTOR_URL = f"{BASE_URL}/twoFactor/"
CONSUMO_URL = f"{BASE_URL}/consumos/miConsumo/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    "HomeAssistant-emasesa-integration"
)

FORM_LOGIN_ID = "formLogin"
FIELD_LOGIN_USER = "user"
FIELD_LOGIN_PASSWORD = "password"
FIELD_LOGIN_BTN = "formLogin:btnLogin"
FIELD_LOGIN_HIDDEN_DISP = "formLogin:hiddenDisp"
FIELD_LOGIN_DEVICE_ID = "formLogin:deviceId"

# Pantalla de verificación por SMS/email ("/twoFactor/"). Nombres de
# campo capturados inspeccionando el formulario real (ver docstring del
# módulo): `formData:j_idt152` es el código, `formData:j_idt156_input` es
# la casilla "Dispositivo de confianza" (marcada por defecto en el HTML
# real) y `formData:j_idt167` es el botón "Verificar" (envío AJAX de
# PrimeFaces, mismo patrón que "Consultar" en la consulta de consumo).
FORM_2FA_ID = "formData"
FIELD_2FA_CODE = "formData:j_idt152"
FIELD_2FA_TRUST_DEVICE = "formData:j_idt156_input"
FIELD_2FA_VERIFY_BTN = "formData:j_idt167"

FORM_DATE_ID = "form-date"
FIELD_DESDE = "form-date:button_input"
FIELD_HASTA = "form-date:button2_input"
FIELD_CONSULTAR_BTN = "form-date:j_idt169"

VIEWSTATE_FIELD = "javax.faces.ViewState"

DATE_FMT_EMASESA = "%d/%m/%Y"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=25)

# Abreviaturas de mes tal y como las devuelve EMASESA en `ticks`
# (p.ej. "M 01-sep", "L 07-sep").
_MESES_ES = {
    "ene": 1,
    "feb": 2,
    "mar": 3,
    "abr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dic": 12,
}


class EmasesaApiError(Exception):
    """Error genérico al hablar con el portal de EMASESA."""


class EmasesaAuthError(EmasesaApiError):
    """El login (o el código de verificación) ha sido rechazado."""


class EmasesaTwoFactorRequired(EmasesaAuthError):
    """
    EMASESA ha respondido con la pantalla de verificación por SMS/email:
    hace falta llamar a `async_submit_2fa_code()` con el código antes de
    poder seguir usando el cliente.
    """


def generate_device_id() -> str:
    """Genera un device_id nuevo (mismo formato que usa el JS del portal: un UUID)."""
    return str(uuid.uuid4())


def _extract_viewstate(html: str) -> str:
    m = re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]*)"', html)
    if not m:
        m = re.search(r'value="([^"]*)"[^>]*name="javax\.faces\.ViewState"', html)
    if not m:
        raise EmasesaApiError(
            "No se ha encontrado 'javax.faces.ViewState' en la página; "
            "es probable que EMASESA haya cambiado el frontend."
        )
    return m.group(1)


def _looks_like_login_page(html: str) -> bool:
    return f'id="{FIELD_LOGIN_PASSWORD}"' in html or 'name="password"' in html


def _looks_like_2fa_page(html: str) -> bool:
    return f'id="{FIELD_2FA_CODE}"' in html or f'name="{FORM_2FA_ID}"' in html


def _extract_max_fecha(html: str) -> date | None:
    """
    Última fecha que EMASESA permite consultar, según el `maxDate` del
    datepicker de la propia pantalla (p.ej. `maxDate:"07\\/09\\/2026"`).

    EMASESA no publica la lectura de "hoy" ni la de "ayer" de forma
    fiable (el retraso varía, no siempre es de un día como cabría
    esperar); pedir un `hasta` posterior a este límite hace que el
    formulario lo rechace (campo marcado `aria-invalid`) y la respuesta
    AJAX no incluya el gráfico. Se lee este límite de la propia página
    en vez de asumir un retraso fijo en días.
    """
    m = re.search(r'maxDate:"(\d{2})\\/(\d{2})\\/(\d{4})"', html)
    if not m:
        return None
    dia, mes, anio = (int(g) for g in m.groups())
    try:
        return date(anio, mes, dia)
    except ValueError:
        return None


def _parse_chart_response(xml_text: str) -> list[tuple[str, float]]:
    """Devuelve [(etiqueta, litros), ...] emparejados directamente por índice."""
    data_match = re.search(r"data:(\[\[[\d\s,.\-]*\]\])", xml_text)
    ticks_match = re.search(r"ticks:(\[[^\]]*\])", xml_text)

    if not data_match:
        if _looks_like_login_page(xml_text):
            raise EmasesaAuthError(
                "La respuesta a la consulta de consumo es la página de login: "
                "la sesión no está autenticada."
            )
        if _looks_like_2fa_page(xml_text):
            raise EmasesaTwoFactorRequired(
                "EMASESA ha vuelto a pedir verificación por SMS a mitad de sesión "
                "(¿se ha revocado la confianza en este dispositivo?)."
            )
        # No hay forma de saber desde aquí si es un rango genuinamente sin
        # datos o si EMASESA ha cambiado el formato de la respuesta; se
        # deja un fragmento en el log de depuración para poder
        # diagnosticarlo sin tener que reproducir el problema en vivo.
        _LOGGER.debug(
            "Respuesta de consulta de consumo sin array 'data' reconocible "
            "(primeros 4000 caracteres): %s",
            xml_text[:4000],
        )
        raise EmasesaApiError(
            "No se ha encontrado el array 'data' del gráfico en la respuesta "
            "(¿rango sin datos, o formato cambiado?)."
        )

    valores = json.loads(data_match.group(1))[0]

    if ticks_match:
        ticks_raw = ticks_match.group(1).replace("\\-", "-")
        etiquetas = json.loads(ticks_raw)
    else:
        etiquetas = [str(i) for i in range(len(valores))]

    n = min(len(etiquetas), len(valores))
    return [(etiquetas[i], valores[i]) for i in range(n)]


def _etiqueta_a_fecha(etiqueta: str, hasta_ref: date) -> date | None:
    """
    Convierte una etiqueta tipo "M 01-sep" en un `date`, asumiendo que
    pertenece al mismo año que `hasta_ref` salvo que el mes sea posterior
    al de `hasta_ref` (entonces la ventana ha cruzado fin de año y la
    etiqueta es del año anterior).

    Devuelve None si la etiqueta no tiene el formato de vista diaria
    (p.ej. si por lo que sea llega una vista horaria tipo "13-14").
    """
    partes = etiqueta.strip().split(" ")
    dia_mes = partes[-1]  # "01-sep"
    m = re.match(r"^(\d{1,2})-([a-zA-Z]{3})$", dia_mes)
    if not m:
        return None
    dia = int(m.group(1))
    mes = _MESES_ES.get(m.group(2).lower())
    if not mes:
        return None
    anio = hasta_ref.year
    if mes > hasta_ref.month:
        anio -= 1
    try:
        return date(anio, mes, dia)
    except ValueError:
        return None


_HORA_RANGO_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")


def _etiqueta_a_hora(etiqueta: str) -> int | None:
    """
    Convierte una etiqueta de franja horaria tipo "13-14" o "23-00" en
    la hora de INICIO de esa franja (13, 23, ...). Devuelve None si la
    etiqueta no tiene ese formato (p.ej. si llega una vista diaria tipo
    "01-sep" en vez de horaria — pasa si se pide un rango de más de un
    día).
    """
    m = _HORA_RANGO_RE.match(etiqueta.strip())
    if not m:
        return None
    inicio = int(m.group(1))
    if not (0 <= inicio <= 23):
        return None
    return inicio


class EmasesaApiClient:
    """
    Sesión autenticada contra la Oficina Online de EMASESA.

    `device_id` debe ser un identificador ESTABLE entre ejecuciones
    (generar uno con `generate_device_id()` una única vez y persistirlo,
    p.ej. en el config entry de Home Assistant): es lo que le permite a
    EMASESA recordar que este "dispositivo" ya pasó por la verificación
    SMS y no volver a pedirla.
    """

    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str, device_id: str
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._device_id = device_id
        self._logged_in = False
        # ViewState de la pantalla /twoFactor/ pendiente de verificar,
        # guardado por async_login() cuando detecta el reto de SMS, para
        # que async_submit_2fa_code() no tenga que volver a pedir la
        # página (evita una petición y un posible cambio de ViewState).
        self._pending_2fa_viewstate: str | None = None

    async def async_login(self) -> None:
        """
        Inicia sesión. Si EMASESA responde con la pantalla de
        verificación SMS, lanza `EmasesaTwoFactorRequired` (no es un
        error fatal: hay que llamar a `async_submit_2fa_code()` a
        continuación, reutilizando este mismo cliente/sesión).
        """
        _LOGGER.debug("Obteniendo formulario de login de EMASESA")
        async with self._session.get(
            LOGIN_URL, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            html = await resp.text()
        viewstate = _extract_viewstate(html)

        payload = {
            FORM_LOGIN_ID: FORM_LOGIN_ID,
            FIELD_LOGIN_USER: self._username,
            FIELD_LOGIN_PASSWORD: self._password,
            FIELD_LOGIN_BTN: "",
            FIELD_LOGIN_HIDDEN_DISP: "",
            FIELD_LOGIN_DEVICE_ID: self._device_id,
            VIEWSTATE_FIELD: viewstate,
        }
        _LOGGER.debug("Enviando credenciales a EMASESA (device_id conocido)")
        async with self._session.post(
            LOGIN_URL,
            data=payload,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            html2 = await resp.text()

        if _looks_like_2fa_page(html2):
            _LOGGER.debug(
                "EMASESA pide verificación por SMS/email para este device_id"
            )
            self._pending_2fa_viewstate = _extract_viewstate(html2)
            raise EmasesaTwoFactorRequired(
                "EMASESA requiere un código de verificación (SMS/email) para "
                "este dispositivo."
            )

        if _looks_like_login_page(html2):
            self._logged_in = False
            raise EmasesaAuthError(
                "Login rechazado por EMASESA (usuario/contraseña incorrectos, "
                "o el formulario ha cambiado)."
            )
        self._logged_in = True
        _LOGGER.debug("Sesión de EMASESA iniciada correctamente")

    async def async_submit_2fa_code(self, code: str, trust_device: bool = True) -> None:
        """
        Envía el código recibido por SMS/email tras un `async_login()`
        que haya lanzado `EmasesaTwoFactorRequired`. Debe llamarse sobre
        el MISMO cliente (misma sesión/cookies) que hizo el login.
        """
        if self._pending_2fa_viewstate is None:
            raise EmasesaApiError(
                "async_submit_2fa_code() llamado sin un login pendiente de "
                "verificación (llama primero a async_login())."
            )

        payload = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": FIELD_2FA_VERIFY_BTN,
            "javax.faces.partial.execute": "@all",
            "javax.faces.partial.render": FORM_2FA_ID,
            FIELD_2FA_VERIFY_BTN: FIELD_2FA_VERIFY_BTN,
            FORM_2FA_ID: FORM_2FA_ID,
            FIELD_2FA_CODE: code.strip(),
            VIEWSTATE_FIELD: self._pending_2fa_viewstate,
        }
        if trust_device:
            # Casilla "Dispositivo de confianza": un checkbox HTML
            # marcado se envía como "on" cuando no se indica un `value`
            # explícito, que es el caso aquí (igual que hace el navegador).
            payload[FIELD_2FA_TRUST_DEVICE] = "on"

        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Faces-Request": "partial/ajax",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/xml, text/xml, */*; q=0.01",
        }
        _LOGGER.debug("Enviando código de verificación a EMASESA")
        async with self._session.post(
            TWO_FACTOR_URL, data=payload, headers=headers, timeout=REQUEST_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            html = await resp.text()

        self._pending_2fa_viewstate = None

        if _looks_like_2fa_page(html):
            # Sigue en la pantalla de verificación: código incorrecto (o
            # caducado). Refresca el ViewState por si el usuario quiere
            # reintentarlo desde el mismo flujo.
            try:
                self._pending_2fa_viewstate = _extract_viewstate(html)
            except EmasesaApiError:
                pass
            raise EmasesaAuthError(
                "Código de verificación incorrecto o caducado."
            )

        if _looks_like_login_page(html):
            self._logged_in = False
            raise EmasesaAuthError(
                "EMASESA ha devuelto a la pantalla de login tras el código de "
                "verificación; inténtalo de nuevo."
            )

        self._logged_in = True
        _LOGGER.debug(
            "Código de verificación aceptado; dispositivo marcado como de "
            "confianza=%s",
            trust_device,
        )

    async def _ensure_login(self) -> None:
        if not self._logged_in:
            await self.async_login()

    async def _get_consumo_page(self) -> str:
        """GET de la pantalla de consumo, reautenticando si hace falta."""
        async with self._session.get(
            CONSUMO_URL, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            html = await resp.text()
        if _looks_like_login_page(html) or _looks_like_2fa_page(html):
            _LOGGER.debug("Sesión de EMASESA caducada, reintentando login")
            self._logged_in = False
            await self.async_login()
            async with self._session.get(
                CONSUMO_URL, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        return html

    async def _post_consulta(self, desde_str: str, hasta_str: str, viewstate: str) -> str:
        """POST AJAX compartido por la consulta diaria y la horaria."""
        _LOGGER.debug("Consultando consumo EMASESA %s -> %s", desde_str, hasta_str)
        payload = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": FIELD_CONSULTAR_BTN,
            "javax.faces.partial.execute": "@all",
            "javax.faces.partial.render": f"{FORM_DATE_ID} {FORM_DATE_ID}:graficoConsumos",
            FIELD_CONSULTAR_BTN: FIELD_CONSULTAR_BTN,
            FORM_DATE_ID: FORM_DATE_ID,
            FIELD_DESDE: desde_str,
            FIELD_HASTA: hasta_str,
            VIEWSTATE_FIELD: viewstate,
        }
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Faces-Request": "partial/ajax",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/xml, text/xml, */*; q=0.01",
        }
        async with self._session.post(
            CONSUMO_URL, data=payload, headers=headers, timeout=REQUEST_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def async_get_daily_readings(self, desde: date, hasta: date) -> list[dict]:
        """
        Devuelve [{"date": date(...), "litros": float}, ...] ordenado
        ascendente por fecha, para el rango [desde, hasta] (ambos
        inclusive). `desde` debe ser distinto de `hasta` para obtener
        granularidad diaria (si son iguales, EMASESA devuelve el
        desglose horario de ese día — usa `async_get_hourly_readings`
        para eso; esta función descarta las etiquetas horarias si
        llegan).

        Puede lanzar `EmasesaTwoFactorRequired` si, a mitad de sesión,
        EMASESA decide revocar la confianza en este `device_id` y volver
        a pedir SMS (no se puede completar sin intervención humana; el
        coordinator lo convierte en un reauth de Home Assistant).
        """
        await self._ensure_login()
        html = await self._get_consumo_page()
        viewstate = _extract_viewstate(html)

        max_fecha = _extract_max_fecha(html)
        if max_fecha is not None and hasta > max_fecha:
            _LOGGER.debug(
                "El 'hasta' solicitado (%s) supera el máximo que EMASESA permite "
                "consultar ahora mismo (%s); se ajusta a ese límite.",
                hasta,
                max_fecha,
            )
            hasta = max_fecha
            if desde > hasta:
                desde = hasta

        text = await self._post_consulta(
            desde.strftime(DATE_FMT_EMASESA), hasta.strftime(DATE_FMT_EMASESA), viewstate
        )
        pares = _parse_chart_response(text)

        resultado: list[dict] = []
        for etiqueta, litros in pares:
            fecha = _etiqueta_a_fecha(etiqueta, hasta)
            if fecha is None:
                _LOGGER.debug(
                    "Etiqueta '%s' no se ha podido interpretar como fecha diaria; "
                    "se descarta (¿rango de 1 día devolviendo vista horaria?)",
                    etiqueta,
                )
                continue
            resultado.append({"date": fecha, "litros": float(litros)})

        resultado.sort(key=lambda r: r["date"])
        return resultado

    async def async_get_hourly_readings(self, dia: date) -> list[dict]:
        """
        Devuelve el consumo por horas de UN día concreto:
        [{"hora_inicio": 0, "litros": float}, ...] para las franjas
        horarias del día con dato disponible, ordenado ascendente. Se
        usa para importar estadísticas externas con granularidad
        horaria (ver `statistics.py`) — sigue siendo consumo YA
        registrado, sujeto al mismo retraso de publicación que el resto
        de la telelectura, y se recorta al `maxDate` vigente igual que
        `async_get_daily_readings`.
        """
        await self._ensure_login()
        html = await self._get_consumo_page()
        viewstate = _extract_viewstate(html)

        max_fecha = _extract_max_fecha(html)
        if max_fecha is not None and dia > max_fecha:
            # A diferencia de `async_get_daily_readings` (que recorta un
            # RANGO), aquí `dia` es un único día concreto que el llamante
            # espera que se corresponda con los datos devueltos: si
            # EMASESA aún no lo permite consultar, se devuelve una lista
            # vacía en vez de sustituirlo por otro día sin que el
            # llamante se entere.
            _LOGGER.debug(
                "El día solicitado (%s) supera el máximo que EMASESA permite "
                "consultar ahora mismo (%s); no hay datos horarios todavía.",
                dia,
                max_fecha,
            )
            return []

        dia_str = dia.strftime(DATE_FMT_EMASESA)
        text = await self._post_consulta(dia_str, dia_str, viewstate)
        pares = _parse_chart_response(text)

        resultado: list[dict] = []
        for etiqueta, litros in pares:
            hora = _etiqueta_a_hora(etiqueta)
            if hora is None:
                _LOGGER.debug(
                    "Etiqueta '%s' no se ha podido interpretar como franja horaria; "
                    "se descarta",
                    etiqueta,
                )
                continue
            resultado.append({"hora_inicio": hora, "litros": float(litros)})

        resultado.sort(key=lambda r: r["hora_inicio"])
        return resultado
