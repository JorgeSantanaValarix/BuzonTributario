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

    return {
        "portal_url": portal_url,
        "cer_path": cer_path,
        "key_path": key_path,
        "password": password,
        "sat_ui": sat_ui,
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


def login_buzon(page, efirma: dict, mapping: dict, base_url: str = DEFAULT_PORTAL_URL) -> None:
    """
    Minimal login flow for SAT Buzón using e.firma, using selectors from mapping.
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
    page.wait_for_timeout(1000)

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
    # Similar to sat_declaration_filler: poll URL/body until expected pattern or timeout.
    logging.info("Phase 1: [%.2fs] waiting for Buzón post-login page...", _elapsed())
    post_login_timeout_ms = 8000
    poll_ms = 150
    t_end = time.perf_counter() + (post_login_timeout_ms / 1000.0)
    expected_pat = "buzón tributario de"
    while time.perf_counter() < t_end:
        page.wait_for_timeout(poll_ms)
        try:
            url = (page.url or "").lower()
            if "/buzon" in url:
                logging.info("Phase 1: [%.2fs] Buzón URL detected: %s", _elapsed(), url)
                break
            # Fallback: check body text for the header "Buzón Tributario de"
            for frame in _iter_frames(page):
                try:
                    body = (frame.locator("body").inner_text(timeout=500) or "").lower()
                except Exception:
                    continue
                if expected_pat in body:
                    logging.info("Phase 1: [%.2fs] Buzón header detected in body text", _elapsed())
                    t_end = time.perf_counter()  # Force exit
                    break
        except Exception:
            continue


def open_mis_expedientes_menu(page) -> None:
    """
    Open the 'Mis expedientes' dropdown in the Buzón top navigation.
    """
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
    logging.info("Phase 2: navigating to 'Mis notificaciones'...")
    selectors = [
        "a:has-text('Mis notificaciones')",
        "button:has-text('Mis notificaciones')",
        "text=/\\bMis notificaciones\\b/i",
    ]
    if not _try_click(page, selectors):
        raise RuntimeError("Could not find 'Mis notificaciones' option in Buzón")
    page.wait_for_timeout(1000)


def go_to_mis_comunicados(page) -> None:
    """
    From the opened 'Mis expedientes' menu, click 'Mis comunicados'.
    """
    logging.info("Phase 2: navigating to 'Mis comunicados'...")
    selectors = [
        "a:has-text('Mis comunicados')",
        "button:has-text('Mis comunicados')",
        "text=/\\bMis comunicados\\b/i",
    ]
    if not _try_click(page, selectors):
        raise RuntimeError("Could not find 'Mis comunicados' option in Buzón")
    page.wait_for_timeout(1000)


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
        return

    logging.info("Líneas de captura: found %d row(s).", len(rows_data))
    for i, row in enumerate(rows_data, start=1):
        logging.info("Líneas de captura row %d: %s", i, row)


def read_notificaciones_table(page) -> None:
    """
    Read 'Mis notificaciones' table.

    Columns: Folio del acto administrativo, Autoridad emisora, Acto administrativo,
    Fecha de aviso, Aviso, Documento.
    When there is no data:
      - Filters for Autoridad emisora / Acto administrativo only have 'Seleccione'
      - Table body shows 'No se encontraron resultados'
    """
    logging.info("Phase 3: reading 'Mis notificaciones' table...")

    # Check filters first (best-effort, logs only).
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
                logging.info("Filtro '%s' opciones: %s", label_text, options)
        # Only inspect first frame for filters; avoid duplicate logs.
        break

    # Check for 'No se encontraron resultados'.
    for frame in _iter_frames(page):
        try:
            no_results = frame.locator("text=/No se encontraron resultados/i")
            if no_results.count() > 0:
                logging.info("Mis notificaciones: No se encontraron resultados")
                return
        except Exception:
            continue

    # Otherwise, read the table rows similarly to Líneas de captura.
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
        logging.info("Mis notificaciones: table found but no data rows detected.")
        return

    logging.info("Mis notificaciones: found %d row(s).", len(rows_data))
    for i, row in enumerate(rows_data, start=1):
        logging.info("Mis notificaciones row %d: %s", i, row)


def read_comunicados_table(page) -> None:
    """
    Read 'Mis comunicados' table in a generic way:
      - If 'No se encontraron resultados' is present, log it.
      - Else log each row's columns.
    """
    logging.info("Phase 3: reading 'Mis comunicados' table...")

    for frame in _iter_frames(page):
        try:
            no_results = frame.locator("text=/No se encontraron resultados/i")
            if no_results.count() > 0:
                logging.info("Mis comunicados: No se encontraron resultados")
                return
        except Exception:
            continue

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
        logging.info("Mis comunicados: table found but no data rows detected.")
        return

    logging.info("Mis comunicados: found %d row(s).", len(rows_data))
    for i, row in enumerate(rows_data, start=1):
        logging.info("Mis comunicados row %d: %s", i, row)


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

    for attempt in range(2):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context()
                page = context.new_page()
                try:
                    login_buzon(page, efirma, mapping, base_url=portal_url)
                    # Navigation per mode:
                    # - test-login: login only (no navigation)
                    # - test-documentos: Mis expedientes -> Mis documentos (which does
                    #   Cobranza -> Líneas de captura -> read table)
                    # - test-notificaciones: Mis expedientes -> Mis notificaciones -> read table
                    # - test-comunicados: Mis expedientes -> Mis comunicados -> read table
                    # - test-full: run all three in sequence, reusing same session
                    if mode in ("test-documentos", "test-notificaciones", "test-comunicados", "test-full"):
                        open_mis_expedientes_menu(page)
                        if mode in ("test-documentos", "test-full"):
                            go_to_mis_documentos(page)
                        if mode in ("test-notificaciones", "test-full"):
                            go_to_mis_notificaciones(page)
                            read_notificaciones_table(page)
                        if mode in ("test-comunicados", "test-full"):
                            go_to_mis_comunicados(page)
                            read_comunicados_table(page)

                    # Basic post-login sanity check: log current URL, then leave browser
                    # open for 10 seconds for manual inspection before closing.
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
        logging.info("Cleanup: user is logged in, attempting to click 'Cerrar sesión'...")
        try:
            # Try both button and link variants for Cerrar sesión across all frames.
            logout_selectors = [
                "button:has-text('Cerrar sesión')",
                "a:has-text('Cerrar sesión')",
                "[role='button']:has-text('Cerrar sesión')",
            ]
            _try_click(page, logout_selectors)
            # Give SAT a moment to process logout and redirect.
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

