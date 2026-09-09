# EMASESA - Consumo de agua (Home Assistant)

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/aramcap/hass_aqua_emasesa.svg)](https://github.com/aramcap/hass_aqua_emasesa/releases)
[![License](https://img.shields.io/github/license/aramcap/hass_aqua_emasesa.svg)](LICENSE)

Integración personalizada (no oficial) para leer el consumo de agua de [EMASESA](https://www.emasesaonline.com) (Sevilla) desde Home Assistant, usando tu cuenta de la Oficina Online (telelectura).

**No es una integración oficial de EMASESA.** EMASESA no publica una API pública: esto reproduce, mediante peticiones HTTP normales, lo mismo que hace tu navegador al entrar en *Mis consumos > Mi consumo (Telelectura)*. Si EMASESA cambia el frontend de su portal, la integración puede dejar de funcionar hasta que se actualice - ver [Cómo funciona / mantenimiento](#cómo-funciona--mantenimiento).

## 📋 Tabla de contenidos

- [EMASESA - Consumo de agua (Home Assistant)](#emasesa---consumo-de-agua-home-assistant)
  - [📋 Tabla de contenidos](#-tabla-de-contenidos)
  - [Qué aporta](#qué-aporta)
  - [Requisitos](#requisitos)
  - [Instalación](#instalación)
    - [HACS (recomendado)](#hacs-recomendado)
    - [Instalación manual](#instalación-manual)
  - [Configuración](#configuración)
    - [Opciones de configuración](#opciones-de-configuración)
  - [Verificación en dos pasos (SMS/email) al dar de alta la integración](#verificación-en-dos-pasos-smsemail-al-dar-de-alta-la-integración)
  - [Opciones](#opciones)
  - [Añadirlo al Panel de Energía](#añadirlo-al-panel-de-energía)
  - [Cómo funciona / mantenimiento](#cómo-funciona--mantenimiento)
  - [Solución de problemas](#solución-de-problemas)
    - [Pide reautenticación constantemente](#pide-reautenticación-constantemente)
    - [No llega el código SMS/email](#no-llega-el-código-smsemail)
    - [Activa el registro de depuración](#activa-el-registro-de-depuración)
    - [Lista de comprobación de instalación](#lista-de-comprobación-de-instalación)
  - [Aviso](#aviso)
  - [Licencia](#licencia)
  - [Soporte](#soporte)

## Qué aporta

Por cada cuenta configurada, crea un dispositivo EMASESA con dos sensores:

- ✅ **Último día** (`sensor.<nombre>_ultimo_dia`): litros del último día con lectura disponible (normalmente ayer; EMASESA suele tardar un día en publicar la lectura). Incluye como atributo el histórico de los últimos días consultados (`historico_reciente`).
- ✅ **Consumo acumulado** (`sensor.<nombre>_consumo_acumulado`): contador que solo crece (litros), pensado para añadirlo como fuente de **Agua** en el **Panel de Energía** de Home Assistant. Como EMASESA no ofrece una lectura de contador continua (solo consumos por día), la propia integración va sumando cada día nuevo una única vez y guarda ese total en disco para no perderlo si reinicias Home Assistant.
- ✅ Configuración por UI (Config Flow), incluida la verificación por SMS/email
- ✅ Reautenticación guiada desde la propia integración si EMASESA deja de confiar en el dispositivo
- ✅ Intervalo de actualización configurable

## Requisitos

- Home Assistant 2024.1.0 o superior
- Una cuenta activa en la Oficina Online de EMASESA (usuario DNI/NIE y contraseña) con telelectura disponible

## Instalación

### HACS (recomendado)

Esta integración no está en el listado por defecto de HACS (no ha pasado por el proceso de publicación/revisión), así que se instala como **repositorio personalizado**:

1. Abre HACS en Home Assistant
2. Pulsa en "Integraciones"
3. Pulsa el menú de los tres puntos (⋮) en la esquina superior derecha
4. Selecciona "Repositorios personalizados"
5. Añade la URL de este repositorio: `https://github.com/aramcap/hass_aqua_emasesa`
6. Selecciona la categoría: "Integración"
7. Pulsa "Añadir"
8. Busca "EMASESA"
9. Pulsa "Descargar"
10. Reinicia Home Assistant

### Instalación manual

1. Copia la carpeta `custom_components/emasesa/` dentro de la carpeta `custom_components/` de tu instalación de Home Assistant
2. Reinicia Home Assistant

La estructura de carpetas debería quedar así:

```
config/
└── custom_components/
    └── emasesa/
        ├── __init__.py
        ├── api.py
        ├── config_flow.py
        ├── const.py
        ├── coordinator.py
        ├── manifest.json
        ├── sensor.py
        ├── strings.json
        └── translations/
            ├── en.json
            └── es.json
```

## Configuración

1. Ve a **Ajustes** → **Dispositivos y servicios**
2. Pulsa **+ Añadir integración**
3. Busca "EMASESA"
4. Introduce tu usuario (DNI/NIE) y contraseña de la Oficina Online
5. Si es la primera vez que esta integración se conecta a tu cuenta, verás un segundo paso pidiendo un **código de verificación** (ver [siguiente sección](#verificación-en-dos-pasos-smsemail-al-dar-de-alta-la-integración)) - es normal, solo ocurre una vez
6. Pulsa **Enviar**

### Opciones de configuración

| Opción | Descripción | Obligatorio |
|--------|-------------|--------------|
| Usuario | DNI/NIE de tu cuenta de la Oficina Online | Sí |
| Contraseña | Contraseña de tu cuenta de la Oficina Online | Sí |
| Nombre | Nombre personalizado para el dispositivo | No (por defecto: "EMASESA") |
| Intervalo de actualización | Cada cuántos minutos se consulta EMASESA | No (por defecto: 360 min / 6 h) |

## Verificación en dos pasos (SMS/email) al dar de alta la integración

La Oficina Online de EMASESA protege el acceso desde un "dispositivo" nuevo con un segundo factor: la primera vez que detecta un identificador de dispositivo que no conoce, en vez de dejarte entrar directamente te pide un código enviado por SMS (o email) y, si lo confirmas con la casilla "Dispositivo de confianza" marcada, recuerda ese dispositivo para no volver a pedírtelo.

Esta integración se comporta como un dispositivo más ante EMASESA:

1. Al añadirla, genera un identificador propio (un UUID) y lo guarda junto con tus credenciales dentro de la propia entrada de configuración de Home Assistant - no se comparte con el script standalone ni con tu navegador.
2. Si EMASESA no reconoce ese identificador (siempre la primera vez), el asistente de configuración muestra un paso adicional pidiendo el **código que te llega por SMS/email**. Al introducirlo correctamente, EMASESA marca ese identificador como dispositivo de confianza.
3. A partir de ahí, todas las actualizaciones automáticas (cada 6 horas, por defecto) reutilizan siempre ese mismo identificador y EMASESA no vuelve a pedir código - de la misma forma que tu navegador no te lo vuelve a pedir tras marcar "recordar este dispositivo".

**Si en algún momento EMASESA deja de confiar en ese identificador** (por ejemplo, si cambias la contraseña de tu cuenta de EMASESA, o si EMASESA revoca la confianza por su cuenta pasado un tiempo), la próxima actualización automática fallará de forma controlada: Home Assistant mostrará un aviso de **reautenticación** en la propia integración ("Reconectar con EMASESA"). Al pulsarlo, solo hace falta volver a introducir la contraseña y, si EMASESA vuelve a pedir código, repetir el mismo paso de verificación - no hace falta borrar ni volver a añadir la integración desde cero, y los sensores/histórico existentes no se pierden.

> **Nota:** el identificador de dispositivo guardado en la entrada de configuración es lo único que mantiene la "confianza" del lado de EMASESA. No hay manera de transferirlo entre instalaciones de Home Assistant ni de restaurarlo desde una copia antigua sin que EMASESA vuelva a pedir el código - si restauras un backup, es normal tener que volver a introducir un código SMS una vez.

## Opciones

Desde la propia integración (**Configurar**) puedes cambiar el intervalo de actualización (por defecto, 6 horas, con un rango permitido de 15 minutos a 24 horas). No merece la pena bajarlo mucho: EMASESA solo publica lecturas nuevas una vez al día, así que consultar con más frecuencia solo añade peticiones innecesarias.

## Añadirlo al Panel de Energía

1. Ve a **Ajustes** → **Paneles** → **Energía**
2. Pulsa **Añadir fuente de agua**
3. Selecciona el sensor `Consumo acumulado`

## Cómo funciona / mantenimiento

Todo el detalle de las peticiones HTTP (login por formulario JSF, la verificación en dos pasos por SMS/email, la petición AJAX de PrimeFaces al pulsar "Consultar", y cómo se extraen los valores de la respuesta) está documentado en los comentarios de `custom_components/emasesa/api.py`. Es el mismo mecanismo, verificado contra una cuenta real, que el script standalone `emasesa_consumo.py` del que nace este proyecto (ese script no gestiona el SMS: solo esta integración lo hace).

Puntos frágiles a vigilar si deja de funcionar:

- Cambios en los `id`/`name` de los campos de los formularios de login, de la pantalla de verificación SMS/email, o de la consulta de fechas.
- Cambios en el formato de la respuesta del gráfico (`PrimeFaces.cw("Chart", ...)`), que es como se extraen los litros por día.
- El campo oculto `hiddenDisp` del login, que este proyecto sigue enviando vacío porque así llega en el HTML público del formulario (no forma parte del mecanismo de confianza: es `deviceId` el que importa, y ese sí se envía con un valor real generado por la integración).
- Si EMASESA cambia el criterio para detectar la pantalla de verificación (actualmente se distingue por la presencia del formulario `formData`), la integración podría no reconocerla y fallar con un error de conexión en vez de pedir el código correctamente.

Si algo se rompe, compara la petición real (con las DevTools del navegador, pestaña Network) contra lo que envía `api.py` y ajusta las constantes/regex correspondientes.

## Solución de problemas

### Pide reautenticación constantemente

Es señal de que EMASESA ha dejado de confiar en el identificador de dispositivo guardado (cambio de contraseña, revocación por su parte, etc.). Sigue el aviso de **Reconectar con EMASESA** de la propia integración; no hace falta eliminarla y añadirla de nuevo.

### No llega el código SMS/email

Comprueba que el teléfono/email registrado en tu cuenta de la Oficina Online sigue siendo correcto entrando manualmente en el portal de EMASESA. La integración no puede solicitar el reenvío del código por ti.

### Activa el registro de depuración

Añade esto a tu `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.emasesa: debug
```

Después revisa los logs en **Ajustes → Sistema → Registros**.

### Lista de comprobación de instalación

- [ ] Home Assistant 2024.1.0 o superior
- [ ] Archivos en `/config/custom_components/emasesa/`
- [ ] Home Assistant reiniciado tras la instalación
- [ ] Integración añadida desde la UI con tu usuario y contraseña
- [ ] Código de verificación introducido si se ha solicitado
- [ ] Los sensores `ultimo_dia` y `consumo_acumulado` aparecen en Dispositivos y servicios
- [ ] Sensor `Consumo acumulado` añadido como fuente de agua en el Panel de Energía

## Aviso

Proyecto no oficial, sin relación con EMASESA. Úsalo bajo tu responsabilidad: automatiza el acceso a tu propia cuenta con tus propias credenciales, igual que harías entrando manualmente en el navegador.

## Licencia

Este proyecto está licenciado bajo la GNU Affero General Public License v3.0 - consulta el archivo [LICENSE](LICENSE) para más detalles.

## Soporte

Si encuentras algún problema, por favor [abre un issue](https://github.com/aramcap/hass_aqua_emasesa/issues) en GitHub.
