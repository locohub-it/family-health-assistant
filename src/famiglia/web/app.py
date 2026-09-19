"""Pannello web di configurazione: chiavi API, utenti e cartella dei documenti."""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..auth import set_password, verify_password
from ..clock import TIMEZONE
from ..service import Service
from ..storage import StorageError


class LoginRequired(Exception):
    pass


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
                "missing": settings.missing_for_run(),
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
        return page(request, "stato.html", active="stato", activity=service.recent_activity(50), users=service.users.all())

    # --- Chiavi API --------------------------------------------------------------

    @app.get("/api")
    async def api_page(request: Request) -> Response:
        return page(request, "api.html", active="api")

    @app.post("/api")
    async def api_save(request: Request) -> Response:
        form = await checked_form(request)
        values: dict[str, str] = {}
        for key in ("bot_token", "gemini_api_key", "composio_api_key"):
            secret_field(form, key, values)
        model = form.get("gemini_model", "").strip()
        if not model or len(model) > 80 or " " in model:
            flash(request, "error", "Modello Gemini: inserisci un nome valido, ad esempio gemini-3.8-flash")
            return back("/api")
        values["gemini_model"] = model
        settings.update(values)
        flash(request, "ok", "Chiavi salvate")
        return back("/api")

    # --- Utenti ------------------------------------------------------------------

    @app.get("/utenti")
    async def users_page(request: Request) -> Response:
        return page(request, "utenti.html", active="utenti", users=service.users.all())

    @app.post("/utenti")
    async def users_add(request: Request) -> Response:
        form = await checked_form(request)
        try:
            telegram_id = int(form.get("telegram_id", "").strip())
        except ValueError:
            flash(request, "error", "L'ID Telegram deve essere un numero")
            return back("/utenti")
        try:
            user = service.users.add(telegram_id, form.get("name", ""), form.get("role", ""))
            flash(request, "ok", f"{user.name} aggiunto")
        except ValueError as exc:
            flash(request, "error", str(exc))
        return back("/utenti")

    @app.post("/utenti/{user_id}/elimina")
    async def users_remove(request: Request, user_id: int) -> Response:
        await checked_form(request)
        service.users.remove(user_id)
        flash(request, "ok", "Utente rimosso, con i suoi referti e appuntamenti salvati")
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
