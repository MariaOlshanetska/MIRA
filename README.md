# MIRA

**M**ultimodal **I**nteractive **R**esponsive **A**gent — un agente conversacional encarnado, basado en un LLM, que percibe al usuario por voz y cámara, estima su estado de interacción (*engagement*) y responde con voz expresiva y (en desarrollo) gestos sobre un MetaHuman en Unreal Engine.

Este repositorio contiene el código del Trabajo de Fin de Máster (TFM) en Ciencia Cognitiva de Maria Olshanetska.

> **Estado del proyecto.** El recorrido de voz completo (escuchar → transcribir → estimar engagement → generar respuesta → hablar con voz expresiva) funciona de principio a fin. El renderizado en tiempo real de gestos y expresiones faciales sobre el MetaHuman está en desarrollo: las anotaciones se generan, parsean y registran, pero aún no se dibujan en tiempo real.

---

## Qué hace

El sistema reproduce la organización funcional de un diálogo situado, en un bucle continuo:

```
percibir  →  mantener un estado  →  adaptarse  →  actuar (voz + cuerpo)
```

- **Percibir.** Transcribe el habla del usuario y, en paralelo, extrae señales prosódicas (micrófono) y de comportamiento visual (cámara).
- **Estado.** Integra esas señales en el tiempo para estimar un valor de *engagement* entre 0 y 1.
- **Adaptarse.** El estado modula la estrategia de diálogo (por ejemplo, reparar la conversación cuando el engagement cae, o elaborar cuando se mantiene).
- **Actuar.** Un LLM genera la respuesta, que se separa en un canal de voz (TTS expresivo con emoción, pausas y ráfagas vocales) y un canal de encarnación (gestos y expresiones faciales para el MetaHuman).

El escenario de demostración es una entrevista de trabajo con el agente **Aera**.

---

## Requisitos

- **Python 3.10 o superior**
- Una **cámara web** y un **micrófono** (para la percepción en tiempo real)
- Acceso a los servicios externos usados por el prototipo:
  - Un **modelo Qwen** servido con una API compatible con OpenAI
  - Un **servicio de estimación de engagement** (endpoint HTTP)
  - Un **servicio de TTS expresivo**

---

## Instalación

```bash
# 1. Clonar el repositorio
git clone https://github.com/MariaOlshanetska/MIRA.git
cd MIRA

# 2. (Recomendado) crear un entorno virtual
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# 3. Instalar el paquete y sus dependencias
pip install -e .

# Para desarrollo (tests, linter, type-checking):
pip install -e ".[dev]"
```

### Configuración

Copia el fichero de ejemplo y ajusta los valores a tu entorno:

```bash
cp .env.example .env
```

`.env` define cómo se conecta el sistema al modelo de diálogo:

| Variable | Descripción |
|---|---|
| `QWEN_API_BASE_URL` | URL base de la API compatible con OpenAI que sirve el modelo |
| `QWEN_MODEL` | Nombre del modelo a usar |
| `QWEN_MAX_TOKENS` | Máximo de tokens de la respuesta |
| `QWEN_TEMPERATURE` | Temperatura de muestreo |
| `QWEN_TIMEOUT_SECONDS` | Tiempo máximo de espera por petición |

### Modelo de Whisper (transcripción)

Descarga el modelo local de Whisper antes del primer uso:

```bash
python scripts/download_whisper_model.py
```

---

## Cómo ejecutarlo

El sistema tiene dos piezas ejecutables que trabajan juntas.

### 1. Reconocedor de engagement (cámara + micrófono)

Analiza la cámara y el micrófono en tiempo real y publica el valor de engagement. Es donde vive, de momento, toda la lógica de percepción y fusión de señales.

```bash
python scripts/realtime_engagement_demo.py \
  --url http://<host>/engagement_maria/engagement_maria \
  --camera 0
```

### 2. Pipeline de diálogo por voz

Escucha al usuario, transcribe, consulta el estado de engagement, genera la respuesta con Qwen y la reproduce con voz expresiva.

```bash
python scripts/run_voice_pipeline_qwen_engagement.py
```

> El pipeline de diálogo lee el valor de engagement más reciente producido por el reconocedor antes de construir cada respuesta.

---

## Estructura del proyecto

El código está escrito en **Python** y organizado como un paquete, `dialogue_manager`, cuyos módulos siguen el flujo de un turno de conversación:

```
src/dialogue_manager/
├── core/         Pipeline de turno que coordina la interacción
├── stt/          Captura y transcripción del habla (Whisper)
├── engagement/   Interfaz que conecta el reconocedor con el diálogo
├── llm/          Construcción de prompts y consulta al modelo (Qwen)
├── output/       Parseo de la respuesta en canal de voz y de encarnación
├── tts/          Realización de la voz expresiva
├── unreal/       Envío de comandos de encarnación al MetaHuman
├── utils/        Logging, manejo de errores
└── config.py     Configuración y ajustes compartidos

scripts/          Puntos de entrada ejecutables y utilidades
configs/          Configuración y ficheros de prompt del agente
  └── prompts/    Escenario y repertorio de comportamiento de Aera
data/bursts/      Ráfagas vocales (risa, suspiro, sorpresa, etc.)
```

> **Nota sobre la arquitectura.** La estructura anterior refleja el diseño previsto. En el estado actual, toda la percepción en tiempo real (extracción de señales de cámara y micrófono, y su fusión en un valor de engagement) vive de forma provisional en el script `scripts/realtime_engagement_demo.py`. El módulo `engagement/` solo contiene, por ahora, la interfaz que conecta ese reconocedor con el gestor de diálogo. Consolidar esa lógica dentro del módulo es trabajo pendiente.

Los ficheros de prompt (escenario y comportamiento del agente) se mantienen separados del código, de modo que la interacción puede reescribirse sin tocar la implementación.

---

## Tecnologías

| Función | Herramienta |
|---|---|
| Transcripción de voz (ASR) | Whisper (faster-whisper) |
| Análisis prosódico | Parselmouth / Praat |
| Comportamiento visual | Modelo de visión-lenguaje (VLM) |
| Gestión de diálogo | Qwen (servido vía API compatible con OpenAI) |
| Voz expresiva (TTS) | Servicio de TTS expresivo |
| Encarnación | MetaHuman en Unreal Engine (en desarrollo) |

El desarrollo se realizó con la asistencia de [Kiro](https://kiro.dev), a partir de instrucciones precisas que definían el comportamiento de cada componente. Todo el código generado fue revisado, probado y adaptado al diseño previsto.

---

## Desarrollo

```bash
pytest        # ejecutar los tests
ruff check .  # linter
mypy src      # comprobación de tipos
```

---

## Autoría

Maria Olshanetska — Trabajo de Fin de Máster en Ciencia Cognitiva.
