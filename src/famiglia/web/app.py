"""Pannello web di configurazione: chiavi API, utenti e cartella dei documenti."""

from __future__ import annotations

import asyncio
import io
import re
import secrets
from datetime import datetime
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..ai import KINDS, ROLES, SUGGESTED_ADDRESSES, AiError, NotConfigured
from ..auth import set_password, verify_password
from ..calendar import CONSUMER_KEY_MESSAGE, CalendarError
from ..clock import TIMEZONE
from ..documents import DEFAULT_DOCUMENTS_DIR
from ..service import Service
from ..storage import StorageError
from ..users import coordinator_of


MODEL_NAME = re.compile(r"^[\w./:@\-]{1,120}$")


class LoginRequired(Exception):
    pass


def qr_svg(url: str) -> str:
    """QR come SVG inline, senza dimensioni fisse così lo dimensiona il CSS."""
    buffer = io.BytesIO()
    qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=2).save(buffer)
    svg = buffer.getvalue().decode().split("?>", 1)[-1].strip()
    return re.sub(r'width="[^"]*" height="[^"]*"', "", svg, count=1)


def create_app(service: Service, secret_key: str, admin_user: str, cookie_secure: bool = False) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        https_only=cookie_secure,
        same_site="strict",
        max_age=7 * 24 * 3600,
        session_cookie="famiglia_session",
    )
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    templates.env.filters["datetime"] = lambda ts: datetime.fromtimestamp(ts, TIMEZONE).strftime("%d/%m %H:%M")
    settings = service.settings

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired) -> Response:
        return RedirectResponse("/login", status_code=303)

    def require_login(request: Request) -> None:
        if not request.session.get("user"):
            raise LoginRequired()

    def csrf_token(request: Request) -> str:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(32)
        return request.session["csrf"]

    async def checked_form(request: Request) -> dict[str, str]:
        require_login(request)
        form = await request.form()
        sent = form.get("csrf") or request.headers.get("x-csrf-token") or ""
        if not secrets.compare_digest(str(sent), request.session.get("csrf", "")):
            raise HTTPException(status_code=403, detail="Token CSRF non valido, ricarica la pagina")
        return {key: str(value) for key, value in form.items()}

    def flash(request: Request, kind: str, message: str) -> None:
        request.session.setdefault("flash", []).append([kind, message])

    def page(request: Request, name: str, **context) -> Response:
        require_login(request)
        return templates.TemplateResponse(
            request,
            name,
            {
                "csrf": csrf_token(request),
                "flashes": request.session.pop("flash", []),
                "s": settings,
                "service": service,
                "missing": service.missing_for_run(),
                **context,
            },
        )

    def back(path: str) -> RedirectResponse:
        return RedirectResponse(path, status_code=303)

    def secret_field(form: dict, key: str, values: dict) -> None:
        """I segreti non vengono mai mostrati: campo vuoto = lascia invariato."""
        value = form.get(key, "").strip()
        if value:
            values[key] = value

    # --- Accesso -----------------------------------------------------------------

    @app.get("/login")
    async def login_form(request: Request) -> Response:
        return templates.TemplateResponse(request, "login.html", {"csrf": csrf_token(request), "error": None})

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "")):
            raise HTTPException(status_code=403)
        user_ok = secrets.compare_digest(str(form.get("username", "")), admin_user)
        if user_ok and verify_password(settings, str(form.get("password", ""))):
            request.session.clear()
            request.session["user"] = admin_user
            return back("/")
        await asyncio.sleep(1)
        return templates.TemplateResponse(
            request, "login.html", {"csrf": csrf_token(request), "error": "Credenziali non valide"}, status_code=401
        )

    @app.post("/esci")
    async def logout(request: Request) -> Response:
        await checked_form(request)
        request.session.clear()
        return back("/login")

    @app.get("/password")
    async def password_page(request: Request) -> Response:
        return page(request, "password.html", active="password")

    @app.post("/password")
    async def password_save(request: Request) -> Response:
        form = await checked_form(request)
        if not verify_password(settings, form.get("current", "")):
            flash(request, "error", "Password attuale errata")
        elif form.get("new") != form.get("confirm"):
            flash(request, "error", "Le nuove password non coincidono")
        else:
            try:
                set_password(settings, form.get("new", ""))
                flash(request, "ok", "Password aggiornata")
            except ValueError as exc:
                flash(request, "error", str(exc))
        return back("/password")

    # --- Stato -------------------------------------------------------------------

    @app.get("/")
    async def status(request: Request) -> Response:
        return page(
            request,
            "stato.html",
            active="stato",
            activity=service.recent_activity(50),
            users=service.users.all(),
            default_dir=DEFAULT_DOCUMENTS_DIR,
            folder_problem=folder_problem(),
            ai_summary=service.ai.summary(),
        )

    # --- Chiavi API --------------------------------------------------------------

    @app.get("/api")
    async def api_page(request: Request) -> Response:
        return page(request, "api.html", active="api")

    @app.post("/api")
    async def api_save(request: Request) -> Response:
        form = await checked_form(request)
        values: dict[str, str] = {}
        for key in ("bot_token", "composio_api_key"):
            secret_field(form, key, values)
        if values.get("composio_api_key", "").startswith("ck_"):
            flash(request, "error", CONSUMER_KEY_MESSAGE)
            return back("/api")
        settings.update(values)
        flash(request, "ok", "Chiavi salvate")
        return back("/api")

    # --- Modelli IA --------------------------------------------------------------

    def flash_messages(request: Request, messages: list) -> None:
        for text, ok in messages:
            flash(request, "info" if ok is None else ("ok" if ok else "error"), text)

    def find_service(service_id: int):
        found = service.ai_services.get(service_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Servizio non trovato")
        return found

    @app.get("/ia")
    async def ai_page(request: Request) -> Response:
        current = {}
        for role in ROLES:
            current[role] = []
            for backup in (False, True):
                chosen, model = service.ai.slot(role, backup)
                current[role].append(
                    {
                        "backup": backup,
                        "prefix": service.ai.prefix(role, backup),
                        "service": chosen,
                        "model": model,
                        "models": list(chosen.models) if chosen else [],
                    }
                )
        return page(
            request,
            "ia.html",
            active="ia",
            roles=ROLES,
            kinds=KINDS,
            suggested=SUGGESTED_ADDRESSES,
            services=service.ai_services.all(),
            current=current,
        )

    # -- I servizi: nome, tipo, indirizzo e chiave, inseriti a mano dall'admin

    @app.post("/ia/servizi")
    async def ai_service_add(request: Request) -> Response:
        form = await checked_form(request)
        try:
            added = service.ai_services.add(form.get("name", ""), form.get("kind", "openai"), form.get("base_url", ""), form.get("api_key", ""))
        except ValueError as exc:
            flash(request, "error", str(exc))
            return back("/ia")
        flash(request, "ok", f"Servizio «{added.name}» aggiunto")
        # Se una funzione non ha ancora un servizio, parte con questo: basta il primo inserimento per essere operativi.
        assigned = {f"ai_{role}_service": str(added.id) for role in ROLES if service.ai.slot(role)[0] is None}
        if assigned:
            settings.update({**assigned, **{key.replace("_service", "_model"): "" for key in assigned}})
            flash(request, "info", f"«{added.name}» è ora il servizio principale per: " + ", ".join(ROLES[k.split("_")[1]] for k in assigned))
        flash_messages(request, await service.ai.refresh_and_pick())
        return back("/ia")

    @app.get("/ia/servizi/{service_id}")
    async def ai_service_page(request: Request, service_id: int) -> Response:
        require_login(request)
        return page(request, "ia_servizio.html", active="ia", target=find_service(service_id), kinds=KINDS, suggested=SUGGESTED_ADDRESSES)

    @app.post("/ia/servizi/{service_id}")
    async def ai_service_save(request: Request, service_id: int) -> Response:
        form = await checked_form(request)
        find_service(service_id)
        try:
            saved = service.ai_services.update(service_id, form.get("name", ""), form.get("kind", "openai"), form.get("base_url", ""), form.get("api_key", ""))
        except ValueError as exc:
            flash(request, "error", str(exc))
            return back(f"/ia/servizi/{service_id}")
        flash(request, "ok", f"Servizio «{saved.name}» aggiornato")
        flash_messages(request, await service.ai.refresh_and_pick())
        return back("/ia")

    @app.post("/ia/servizi/{service_id}/modelli")
    async def ai_service_models(request: Request, service_id: int) -> Response:
        """Chiede al servizio i modelli che ha: serve anche a verificare che indirizzo e chiave siano giusti."""
        await checked_form(request)
        target = find_service(service_id)
        try:
            ids = await service.ai.refresh_models(service_id)
            flash(request, "ok", f"{target.name}: {len(ids)} modelli trovati")
        except NotConfigured:
            flash(request, "info", f"{target.name}: chiave non impostata, elenco dei modelli non caricato.")
        except AiError as exc:
            flash(request, "error", f"{target.name}: {exc.user_message}")
        return back("/ia")

    @app.post("/ia/servizi/{service_id}/elimina")
    async def ai_service_remove(request: Request, service_id: int) -> Response:
        await checked_form(request)
        removed = find_service(service_id)
        service.ai_services.remove(service_id)  # le funzioni che lo usavano restano senza servizio
        flash(request, "ok", f"Servizio «{removed.name}» rimosso")
        return back("/ia")

    # -- I modelli da usare: per ogni funzione uno principale e uno di riserva

    @app.post("/ia")
    async def ai_save(request: Request) -> Response:
        """Salva le scelte, poi carica da solo i modelli dei servizi scelti e sceglie dove manca."""
        form = await checked_form(request)
        values: dict[str, str] = {}
        try:
            context_chars = int(form.get("ai_context_chars", "").strip() or settings.get("ai_context_chars"))
        except ValueError:
            context_chars = 0
        if not 2000 <= context_chars <= 200000:
            flash(request, "error", "Dati per domanda: inserisci un numero di caratteri tra 2000 e 200000")
            return back("/ia")
        values["ai_context_chars"] = str(context_chars)
        for role in ROLES:
            for backup in (False, True):
                prefix = service.ai.prefix(role, backup)
                raw = form.get(f"{prefix}_service", "").strip()
                chosen = service.ai_services.get(int(raw)) if raw.isdigit() else None
                if raw and chosen is None:
                    flash(request, "error", "Servizio non valido")
                    return back("/ia")
                # Cambiando servizio, il modello scelto prima non vale più: si lascia scegliere in automatico.
                model = ""
                if chosen is not None and str(chosen.id) == settings.get(f"{prefix}_service"):
                    model = form.get(f"{prefix}_model_manual", "").strip() or form.get(f"{prefix}_model_select", "").strip()
                if model and not MODEL_NAME.match(model):
                    flash(request, "error", f"«{ROLES[role]}»: il nome del modello contiene caratteri non validi")
                    return back("/ia")
                values[f"{prefix}_service"], values[f"{prefix}_model"] = (str(chosen.id) if chosen else ""), model
        settings.update(values)
        flash_messages(request, await service.ai.refresh_and_pick())
        return back("/ia")

    @app.post("/ia/auto")
    async def ai_repick(request: Request) -> Response:
        """Scelta automatica da capo: utile se un modello scelto prima non legge le immagini."""
        await checked_form(request)
        flash_messages(request, await service.ai.repick())
        return back("/ia")

    @app.post("/ia/prova/{role}")
    async def ai_probe(request: Request, role: str) -> Response:
        await checked_form(request)
        if role not in ROLES:
            raise HTTPException(status_code=404)
        for label, ok, detail in await service.ai.probe(role):
            flash(request, "ok" if ok else "error", f"{label}: {detail}")
        return back("/ia")

    # --- Utenti ------------------------------------------------------------------

    def error_text(exc: BaseException) -> str:
        """Messaggio + dettaglio tecnico: la pagina è solo per l'amministratore."""
        if isinstance(exc, CalendarError) and str(exc) != exc.user_message:
            return f"{exc.user_message} ({exc})"
        return exc.user_message if isinstance(exc, CalendarError) else f"{type(exc).__name__}: {exc}"

    def folder_problem() -> str:
        """Se la cartella scelta non è utilizzabile: il motivo. Vuoto se va bene o se si usa la predefinita."""
        chosen = settings.get("documents_dir")
        if not chosen or not service.storage.available:
            return ""
        try:
            service.storage.check_writable(chosen)
        except StorageError as exc:
            return str(exc)
        return ""

    def find_user(user_id: int):
        user = service.users.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="Utente non trovato")
        return user

    @app.get("/utenti")
    async def users_page(request: Request) -> Response:
        require_login(request)
        users = service.users.all()
        states: dict[int, dict] = {}
        if service.calendar.enabled:
            # Un controllo per utente, così un errore su uno non oscura gli altri e mostra la causa vera.
            results = await asyncio.gather(*(service.calendar.is_connected(u) for u in users), return_exceptions=True)
            for user, result in zip(users, results):
                if isinstance(result, BaseException):
                    states[user.id] = {"state": "error", "detail": error_text(result)}
                else:
                    states[user.id] = {"state": "ok" if result else "no", "detail": ""}
        return page(
            request,
            "utenti.html",
            active="utenti",
            users=users,
            states=states,
            calendar_enabled=service.calendar.enabled,
            coordinator=coordinator_of(settings, service.users),
        )

    @app.post("/utenti/{user_id}/aggiorna")
    async def calendar_refresh(request: Request, user_id: int) -> Response:
        """Ricontrolla ora il collegamento Google di questa persona."""
        await checked_form(request)
        user = find_user(user_id)
        try:
            connected = await service.calendar.is_connected(user)
        except Exception as exc:  # noqa: BLE001 - qualunque errore va mostrato, non nascosto
            flash(request, "error", f"{user.name}: non riesco a verificare. {error_text(exc)}")
        else:
            if connected:
                flash(request, "ok", f"{user.name}: Google Calendar collegato")
            else:
                flash(request, "info", f"{user.name}: Google Calendar non ancora collegato. Premi Collega.")
        return back("/utenti")

    @app.post("/utenti")
    async def users_add(request: Request) -> Response:
        form = await checked_form(request)
        try:
            telegram_id = int(form.get("telegram_id", "").strip())
        except ValueError:
            flash(request, "error", "L'ID Telegram deve essere un numero")
            return back("/utenti")
        try:
            user = service.users.add(
                telegram_id, form.get("name", ""), form.get("role", ""), form.get("first_name", ""), form.get("last_name", "")
            )
            flash(request, "ok", f"{user.name} aggiunto")
        except ValueError as exc:
            flash(request, "error", str(exc))
        return back("/utenti")

    @app.get("/utenti/{user_id}/modifica")
    async def users_edit_page(request: Request, user_id: int) -> Response:
        require_login(request)
        return page(request, "modifica.html", active="utenti", target=find_user(user_id))

    @app.post("/utenti/{user_id}/modifica")
    async def users_edit_save(request: Request, user_id: int) -> Response:
        form = await checked_form(request)
        find_user(user_id)
        try:
            user = service.users.update(
                user_id, form.get("name", ""), form.get("role", ""), form.get("first_name", ""), form.get("last_name", "")
            )
        except ValueError as exc:
            flash(request, "error", str(exc))
            return back(f"/utenti/{user_id}/modifica")
        flash(request, "ok", f"{user.name}: dati aggiornati")
        return back("/utenti")

    @app.post("/utenti/{user_id}/elimina")
    async def users_remove(request: Request, user_id: int) -> Response:
        await checked_form(request)
        service.users.remove(user_id)
        if settings.get("coordinator_user_id") == str(user_id):
            settings.update({"coordinator_user_id": ""})  # senza l'utente non c'è più un coordinatore
        flash(request, "ok", "Utente rimosso, con i suoi referti e appuntamenti salvati")
        return back("/utenti")

    @app.post("/utenti/coordinatore")
    async def coordinator_save(request: Request) -> Response:
        form = await checked_form(request)
        raw = form.get("user_id", "").strip()
        if not raw:
            settings.update({"coordinator_user_id": ""})
            flash(request, "ok", "Nessun coordinatore: ognuno riceve le visite solo sul proprio calendario")
            return back("/utenti")
        user = service.users.get(int(raw)) if raw.isdigit() else None
        if user is None:
            flash(request, "error", "Utente non valido")
            return back("/utenti")
        settings.update({"coordinator_user_id": str(user.id)})
        flash(request, "ok", f"{user.name} riceverà sul suo calendario le visite di tutta la famiglia")
        return back("/utenti")

    @app.post("/utenti/{user_id}/collega")
    async def calendar_connect(request: Request, user_id: int) -> Response:
        await checked_form(request)
        user = find_user(user_id)
        try:
            link = await service.calendar.connect_link(user)
        except CalendarError as exc:
            flash(request, "error", error_text(exc))
            return back("/utenti")
        return page(request, "collega.html", active="utenti", target=user, link=link, qr=qr_svg(link))

    @app.get("/utenti/{user_id}/calendario")
    async def calendar_page(request: Request, user_id: int) -> Response:
        require_login(request)
        user = find_user(user_id)
        try:
            calendars = await service.calendar.list_calendars(user)
        except CalendarError as exc:
            flash(request, "error", error_text(exc))
            return back("/utenti")
        return page(request, "calendario.html", active="utenti", target=user, calendars=calendars)

    @app.post("/utenti/{user_id}/calendario")
    async def calendar_save(request: Request, user_id: int) -> Response:
        form = await checked_form(request)
        user = find_user(user_id)
        chosen = form.get("calendar_id", "").strip()
        try:
            valid = {c.id for c in await service.calendar.list_calendars(user)}
        except CalendarError as exc:
            flash(request, "error", error_text(exc))
            return back("/utenti")
        if chosen not in valid:
            flash(request, "error", "Calendario non valido")
            return back(f"/utenti/{user_id}/calendario")
        service.users.set_calendar(user.id, chosen)
        flash(request, "ok", f"Le visite di {user.name} andranno sul calendario scelto")
        return back("/utenti")

    # --- Cartella dei documenti --------------------------------------------------

    def breadcrumbs(relative: str) -> list[tuple[str, str]]:
        crumbs, current = [("Radice", ".")], []
        for part in [p for p in relative.split("/") if p and p != "."]:
            current.append(part)
            crumbs.append((part, "/".join(current)))
        return crumbs

    @app.get("/cartella")
    async def folder_page(request: Request, p: str = ".") -> Response:
        storage = service.storage
        error, folders, here = "", [], "."
        if not storage.available:
            error = "La cartella radice non è montata nel container: controlla STORAGE_ROOT nello stack."
        else:
            try:
                here = storage.relative(storage.resolve(p))
                folders = storage.subfolders(here)
            except StorageError as exc:
                error = str(exc)
        return page(
            request,
            "cartella.html",
            active="cartella",
            here=here,
            folders=folders,
            crumbs=breadcrumbs(here),
            storage_error=error,
            root=str(storage.root),
            default_dir=DEFAULT_DOCUMENTS_DIR,
            folder_problem=folder_problem(),
        )

    @app.post("/cartella/nuova")
    async def folder_new(request: Request) -> Response:
        form = await checked_form(request)
        here = form.get("here", ".")
        try:
            created = service.storage.make_folder(here, form.get("name", ""))
            return back(f"/cartella?p={created}")
        except StorageError as exc:
            flash(request, "error", str(exc))
            return back(f"/cartella?p={here}")

    @app.post("/cartella/predefinita")
    async def folder_default(request: Request) -> Response:
        await checked_form(request)
        settings.update({"documents_dir": ""})
        flash(request, "ok", f"Si torna alla cartella predefinita: {DEFAULT_DOCUMENTS_DIR}")
        return back("/cartella")

    @app.post("/cartella/scegli")
    async def folder_choose(request: Request) -> Response:
        form = await checked_form(request)
        here = form.get("here", ".")
        try:
            chosen = service.storage.relative(service.storage.check_writable(here))
        except StorageError as exc:
            flash(request, "error", str(exc))
            return back(f"/cartella?p={here}")
        settings.update({"documents_dir": chosen})
        flash(request, "ok", f"Cartella dei documenti impostata: {'Radice' if chosen == '.' else chosen}")
        return back("/cartella")

    return app
