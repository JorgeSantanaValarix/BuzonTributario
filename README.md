# BuzonTributario

Web scraper for the **SAT Buzón Tributario** (Mexican tax authority mailbox). It logs in with e.firma (certificate + private key + password), navigates Mis expedientes sections, and **downloads messages and documents** (e.g. PDFs from unread comunicados).

## Objective

- Automate access to [Buzón Tributario](https://wwwmat.sat.gob.mx/personas/iniciar-sesion) using local e.firma credentials.
- **Mis documentos**: read the Líneas de captura table (Cobranza).
- **Mis notificaciones**: read the notifications table and log filter state / "No se encontraron resultados" or rows.
- **Mis comunicados**: in "Mensajes no leídos", log each message label and **download PDFs** via the "aquí" link; if the section shows "No existe información", log that.

All steps are logged; on exit the script attempts to close the satisfaction survey (click "No" if present) and log out ("Cerrar sesión") before closing the browser.

---

## Setup

### 1. Python

- Install **Python 3.10+** and ensure `python` and `pip` are on your PATH.

### 2. Run setup (Windows)

```batch
setup.bat
```

This will install dependencies from `requirements.txt` (Playwright) and the Playwright Chromium browser. No virtual environment is created.

### 3. Manual setup (any OS)

```bash
pip install -r requirements.txt
playwright install chromium
```

(Optional: use a virtual environment with `python -m venv venv` and activate it before running the commands above.)

### 4. Configuration

1. Copy the example config:

   ```batch
   copy config.example.json config.json
   ```

2. Edit `config.json` and set:

   | Field | Description |
   |-------|-------------|
   | `test_cer_path` | Full path to your `.cer` e.firma certificate file |
   | `test_key_path` | Full path to your `.key` private key file |
   | `test_password` | e.firma password |
   | `download_dir` | (Optional) Folder where PDFs from Mis comunicados are saved. If empty, a `downloads` folder next to the script is used. |

3. Keep `buzon_field_mapping.json` in the same directory (used for login selectors).

---

## Usage

Run one of the modes below (from the project directory).

### Modes

| Flag | Description |
|------|-------------|
| `--test-login` | Only log in, verify Buzón loaded, wait 10 s, then log out and close. No navigation to Mis documentos/notificaciones/comunicados. |
| `--test-documentos` | After login: Mis expedientes → Mis documentos → Cobranza → Líneas de captura; read table (or "No existe información"). |
| `--test-notificaciones` | After login: Mis expedientes → Mis notificaciones; wait for load; read table (filters, "No se encontraron resultados" or rows). |
| `--test-comunicados` | After login: Mis expedientes → Mis comunicados; wait for "Mensajes no leídos"; if "No existe información" log it, else for each unread message log label and download PDF via "aquí". |
| `--test-full` | Run **documentos**, then **notificaciones**, then **comunicados** in one session (same login). |

### Examples

```batch
REM Login only
python buzonTributario.py --test-login

REM Mis documentos (Cobranza → Líneas de captura)
python buzonTributario.py --test-documentos

REM Mis notificaciones
python buzonTributario.py --test-notificaciones

REM Mis comunicados (download PDFs from Mensajes no leídos)
python buzonTributario.py --test-comunicados

REM Full run: documentos + notificaciones + comunicados
python buzonTributario.py --test-full
```

### Optional arguments

- `--config PATH` — Use a different config file (default: `config.json` in the script directory).
- `--mapping PATH` — Use a different field mapping JSON (default: `buzon_field_mapping.json`).

---

## Logs

- Log file: path from `config.json` → `log_file` (default: `buzon_tributario.log`).
- Each step (login, navigation, table read, filter checks, "No existe información", "No se encontraron resultados", PDF downloads) is written to the log and to the console.

---

## Requirements

- **Python 3.10+**
- **Playwright** (see `requirements.txt`); browser: Chromium (installed via `playwright install chromium`).
- Valid e.firma (.cer, .key, password) for the SAT Buzón Tributario portal.
