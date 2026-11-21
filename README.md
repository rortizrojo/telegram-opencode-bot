# Bot de Telegram para OpenCode

[![Licencia: MIT](https://img.shields.io/badge/licencia-MIT-green.svg)](LICENSE)
[![Última versión](https://img.shields.io/badge/descarga-v1.1.0-blue.svg)](https://github.com/rortizrojo/telegram-opencode-bot/releases/latest)
![Windows](https://img.shields.io/badge/Windows-10%2F11-0078D6)

> **Instalación rápida:** descarga el ZIP desde
> [Releases](https://github.com/rortizrojo/telegram-opencode-bot/releases/latest),
> descomprímelo y ejecuta `instalar.cmd`.

Controla [OpenCode](https://opencode.ai) desde Telegram: le escribes por el móvil y el agente
trabaja en tu PC (lee, edita y ejecuta código), devolviéndote la respuesta al chat.

Con la app de escritorio usa el **motor v2**, así que **las sesiones son las mismas**: lo que
hablas por Telegram lo puedes continuar en la app, y al revés. Si tu OpenCode está dentro de
WSL, el bot habla directamente con ese motor en **modo v1** (ver más abajo).

---

## Qué necesitas

| Cosa | Detalle |
|---|---|
| Windows 10/11 | Es donde corre el bot |
| Python 3.10+ | El instalador te lo ofrece si falta |
| OpenCode | La app de escritorio (motor v2) **o** OpenCode dentro de WSL (v1) |
| Un bot de Telegram | Se crea gratis con **@BotFather** |
| Cuenta de OpenCode | Autenticada, para que los modelos respondan |

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

## Si tu OpenCode está dentro de WSL

No pasa nada: el instalador lo detecta y configura el bot para hablar con ese motor (**modo v1**,
sin shim). Solo hay una condición, porque Windows tiene que alcanzarlo:

```bash
# dentro de WSL
opencode serve --port 4096 --hostname 0.0.0.0
```

En segundo plano:

```bash
setsid nohup opencode serve --port 4096 --hostname 0.0.0.0 >/tmp/opencode-wsl.log 2>&1 &
```

WSL2 reenvía `localhost` de Windows a la distro, así que el bot conecta a `127.0.0.1:4096`.
El instalador te preguntará *"¿Usar el motor de WSL?"* y dejará el `.env` así:

```ini
MODO=v1
OPENCODE_URL=http://127.0.0.1:4096
WSL_DISTRO=Ubuntu
WSL_CMD=opencode serve --port 4096 --hostname 0.0.0.0
```

El panel mostrará **"motor v1 (WSL): OK"** y, si está caído, lo arranca solo con `WSL_CMD`.
Pulsar **Parar** no apaga el motor de WSL, solo el bot.

> Si `127.0.0.1:4096` no responde desde Windows, prueba con la IP de WSL (`wsl hostname -I`)
> o activa `networkingMode=mirrored` en `.wslconfig`.

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

Modo v2 (app de escritorio):

```
Telegram  →  bot.py  →  oc_shim.py (127.0.0.1:4095)  →  motor v2 (127.0.0.1:4098)
                                                              ↓
                                                     base de datos de OpenCode
                                                              ↑
                                                     app de escritorio
```

Modo v1 (OpenCode en WSL o motor propio):

```
Telegram  →  bot.py  →  127.0.0.1:4096  →  opencode serve
```

- **`control.py`** es el panel: arranca y para el bot *y* el motor, y muestra si algo está caído.
- **`oc_shim.py`** traduce la API antigua que usa el bot a la API v2 del motor (solo v2).
- **`bot.py`** es el bot de Telegram.

En modo v2 el panel tiene su **propio motor**, así que el bot funciona aunque tengas la app
cerrada; comparten base de datos, por eso las sesiones y el contexto son los mismos.

---

## Problemas típicos

| Síntoma | Causa y solución |
|---|---|
| `All connection attempts failed` en `bot.log` | El motor no está arrancado. Abre el panel y pulsa **Arrancar**. |
| Panel dice `motor v1 (WSL): parado` | Arranca el motor en WSL con `opencode serve --port 4096 --hostname 0.0.0.0`. |
| `409 Conflict: terminated by other getUpdates request` | Hay dos copias del bot con el mismo token. Cierra la otra (o usa un token distinto). |
| "No encuentro opencode-cli.exe" | Instala la app de escritorio, pon la ruta en `.env` con `OC_ENGINE=...`, o usa el motor de WSL. |
| El bot responde con error de proveedor/cuota | La cuenta de OpenCode no está autenticada o no tiene saldo. |
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
