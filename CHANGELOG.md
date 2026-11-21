# Changelog

## 1.1.0

- **Modo v1**: el bot puede hablar directamente con un motor v1 (`opencode serve --port 4096`),
  sin shim ni app de escritorio. Pensado para equipos con OpenCode solo dentro de WSL.
- El instalador detecta OpenCode en WSL (probando distro por distro) y configura `MODO=v1`,
  `OPENCODE_URL`, `WSL_DISTRO` y `WSL_CMD` en el `.env`.
- El panel muestra el estado del motor v1 y puede arrancarlo en WSL al pulsar **Arrancar**.
- El `.env` se lee también para los procesos hijos (permite `OC_ENGINE`/`OC_DIR` sin tocar código).
- Arreglos de detección: nombres de distro de WSL en UTF-16, y mensajes de error de WSL que
  se confundían con la ruta del binario.

## 1.0.0

Primera versión publicada.

- Bot de Telegram que controla OpenCode: sesiones reales compartidas con la app de escritorio.
- Selector de modelo por chat (`/model`), interruptor de turno (`/parar`).
- Preguntas y permisos del agente respondibles desde Telegram con botones.
- Herramientas agrupadas en burbujas colapsables ("Ver detalle").
- Shim que traduce la API v1 a la v2 y arranca/vigila su propio motor.
- Panel de control con estado del bot y del motor, y botones Arrancar/Parar/Reiniciar.
- Instalador para Windows con entorno aislado y detección automática del motor.
