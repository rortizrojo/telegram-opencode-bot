# Bot de Telegram para OpenCode

[![Licencia: MIT](https://img.shields.io/badge/licencia-MIT-green.svg)](LICENSE)
[![Última versión](https://img.shields.io/badge/descarga-v1.0.0-blue.svg)](https://github.com/rortizrojo/telegram-opencode-bot/releases/latest)
![Windows](https://img.shields.io/badge/Windows-10%2F11-0078D6)

> **Instalación rápida:** descarga el ZIP desde
> [Releases](https://github.com/rortizrojo/telegram-opencode-bot/releases/latest),
> descomprímelo y ejecuta `instalar.cmd`.

Controla [OpenCode](https://opencode.ai) desde Telegram: le escribes por el móvil y el agente
trabaja en tu PC (lee, edita y ejecuta código), devolviéndote la respuesta al chat.

Funciona con el **motor v2** de OpenCode, el mismo que usa la app de escritorio, así que
**las sesiones son las mismas**: lo que hablas por Telegram lo puedes continuar en la app,
y al revés.

---

## Qué necesitas

| Cosa | Detalle |
|---|---|
| Windows 10/11 | Es donde corre el bot |
| Python 3.10+ | El instalador te lo ofrece si falta |
| OpenCode | La app de escritorio (trae el motor `opencode-cli.exe`) |
| Un bot de Telegram | Se crea gratis con **@BotFather** |
| Cuenta de OpenCode | Autenticada en la app, para que los modelos respondan |

---

## Instalación (2 minutos)

1. Descomprime la carpeta donde quieras (por ejemplo `C:\Bot`).
2. **Doble clic en `instalar.cmd`**.
3. Sigue las preguntas:
   - Si falta Python, te ofrece instalarlo.
   - Te pide el **token** del bot: habla con **@BotFather** en Telegram, `/newbot`, elige nombre
     y copia el token (`123456789:AAH...`).
4. Al terminar, abre el acceso directo **Telegram Bot** del escritorio y pulsa **Arrancar**.
5. En Telegram, habla con tu bot y envía `/start`.

> **No muevas la carpeta después de instalar**: el entorno de Python (`.venv`) guarda rutas
> absolutas. Si necesitas moverla, vuelve a ejecutar `instalar.cmd` desde la nueva ubicación.

---

## Uso

| Comando | Para qué |
|---|---|
| `/start` | Presentación y estado |
| `/sesiones` | Lista las sesiones (las mismas que ves en la app) |
| `/nueva` | Crea una sesión nueva |
| `/model` | Elige el modelo (por chat) |
| `/parar` | Interrumpe lo que esté haciendo |

- Las **preguntas** del agente llegan como botones: respondes con un toque.
- Los **permisos** (ejecutar comandos, editar ficheros) también se aprueban desde Telegram.
- Las herramientas que usa (shell, edición de ficheros) se agrupan en una burbuja
  **"Ver detalle"** para no llenar el chat.

---

## Cómo está montado

```
Telegram  →  bot.py  →  oc_shim.py (127.0.0.1:4095)  →  motor v2 (127.0.0.1:4098)
                                                              ↓
                                                     base de datos de OpenCode
                                                              ↑
                                                     app de escritorio
```

- **`control.py`** es el panel: arranca y para el bot *y* el motor, y muestra si algo está caído.
- **`oc_shim.py`** traduce la API antigua que usa el bot a la API v2 del motor.
- **`bot.py`** es el bot de Telegram.

El panel tiene su **propio motor**, así que el bot funciona aunque tengas la app cerrada.
Comparten base de datos, por eso las sesiones y el contexto son los mismos.

---

## Problemas típicos

| Síntoma | Causa y solución |
|---|---|
| `All connection attempts failed` en `bot.log` | El motor no está arrancado. Abre el panel y pulsa **Arrancar**. |
| `409 Conflict: terminated by other getUpdates request` | Hay dos copias del bot con el mismo token. Cierra la otra (o usa un token distinto). |
| "No encuentro opencode-cli.exe" | Instala la app de escritorio de OpenCode, o pon la ruta en `.env` con `OC_ENGINE=...`. |
| El bot responde con error de proveedor/cuota | La cuenta de OpenCode no está autenticada o no tiene saldo. Ábrela en la app. |
| El panel se queda unos segundos "colgado" al arrancar | Está esperando al motor. Vuelve solo. |

El log está en `bot.log` (rota cada 5 MB, guarda 5 ficheros).

---

## Aviso de seguridad

- **`.env` contiene el token del bot**: no lo compartas ni lo subas a un repositorio.
- El agente puede **ejecutar comandos y editar ficheros** en el PC donde corre. Configura los
  permisos en `%USERPROFILE%\.config\opencode\opencode.json` si quieres restringirlo.
- Cualquiera que encuentre tu bot en Telegram podría hablarle. Si te preocupa, mantén el
  nombre poco obvio o añade una lista de chats permitidos.

---

## Desinstalar

Doble clic en `desinstalar.cmd`: para los procesos, borra el entorno `.venv` y el acceso directo.
Tus sesiones de OpenCode no se tocan (viven en la base de datos de la app).

---

## Contribuir

Las ideas y los parches son bienvenidos.

1. Haz un fork y crea una rama: `git checkout -b mi-mejora`
2. Prueba tu cambio con el instalador en una carpeta limpia.
3. Abre un *pull request* explicando qué problema resuelve.

Si vas a tocar el shim, ten en cuenta que traduce entre dos versiones de la API: hay que
mantener las dos formas documentadas en los comentarios (`/api/session/{id}/prompt`, etc.).

---

## Licencia

**MIT** — puedes usarlo, modificarlo, venderlo y redistribuirlo, conservando el aviso de
copyright. Ver [LICENSE](LICENSE).

Hecho por **Chicano**.

