#!/usr/bin/env python3
"""
BuzonTributario — Login to SAT Buzón using e.firma (.cer, .key, password) reusing
the login flow from sat_declaration_filler_2/sat_declaration_filler.py.

Current modes (local only, no API yet):

  --test-login : open SAT Buzón login page and log in with local e.firma
  --test-full  : same as --test-login, reserved for future extended flows
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_PORTAL_URL = "https://wwwmat.sat.gob.mx/personas/iniciar-sesion"

_run_context: dict | None = None
RETRY_WAIT_SECONDS = 60


def load_buzon_config(config_path: str | None) -> dict:
    """
    Load Buzón config JSON and normalize keys:
      - buzon_sat_portal_url (optional; defaults to DEFAULT_PORTAL_URL)
      - test_cer_path, test_key_path, test_password (required for test-* modes)
      - sat_ui (optional; if empty/missing, DEFAULT_SAT_UI from sat_declaration_filler is used)
    """
    if config_path:
        cfg_path = Path(config_path)
    else:
        cfg_path = SCRIPT_DIR / "config.json"

    if not cfg_path.is_file():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    portal_url = raw.get("buzon_sat_portal_url") or DEFAULT_PORTAL_URL
    cer_path = raw.get("test_cer_path") or ""
    key_path = raw.get("test_key_path") or ""
    password = raw.get("test_password") or ""
    sat_ui = raw.get("sat_ui") or {}
    download_dir = raw.get("download_dir") or ""

    missing = []
    if not cer_path:
        missing.append("test_cer_path")
    if not key_path:
        missing.append("test_key_path")
    if not password:
        missing.append("test_password")
    if missing:
        raise ValueError(
            "Missing required config fields for local e.firma login: "
            + ", ".join(missing)
        )

    # Normalize download directory (for PDFs from Mis comunicados)
    if not download_dir:
        download_dir_path = SCRIPT_DIR / "downloads"
    else:
        download_dir_path = Path(download_dir)
        if not download_dir_path.is_absolute():
            download_dir_path = SCRIPT_DIR / download_dir_path
    download_dir_path.mkdir(parents=True, exist_ok=True)

    # 500 error retry settings (for Mis notificaciones, Mis comunicados, Mis documentos)
    section_500_retry_max = raw.get("section_500_retry_max", 2)
    section_500_retry_wait_seconds = raw.get("section_500_retry_wait_seconds", 10)

    # Login verification max wait (safety limit to prevent infinite loop)
    login_max_wait_seconds = raw.get("login_max_wait_seconds", 120)

    return {
        "portal_url": portal_url,
        "cer_path": cer_path,
        "key_path": key_path,
        "password": password,
        "sat_ui": sat_ui,
        "download_dir": str(download_dir_path),
        "section_500_retry_max": section_500_retry_max,
        "section_500_retry_wait_seconds": section_500_retry_wait_seconds,
        "login_max_wait_seconds": login_max_wait_seconds,
        "raw": raw,
        "config_path": str(cfg_path),
    }


def _resolve_mapping_path(cli_mapping: str | None) -> Path:
    """
    Determine which mapping JSON to use:
      - If CLI --mapping is provided, use that.
      - Else default to buzon_field_mapping.json in this repo.
    """
    if cli_mapping:
        mp = Path(cli_mapping)
        if not mp.is_file():
            raise FileNotFoundError(f"Mapping file not found: {mp}")
        return mp

    buzon_mapping = SCRIPT_DIR / "buzon_field_mapping.json"
    if buzon_mapping.is_file():
        return buzon_mapping

    raise FileNotFoundError(
        f"No mapping JSON found. Expected {buzon_mapping} or pass --mapping explicitly."
    )


def _load_mapping(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _iter_frames(page):
    # main frame + any child frames (iframes)
    yield page.main_frame
    for fr in page.frames:
        if fr is not page.main_frame:
            yield fr


def _try_click(page, selectors: list[str]) -> bool:
    for sel in selectors:
        for frame in _iter_frames(page):
            try:
                loc = frame.locator(sel)
                if loc.count() == 0:
                    continue
                first = loc.first
                first.wait_for(state="visible", timeout=2000)
                first.click()
                return True
            except Exception:
                continue
    return False


def _try_fill_file(page, selectors: list[str], file_path: str) -> bool:
    for sel in selectors:
        for frame in _iter_frames(page):
            try:
                loc = frame.locator(sel)
                if loc.count() == 0:
                    continue
                first = loc.first
                first.set_input_files(file_path)
                return True
            except Exception:
                continue
    return False


def _try_fill_text(page, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        for frame in _iter_frames(page):
            try:
                loc = frame.locator(sel)
                if loc.count() == 0:
                    continue
                first = loc.first
                first.fill(value)
                return True
            except Exception:
                continue
    return False


def _check_sat_500(page) -> None:
    """
    Detect SAT HTTP 500 error pages after login and raise a clear exception.
    """
    patterns = [
        "error: http 500 internal server error",
        "error: http 500",
        "http 500 internal server error",
    ]
    text = ""
    for frame in _iter_frames(page):
        try:
            body = (frame.locator("body").inner_text(timeout=1000) or "").lower()
            text += "\n" + body
        except Exception:
            continue
    if any(pat in text for pat in patterns):
        msg = "SAT login returned HTTP 500 Internal Server Error (server-side). Try again later or check SAT status."
        logging.error(msg)
        raise RuntimeError(msg)


def _detect_sat_500(page) -> bool:
    """
    Detect SAT HTTP 500 error pages and return True if found, False otherwise.
    Does NOT raise an exception (used for retry logic).
    """
    patterns = [
        "error: http 500 internal server error",
        "error: http 500",
        "http 500 internal server error",
        "error 500--internal server error",
        "500 internal server error",
    ]
    text = ""
    for frame in _iter_frames(page):
        try:
            body = (frame.locator("body").inner_text(timeout=1000) or "").lower()
            text += "\n" + body
        except Exception:
            continue
    return any(pat in text for pat in patterns)


def _navigate_section_with_retry(
    page,
    nav_func,
    section_name: str,
    max_retries: int,
    wait_seconds: int,
) -> None:
    """
    Navigate to a section (Mis notificaciones, Mis comunicados, Mis documentos) with
    retry logic for HTTP 500 errors.

    1. Opens 'Mis expedientes' menu
    2. Calls nav_func(page) to navigate to the section
    3. If 500 error detected: wait, then retry from step 1
    4. Repeats up to max_retries times before raising RuntimeError
    """
    for attempt in range(max_retries + 1):
        open_mis_expedientes_menu(page)
        nav_func(page)
        page.wait_for_timeout(1000)

        if not _detect_sat_500(page):
            return

        if attempt < max_retries:
            logging.warning(
                "Section '%s': HTTP 500 error detected (attempt %d/%d). "
                "Waiting %d seconds before retry...",
                section_name,
                attempt + 1,
                max_retries + 1,
                wait_seconds,
            )
            page.wait_for_timeout(wait_seconds * 1000)
        else:
            msg = (
                f"Section '{section_name}': HTTP 500 error persisted after "
                f"{max_retries + 1} attempts. Giving up."
            )
            logging.error(msg)
            raise RuntimeError(msg)


def login_buzon(
    page,
    efirma: dict,
    mapping: dict,
    base_url: str = DEFAULT_PORTAL_URL,
    login_max_wait_seconds: int = 120,
) -> None:
    """
    Minimal login flow for SAT Buzón using e.firma, using selectors from mapping.
    Waits until 'Buzón Tributario de' pattern is detected to confirm successful login.
    """
    t0 = time.perf_counter()

    def _elapsed() -> float:
        return round(time.perf_counter() - t0, 2)

    global _run_context
    _run_context = {
        "page": page,
        "mapping": mapping,
        "logged_in": False,
    }
    logging.info("")
    logging.info("===== Section: Login (e.firma, Buzón) =====")

    response = page.goto(base_url, wait_until="load", timeout=60000)
    page.wait_for_load_state("domcontentloaded")
    status = response.status if response else None
    logging.info("Phase 1: [%.2fs] SAT page loaded (status=%s), looking for e.firma button", _elapsed(), status)

    efirma_selectors = mapping.get("_login_e_firma_button", [])
    max_tries = 20
    for _ in range(max_tries):
        if _try_click(page, efirma_selectors):
            break
        page.wait_for_timeout(500)
    else:
        raise RuntimeError("Could not find e.firma button on SAT Buzón login page")

    logging.info("Phase 1: [%.2fs] e.firma pressed", _elapsed())
    page.wait_for_timeout(1500)

    cer_selectors = mapping.get("_login_cer_file_input", [])
    key_selectors = mapping.get("_login_key_file_input", [])
    pwd_selectors = mapping.get("_login_password_input", [])
    enviar_selectors = mapping.get("_login_enviar_button", [])

    if not _try_fill_file(page, cer_selectors, efirma["cer_path"]):
        raise RuntimeError("Could not fill .cer file input on SAT Buzón page")
    logging.info("Phase 1: [%.2fs] filled .cer", _elapsed())

    if not _try_fill_file(page, key_selectors, efirma["key_path"]):
        raise RuntimeError("Could not fill .key file input on SAT Buzón page")
    logging.info("Phase 1: [%.2fs] filled .key", _elapsed())

    if not _try_fill_text(page, pwd_selectors, efirma["password"]):
        raise RuntimeError("Could not fill password input on SAT Buzón page")
    logging.info("Phase 1: [%.2fs] filled password", _elapsed())

    # Small delay before pressing Enviar to let SAT finish any background validation.
    page.wait_for_timeout(500)
    if not _try_click(page, enviar_selectors):
        raise RuntimeError("Could not find Enviar button on SAT Buzón page")
    logging.info("Phase 1: [%.2fs] Enviar pressed", _elapsed())
    _run_context["logged_in"] = True
    page.wait_for_timeout(1000)
    _check_sat_500(page)

    # Wait for post-login Buzón page to fully load before any navigation.
    # Poll continuously until "Buzón Tributario de" pattern is detected.
    logging.info("Phase 1: [%.2fs] waiting for login confirmation (Buzón Tributario de)...", _elapsed())
    poll_ms = 500
    progress_interval = 10  # Log progress every 10 seconds
    last_progress_log = time.perf_counter()
    max_wait_end = time.perf_counter() + login_max_wait_seconds
    expected_pat = "buzón tributario de"
    login_confirmed = False
    detected_name = ""

    while time.perf_counter() < max_wait_end:
        page.wait_for_timeout(poll_ms)

        # Log progress every 10 seconds
        if time.perf_counter() - last_progress_log >= progress_interval:
            logging.info("Phase 1: [%.2fs] still waiting for login confirmation...", _elapsed())
            last_progress_log = time.perf_counter()

        try:
            # Check body text for "Buzón Tributario de [Name]"
            for frame in _iter_frames(page):
                try:
                    body = frame.locator("body").inner_text(timeout=1000) or ""
                    body_lower = body.lower()
                except Exception:
                    continue

                if expected_pat in body_lower:
                    # Extract the user's name from the pattern
                    match = re.search(r"buzón tributario de\s+([^\n\r]+)", body, re.IGNORECASE)
                    if match:
                        detected_name = match.group(1).strip()
                    login_confirmed = True
                    break

            if login_confirmed:
                break
        except Exception:
            continue

    if not login_confirmed:
        msg = (
            f"Login verification failed: 'Buzón Tributario de' pattern not detected "
            f"after {login_max_wait_seconds} seconds. Login may have failed."
        )
        logging.error(msg)
        raise RuntimeError(msg)

    # Log successful login
    if detected_name:
        logging.info("Phase 1: [%.2fs] >>> LOGIN SUCCESSFUL: Buzón Tributario de %s <<<", _elapsed(), detected_name)
    else:
        logging.info("Phase 1: [%.2fs] >>> LOGIN SUCCESSFUL: Buzón Tributario de (name not extracted) <<<", _elapsed())


def open_mis_expedientes_menu(page) -> None:
    """
    Open the 'Mis expedientes' dropdown in the Buzón top navigation.
    """
    logging.info("")
    logging.info("===== Section: Mis expedientes =====")
    logging.info("Phase 2: opening 'Mis expedientes' menu...")
    selectors = [
        "button:has-text('Mis expedientes')",
        "a:has-text('Mis expedientes')",
        "[role='button']:has-text('Mis expedientes')",
        "text=/\\bMis expedientes\\b/i",
    ]
    if not _try_click(page, selectors):
        raise RuntimeError("Could not find 'Mis expedientes' menu in Buzón")
    page.wait_for_timeout(500)


def go_to_mis_documentos(page) -> None:
    """
    From the opened 'Mis expedientes' menu, click 'Mis documentos' and then
    navigate Cobranza -> Líneas de captura and read the table.
    """
    logging.info("")
    logging.info("===== Section: Mis Documentos =====")
    logging.info("Phase 2: navigating to 'Mis documentos'...")
    selectors = [
        "a:has-text('Mis documentos')",
        "button:has-text('Mis documentos')",
        "text=/\\bMis documentos\\b/i",
    ]
    if not _try_click(page, selectors):
        raise RuntimeError("Could not find 'Mis documentos' option in Buzón")
    page.wait_for_timeout(1000)

    # Mis Documentos -> Cobranza -> Líneas de captura -> read table.
    go_to_cobranza(page)
    go_to_lineas_de_captura(page)
    read_lineas_de_captura_table(page)


def go_to_mis_notificaciones(page) -> None:
    """
    From the opened 'Mis expedientes' menu, click 'Mis notificaciones'.
    """
    logging.info("")
    logging.info("===== Section: Mis Notificaciones =====")
    logging.info("Phase 2: navigating to 'Mis notificaciones'...")
    selectors = [
        "a:has-text('Mis notificaciones')",
        "button:has-text('Mis notificaciones')",
        "text=/\\bMis notificaciones\\b/i",
    ]
    if not _try_click(page, selectors):
        raise RuntimeError("Could not find 'Mis notificaciones' option in Buzón")
    page.wait_for_timeout(1000)


def wait_for_notificaciones_loaded(page) -> None:
    """
    Wait until the Mis notificaciones page has finished loading by detecting
    the text 'Total de notificaciones pendientes:' (same pattern as post-login wait).
    """
    logging.info("Phase 2: waiting for Mis notificaciones page to load...")
    timeout_ms = 8000
    poll_ms = 150
    t_end = time.perf_counter() + (timeout_ms / 1000.0)
    expected_pat = "total de notificaciones pendientes:"
    while time.perf_counter() < t_end:
        page.wait_for_timeout(poll_ms)
        try:
            for frame in _iter_frames(page):
                try:
                    body = (frame.locator("body").inner_text(timeout=500) or "").lower()
                except Exception:
                    continue
                if expected_pat in body:
                    logging.info("Phase 2: Mis notificaciones page loaded (pattern detected).")
                    return
        except Exception:
            continue
    logging.warning("Phase 2: Mis notificaciones load timeout; continuing anyway.")


def go_to_mis_comunicados(page) -> None:
    """
    From the opened 'Mis expedientes' menu, click 'Mis comunicados'.
    """
    logging.info("")
    logging.info("===== Section: Mis comunicados =====")
    logging.info("Phase 2: navigating to 'Mis comunicados'...")
    selectors = [
        "a:has-text('Mis comunicados')",
        "button:has-text('Mis comunicados')",
        "text=/\\bMis comunicados\\b/i",
    ]
    if not _try_click(page, selectors):
        raise RuntimeError("Could not find 'Mis comunicados' option in Buzón")
    page.wait_for_timeout(1000)


def wait_for_comunicados_loaded(page) -> None:
    """
    Wait until the Mis comunicados page has finished loading by detecting
    the section header 'Mensajes no leídos' (same pattern as post-login wait).
    """
    logging.info("Phase 2: waiting for Mis comunicados page to load...")
    timeout_ms = 8000
    poll_ms = 150
    t_end = time.perf_counter() + (timeout_ms / 1000.0)
    expected_pat = "mensajes no leídos"
    while time.perf_counter() < t_end:
        page.wait_for_timeout(poll_ms)
        try:
            for frame in _iter_frames(page):
                try:
                    body = (frame.locator("body").inner_text(timeout=500) or "").lower()
                except Exception:
                    continue
                if expected_pat in body:
                    logging.info("Phase 2: Mis comunicados page loaded (pattern 'Mensajes no leídos' detected).")
                    return
        except Exception:
            continue
    logging.warning("Phase 2: Mis comunicados load timeout; continuing anyway.")


def go_to_cobranza(page) -> None:
    """
    Inside 'Mis documentos', click the 'Cobranza' button/tab.
    """
    logging.info("Phase 3: navigating to 'Cobranza'...")
    selectors = [
        "button:has-text('Cobranza')",
        "a:has-text('Cobranza')",
        "text=/\\bCobranza\\b/i",
    ]
    if not _try_click(page, selectors):
        raise RuntimeError("Could not find 'Cobranza' option in Mis documentos")
    page.wait_for_timeout(1000)


def go_to_lineas_de_captura(page) -> None:
    """
    From Cobranza, click the 'Líneas de captura' option.
    """
    logging.info("Phase 3: navigating to 'Líneas de captura'...")
    selectors = [
        "a:has-text('Líneas de captura')",
        "button:has-text('Líneas de captura')",
        "text=/L[ií]neas de captura/i",
    ]
    if not _try_click(page, selectors):
        raise RuntimeError("Could not find 'Líneas de captura' option in Cobranza")
    page.wait_for_timeout(1000)


def read_lineas_de_captura_table(page) -> None:
    """
    Read the 'Líneas de captura' table.

    Expected columns: Fecha, Identificador, Descripción, Formato de pago.
    If there is no data, the page shows 'No existe información' which we log explicitly.
    """
    logging.info("Phase 3: reading 'Líneas de captura' table...")

    # Check for 'No existe información' in any frame.
    for frame in _iter_frames(page):
        try:
            no_info_loc = frame.locator("text=/No existe informaci[oó]n/i")
            if no_info_loc.count() > 0:
                logging.info("Líneas de captura: No existe información")
                logging.info(">>> MESSAGES IN MIS DOCUMENTOS (Líneas de captura): 0 messages found (No existe información) <<<")
                return
        except Exception:
            continue

    # Try to find the first visible table in any frame and read its rows.
    rows_data: list[dict] = []
    for frame in _iter_frames(page):
        try:
            table = frame.locator("table").first
            if table.count() == 0:
                continue
            trs = table.locator("tr")
            row_count = trs.count()
            if row_count <= 1:
                continue
            # Assume first row is header.
            headers = [h.inner_text().strip() for h in trs.nth(0).locator("th, td").all()]
            for i in range(1, row_count):
                tds = trs.nth(i).locator("td").all()
                if not tds:
                    continue
                values = [td.inner_text().strip() for td in tds]
                row = {}
                for idx, val in enumerate(values):
                    key = headers[idx] if idx < len(headers) and headers[idx] else f"col_{idx}"
                    row[key] = val
                rows_data.append(row)
            break
        except Exception:
            continue

    if not rows_data:
        logging.info("Líneas de captura: table found but no data rows detected.")
        logging.info(">>> MESSAGES IN MIS DOCUMENTOS (Líneas de captura): 0 messages found <<<")
        return

    logging.info("Líneas de captura: found %d row(s).", len(rows_data))
    for i, row in enumerate(rows_data, start=1):
        logging.info("Líneas de captura row %d: %s", i, row)
    logging.info(">>> MESSAGES IN MIS DOCUMENTOS (Líneas de captura): %d messages found <<<", len(rows_data))


def read_notificaciones_table(page) -> None:
    """
    Read 'Mis notificaciones' table.

    Columns: Folio del acto administrativo, Autoridad emisora, Acto administrativo,
    Fecha de aviso, Aviso, Documento.
    When there is no data:
      - Filters for Autoridad emisora / Acto administrativo only have 'Seleccione'
      - Table body shows 'No se encontraron resultados'
    Logs each step and its output so the user can see the full check sequence.
    """
    logging.info("Phase 3: reading 'Mis notificaciones' table...")

    # Step 1: Check filter options (Autoridad emisora, Acto administrativo).
    logging.info("Mis notificaciones Step 1: Checking filter options (Autoridad emisora, Acto administrativo)...")
    filters_logged = 0
    for frame in _iter_frames(page):
        try:
            selects = frame.locator("select").all()
        except Exception:
            continue
        for sel in selects:
            label_text = ""
            try:
                label = sel.locator("xpath=preceding::label[1]")
                if label.count() > 0:
                    label_text = (label.first.inner_text() or "").strip()
            except Exception:
                label_text = ""
            try:
                options = [o.inner_text().strip() for o in sel.locator("option").all()]
            except Exception:
                options = []
            if not options:
                continue
            if label_text and any(k in label_text for k in ["Autoridad emisora", "Acto administrativo"]):
                logging.info("Mis notificaciones Step 1 output: Filtro '%s' opciones: %s", label_text, options)
                filters_logged += 1
        break
    if filters_logged == 0:
        logging.info("Mis notificaciones Step 1 output: No filter dropdowns found for Autoridad emisora / Acto administrativo.")

    # Step 2: Check for 'No se encontraron resultados' in table body.
    logging.info("Mis notificaciones Step 2: Checking for 'No se encontraron resultados' in table body...")
    found_no_results_text = False
    for frame in _iter_frames(page):
        try:
            no_results = frame.locator("text=/No se encontraron resultados/i")
            if no_results.count() > 0:
                found_no_results_text = True
                break
        except Exception:
            continue
    if found_no_results_text:
        logging.info("Mis notificaciones Step 2 output: Found 'No se encontraron resultados' — no notification rows for current filters.")
        logging.info(">>> MESSAGES IN MIS NOTIFICACIONES: 0 messages found (No se encontraron resultados) <<<")
        return
    logging.info("Mis notificaciones Step 2 output: Text 'No se encontraron resultados' not found; attempting to read table rows.")

    # Step 3: Read table rows (headers: Folio del acto administrativo, Autoridad emisora, etc.).
    logging.info("Mis notificaciones Step 3: Reading table rows...")
    rows_data: list[dict] = []
    for frame in _iter_frames(page):
        try:
            table = frame.locator("table").first
            if table.count() == 0:
                continue
            trs = table.locator("tr")
            row_count = trs.count()
            if row_count <= 1:
                continue
            headers = [h.inner_text().strip() for h in trs.nth(0).locator("th, td").all()]
            logging.info("Mis notificaciones Step 3 output: Table headers: %s", headers)
            for i in range(1, row_count):
                tds = trs.nth(i).locator("td").all()
                if not tds:
                    continue
                values = [td.inner_text().strip() for td in tds]
                row = {}
                for idx, val in enumerate(values):
                    key = headers[idx] if idx < len(headers) and headers[idx] else f"col_{idx}"
                    row[key] = val
                rows_data.append(row)
            break
        except Exception:
            continue

    if not rows_data:
        logging.info("Mis notificaciones Step 3 output: Table found but no data rows; re-checking for 'No se encontraron resultados'...")
        for frame in _iter_frames(page):
            try:
                no_results = frame.locator("text=/No se encontraron resultados/i")
                if no_results.count() > 0:
                    logging.info("Mis notificaciones Step 3 output: Confirmed 'No se encontraron resultados' — no notification rows.")
                    logging.info(">>> MESSAGES IN MIS NOTIFICACIONES: 0 messages found (No se encontraron resultados) <<<")
                    return
            except Exception:
                continue
        logging.info("Mis notificaciones Step 3 output: No data rows and 'No se encontraron resultados' not detected.")
        logging.info(">>> MESSAGES IN MIS NOTIFICACIONES: 0 messages found <<<")
        return

    logging.info("Mis notificaciones Step 3 output: Found %d row(s).", len(rows_data))
    for i, row in enumerate(rows_data, start=1):
        logging.info("Mis notificaciones row %d: %s", i, row)
    logging.info(">>> MESSAGES IN MIS NOTIFICACIONES: %d messages found <<<", len(rows_data))


def _find_section_container(page, section_title: str):
    """
    Find the container element for a section by its header text.
    Returns (frame, container_locator) or (None, None) if not found.
    
    The section is identified by finding the header text and then getting
    the parent container or the next sibling that contains the content.
    """
    for frame in _iter_frames(page):
        try:
            # Look for the section header (e.g., "Mensajes no leídos" or "Mensajes leídos")
            header = frame.locator(f"text=/^{section_title}$/i")
            if header.count() > 0:
                # Try to find the container: look for parent div/section or table
                # that follows the header
                container = header.first.locator("xpath=following-sibling::*[1]")
                if container.count() > 0:
                    return (frame, container.first)
                # Fallback: return the parent element
                parent = header.first.locator("xpath=parent::*")
                if parent.count() > 0:
                    return (frame, parent.first)
        except Exception:
            continue
    return (None, None)


def _process_comunicados_section(
    page,
    section_name: str,
    section_label: str,
    download_dir: str | None,
) -> tuple[int, bool]:
    """
    Process a single section of Mis comunicados (either 'Mensajes no leídos' or 'Mensajes leídos').
    
    - Finds expandable items in the section
    - For each item: expands it, logs the label, clicks 'aqui' to download PDF
    - Returns (count of messages found, whether 'No existe información' was found)
    """
    logging.info("Processing section: %s...", section_name)
    
    # Check for 'No existe información' in this specific section
    # We need to look for the section header and check content near it
    no_info_found = False
    messages_found = []
    
    for frame in _iter_frames(page):
        try:
            # Find the section by looking for its header
            section_header = frame.locator(f"text=/^{section_name}$/i")
            if section_header.count() == 0:
                continue
            
            # Get the parent container that holds the section content
            # The structure appears to be: header in a div, followed by content
            section_parent = section_header.first.locator("xpath=ancestor::div[1]")
            if section_parent.count() == 0:
                section_parent = section_header.first.locator("xpath=parent::*")
            
            if section_parent.count() == 0:
                continue
                
            parent_element = section_parent.first
            
            # Check for 'No existe información' within this section's parent context
            # Look in the next sibling or within nearby elements
            try:
                # Get the outer container that includes both header and content
                outer_container = section_header.first.locator("xpath=ancestor::*[3]")
                if outer_container.count() > 0:
                    section_text = (outer_container.first.inner_text(timeout=2000) or "").lower()
                    # Check if this section specifically has "no existe información"
                    # by looking at the text between this header and the next section
                    header_text = section_name.lower()
                    header_pos = section_text.find(header_text)
                    if header_pos >= 0:
                        # Get text after this header
                        after_header = section_text[header_pos + len(header_text):]
                        # Check if "no existe información" appears before "mensajes leídos" (next section)
                        next_section_pos = after_header.find("mensajes le")
                        if next_section_pos > 0:
                            section_content = after_header[:next_section_pos]
                        else:
                            section_content = after_header[:500]  # Limit search
                        
                        if "no existe informaci" in section_content:
                            no_info_found = True
                            logging.info("%s: No existe información", section_label)
            except Exception:
                pass
            
            # Find expandable items - look for [+] buttons or clickable rows
            # The items appear to have a pattern with dates like "21/dic/2020"
            try:
                # Find all items that look like message rows (contain date pattern)
                # Look for elements with text matching date pattern
                all_text_elements = frame.locator("//*[contains(text(), '/') and contains(text(), 'hrs')]")
                count = all_text_elements.count()
                
                for i in range(count):
                    try:
                        elem = all_text_elements.nth(i)
                        elem_text = (elem.inner_text(timeout=1000) or "").strip()
                        
                        # Check if this element is within our section
                        # by checking if the section header comes before it
                        elem_parent = elem.locator("xpath=ancestor::*[10]")
                        if elem_parent.count() > 0:
                            parent_text = (elem_parent.first.inner_text(timeout=1000) or "").lower()
                            section_name_lower = section_name.lower()
                            other_section = "mensajes leídos" if "no leídos" in section_name_lower else "mensajes no leídos"
                            
                            # Determine if element belongs to this section
                            section_pos = parent_text.find(section_name_lower)
                            other_pos = parent_text.find(other_section)
                            elem_text_lower = elem_text.lower()[:50]
                            elem_pos = parent_text.find(elem_text_lower[:20])
                            
                            if section_pos >= 0 and elem_pos >= 0:
                                # Check if element is in correct section
                                if other_pos >= 0:
                                    if "no leídos" in section_name_lower:
                                        # For "no leídos", element should be between section_pos and other_pos
                                        if not (section_pos < elem_pos < other_pos):
                                            continue
                                    else:
                                        # For "leídos", element should be after other_pos
                                        if elem_pos < other_pos:
                                            continue
                        
                        messages_found.append((frame, elem, elem_text))
                    except Exception:
                        continue
            except Exception:
                pass
            
            break  # Found our section, stop searching frames
        except Exception:
            continue
    
    if no_info_found and not messages_found:
        return (0, True)
    
    if not messages_found:
        logging.info("%s: no messages found.", section_label)
        return (0, False)
    
    logging.info("%s: found %d message(s).", section_label, len(messages_found))
    
    # Process each message: expand, log, download PDF
    for idx, (frame, elem, label_text) in enumerate(messages_found, start=1):
        safe_label = " ".join(label_text.split()) if label_text else "(sin texto)"
        logging.info("%s mensaje %d: %s", section_label, idx, safe_label)
        
        # Try to click the element or a nearby [+] button to expand
        try:
            # Look for a clickable expand button near this element
            expand_btn = elem.locator("xpath=preceding-sibling::*[contains(@class, 'expand') or contains(text(), '+')]")
            if expand_btn.count() == 0:
                expand_btn = elem.locator("xpath=ancestor::*[1]/*[contains(text(), '+')]")
            if expand_btn.count() == 0:
                # Try clicking the element itself or its parent row
                parent_row = elem.locator("xpath=ancestor::tr[1]")
                if parent_row.count() > 0:
                    try:
                        parent_row.first.click()
                        frame.page.wait_for_timeout(300)
                    except Exception:
                        pass
            else:
                try:
                    expand_btn.first.click()
                    frame.page.wait_for_timeout(300)
                except Exception:
                    pass
        except Exception:
            pass
        
        # Now look for 'aqui' link to download PDF
        try:
            # Look for 'aqui' link in the expanded content
            aqui_link = None
            
            # Search in the parent container for 'aqui' link
            parent_container = elem.locator("xpath=ancestor::*[3]")
            if parent_container.count() > 0:
                aqui_in_container = parent_container.first.locator("a:has-text('aqui')")
                if aqui_in_container.count() > 0:
                    aqui_link = aqui_in_container.first
            
            if aqui_link is None:
                # Broader search: find any visible 'aqui' link
                all_aqui = frame.locator("a:has-text('aqui'):visible")
                if all_aqui.count() > 0:
                    aqui_link = all_aqui.first
            
            if aqui_link:
                with frame.page.expect_download(timeout=10000) as dl_info:
                    aqui_link.click()
                download = dl_info.value
                suggested = download.suggested_filename or f"comunicado_{section_label.replace(' ', '_')}_{idx}.pdf"
                if download_dir:
                    target_path = Path(download_dir) / suggested
                else:
                    target_path = SCRIPT_DIR / suggested
                download.save_as(str(target_path))
                logging.info("%s mensaje %d: PDF descargado en %s", section_label, idx, target_path)
            else:
                logging.info("%s mensaje %d: no 'aqui' link found for download.", section_label, idx)
        except Exception as e:
            logging.warning("%s mensaje %d: error al descargar PDF: %s", section_label, idx, e)
    
    return (len(messages_found), False)


def read_comunicados_table(page) -> None:
    """
    Read 'Mis comunicados' page with two sections:
      - Mensajes no leídos (unread messages)
      - Mensajes leídos (read messages)
    
    For each section:
      - If 'No existe información' is present, log it
      - Else for each message, log the label and download PDF via 'aqui' link
    
    Logs separate summaries for unread and read messages, plus a combined total.
    """
    logging.info("Phase 3: reading 'Mis comunicados' table...")

    # Get download directory from run context
    download_dir = None
    if _run_context:
        download_dir = _run_context.get("download_dir")

    # Process "Mensajes no leídos" section
    logging.info("")
    logging.info("----- Mensajes no leídos -----")
    unread_count, unread_no_info = _process_comunicados_section(
        page, "Mensajes no leídos", "Mis comunicados (no leídos)", download_dir
    )
    
    # Process "Mensajes leídos" section
    logging.info("")
    logging.info("----- Mensajes leídos -----")
    read_count, read_no_info = _process_comunicados_section(
        page, "Mensajes leídos", "Mis comunicados (leídos)", download_dir
    )
    
    # Log summaries
    logging.info("")
    if unread_no_info:
        logging.info(">>> UNREAD MESSAGES IN MIS COMUNICADOS: 0 messages found (No existe información) <<<")
    else:
        logging.info(">>> UNREAD MESSAGES IN MIS COMUNICADOS: %d messages found <<<", unread_count)
    
    if read_no_info:
        logging.info(">>> READ MESSAGES IN MIS COMUNICADOS: 0 messages found (No existe información) <<<")
    else:
        logging.info(">>> READ MESSAGES IN MIS COMUNICADOS: %d messages found <<<", read_count)
    
    logging.info(">>> TOTAL MESSAGES IN MIS COMUNICADOS: %d unread, %d read <<<", unread_count, read_count)


def run_buzon_login(config_path: str | None, mapping_path: str | None, mode: str) -> bool:
    """
    Run Buzón login flow for the given mode ("test-login" or "test-full").
    Currently both modes only perform login; mode is reserved for future extensions.
    """
    cfg = load_buzon_config(config_path)
    mapping_file = _resolve_mapping_path(mapping_path)
    mapping = _load_mapping(mapping_file)

    efirma = {
        "cer_path": cfg["cer_path"],
        "key_path": cfg["key_path"],
        "password": cfg["password"],
    }

    portal_url = cfg["portal_url"] or DEFAULT_PORTAL_URL

    log_file = cfg["raw"].get("log_file") or "buzon_tributario.log"
    _setup_logging(log_file)

    logging.info("")
    logging.info("=== BuzonTributario login (%s) ===", mode)
    logging.info("Using config: %s", cfg["config_path"])
    logging.info("Using mapping: %s", mapping_file)
    logging.info("Portal URL: %s", portal_url)

    # Keep download directory and retry config in run context.
    global _run_context
    if _run_context is None:
        _run_context = {}
    _run_context["download_dir"] = cfg["download_dir"]
    _run_context["section_500_retry_max"] = cfg["section_500_retry_max"]
    _run_context["section_500_retry_wait_seconds"] = cfg["section_500_retry_wait_seconds"]

    retry_max = cfg["section_500_retry_max"]
    retry_wait = cfg["section_500_retry_wait_seconds"]

    for attempt in range(2):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()
                try:
                    login_buzon(
                        page,
                        efirma,
                        mapping,
                        base_url=portal_url,
                        login_max_wait_seconds=cfg["login_max_wait_seconds"],
                    )
                    # Navigation per mode:
                    # - test-login: login only (no navigation)
                    # - test-documentos: Mis expedientes -> Mis documentos (Cobranza -> Líneas de captura -> read table)
                    # - test-notificaciones: Mis expedientes -> Mis notificaciones -> read table
                    # - test-comunicados: Mis expedientes -> Mis comunicados -> read table
                    # - test-full: run all three in sequence; uses _navigate_section_with_retry for 500 error handling
                    if mode in ("test-documentos", "test-full"):
                        _navigate_section_with_retry(
                            page, go_to_mis_documentos, "Mis documentos", retry_max, retry_wait
                        )
                    if mode in ("test-notificaciones", "test-full"):
                        _navigate_section_with_retry(
                            page, go_to_mis_notificaciones, "Mis notificaciones", retry_max, retry_wait
                        )
                        wait_for_notificaciones_loaded(page)
                        read_notificaciones_table(page)
                    if mode in ("test-comunicados", "test-full"):
                        _navigate_section_with_retry(
                            page, go_to_mis_comunicados, "Mis comunicados", retry_max, retry_wait
                        )
                        wait_for_comunicados_loaded(page)
                        read_comunicados_table(page)

                    # Post-run: log final URL and leave browser open briefly for manual inspection.
                    logging.info("")
                    logging.info("===== Section: Inspection =====")
                    page.wait_for_timeout(1000)
                    current_url = page.url or ""
                    logging.info("Post-login URL: %s", current_url)
                    logging.info("Keeping browser open 10 seconds for inspection...")
                    page.wait_for_timeout(10000)
                    logging.info("Inspection period complete; closing browser (with logout if logged in).")
                    success = True
                except KeyboardInterrupt:
                    logging.info("KeyboardInterrupt detected, running cleanup.")
                    _cleanup_on_interrupt(page, context, browser)
                    raise
                finally:
                    # Normal or error exit: attempt logout (if logged in) and close browser.
                    _cleanup_on_interrupt(page, context, browser)
            return success
        except Exception as exc:
            logging.exception("Error during BuzonTributario login: %s", exc)
            print(f"Error during BuzonTributario login: {exc}", file=sys.stderr)
            if attempt == 0:
                logging.info("Closing and retrying once in %s seconds...", RETRY_WAIT_SECONDS)
                time.sleep(RETRY_WAIT_SECONDS)
            else:
                return False
    return False


def _setup_logging(log_file: str) -> None:
    """
    Minimal logging setup for BuzonTributario.
    """
    log_path = Path(log_file)
    if not log_path.is_absolute():
        log_path = SCRIPT_DIR / log_file

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _cleanup_on_interrupt(page, context, browser) -> None:
    """
    Best-effort cleanup when the program ends (normal exit or Ctrl+C).
    If we are logged in, try to click 'Cerrar sesión' before closing browser.
    """
    global _run_context
    logged_in = _run_context.get("logged_in", False) if _run_context else False
    if logged_in:
        logging.info("")
        logging.info("===== Section: Logout =====")
        logging.info("Cleanup: user is logged in, clicking 'Cerrar sesión'...")
        try:
            # Step 1: Click 'Cerrar sesión' across frames.
            logout_selectors = [
                "button:has-text('Cerrar sesión')",
                "a:has-text('Cerrar sesión')",
                "[role='button']:has-text('Cerrar sesión')",
                "text=/Cerrar sesi[oó]n/i",
            ]
            clicked_logout = _try_click(page, logout_selectors)
            if clicked_logout:
                logging.info("Cleanup: 'Cerrar sesión' clicked, checking for survey popup...")
            else:
                logging.warning("Cleanup: could not find 'Cerrar sesión' button.")

            # Step 2: Wait briefly for survey popup to appear.
            page.wait_for_timeout(800)

            # Step 3: Handle optional satisfaction survey popup.
            # Popup text: "¿Desea contestar la encuesta de satisfacción?" with "Si" and "No" buttons.
            survey_handled = False
            for frame in _iter_frames(page):
                try:
                    dialog = frame.locator("text=/encuesta de satisfacci[oó]n/i")
                    if dialog.count() > 0:
                        logging.info("Cleanup: satisfaction survey popup detected, clicking 'No'.")
                        no_selectors = [
                            "button:has-text('No')",
                            "[role='button']:has-text('No')",
                        ]
                        _try_click(page, no_selectors)
                        survey_handled = True
                        break
                except Exception:
                    continue

            if survey_handled:
                logging.info("Cleanup: survey popup dismissed.")

            # Step 4: Wait for logout to complete.
            page.wait_for_timeout(1500)
            logging.info("Cleanup: logout attempt completed.")
        except Exception as e:
            logging.warning("Cleanup: error while trying to logout: %s", e)

    _run_context = None

    for name, obj in (("page", page), ("context", context), ("browser", browser)):
        try:
            if obj is not None:
                obj.close()
        except Exception as e:
            logging.debug("%s close error: %s", name, e)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Login to SAT Buzón using e.firma, reusing sat_declaration_filler login flow."
    )
    parser.add_argument(
        "--config",
        help="Path to config.json (default: script dir/config.json)",
    )
    parser.add_argument(
        "--mapping",
        help="Path to mapping JSON (default: form_field_mapping.json next to sat_declaration_filler.py)",
    )
    parser.add_argument(
        "--test-login",
        action="store_true",
        help="Test only: open SAT Buzón and log in with local .cer/.key and password from config.",
    )
    parser.add_argument(
        "--test-full",
        action="store_true",
        help="Run Mis documentos (Cobranza -> Líneas de captura), Mis notificaciones, and Mis comunicados in sequence after login.",
    )
    parser.add_argument(
        "--test-documentos",
        action="store_true",
        help="After login, go to Mis expedientes -> Mis documentos -> Cobranza -> Líneas de captura and read the table.",
    )
    parser.add_argument(
        "--test-notificaciones",
        action="store_true",
        help="After login, go to Mis expedientes -> Mis notificaciones and read the notifications table.",
    )
    parser.add_argument(
        "--test-comunicados",
        action="store_true",
        help="After login, go to Mis expedientes -> Mis comunicados and read the comunicados table.",
    )

    args = parser.parse_args()

    selected_modes = [
        flag
        for flag, enabled in [
            ("test-login", args.test_login),
            ("test-full", args.test_full),
            ("test-documentos", getattr(args, "test_documentos", False)),
            ("test-notificaciones", getattr(args, "test_notificaciones", False)),
            ("test-comunicados", getattr(args, "test_comunicados", False)),
        ]
        if enabled
    ]

    if not selected_modes:
        parser.print_help(sys.stderr)
        sys.exit(2)

    if len(selected_modes) > 1:
        print("Error: Please specify only one test mode (--test-login, --test-full, --test-documentos, --test-notificaciones, or --test-comunicados).", file=sys.stderr)
        sys.exit(2)

    mode = selected_modes[0]
    success = run_buzon_login(args.config, args.mapping, mode=mode)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

