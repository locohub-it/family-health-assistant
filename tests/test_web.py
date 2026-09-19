import re

import pytest
from fastapi.testclient import TestClient

from famiglia.auth import set_password
from famiglia.web.app import create_app

PASSWORD = "password-lunga-123"
PAGES = ("/", "/api", "/ia", "/utenti", "/cartella", "/password")


@pytest.fixture
def client(service):
    set_password(service.settings, PASSWORD)
    return TestClient(create_app(service, secret_key="chiave-di-test", admin_user="admin"))


def csrf(client, path="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def login(client):
    token = csrf(client)
    response = client.post("/login", data={"csrf": token, "username": "admin", "password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    return csrf(client, "/")


def test_pages_require_login(client):
    for path in PAGES:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"


def test_wrong_password_is_rejected(client):
    token = csrf(client)
    assert client.post("/login", data={"csrf": token, "username": "admin", "password": "sbagliata"}).status_code == 401


def test_all_pages_render_after_login(client):
    login(client)
    for path in PAGES:
        assert client.get(path).status_code == 200


def test_post_without_csrf_is_forbidden(client):
    login(client)
    assert client.post("/utenti", data={"name": "Mario", "telegram_id": "1"}).status_code == 403


def test_save_telegram_and_composio_keys_and_blank_secret_is_kept(client, service):
    token = login(client)
    client.post("/api", data={"csrf": token, "bot_token": "1:abc", "composio_api_key": "ak_uno"})
    client.post("/api", data={"csrf": token, "bot_token": "", "composio_api_key": ""})
    assert service.settings.get("bot_token") == "1:abc" and service.settings.get("composio_api_key") == "ak_uno"


def test_gemini_key_and_models_are_saved_from_the_ai_page(gemini_client):
    client, service = gemini_client
    token = login(client)
    base = {"csrf": token, "ai_docs_provider": "gemini", "ai_chat_provider": "gemini"}
    response = client.post("/ia", data={**base, "gemini_api_key": "AIza-1", "gemini_model": "gemini-3.8-flash", "gemini_fallback_model": "gemini-3.5-flash-lite"})
    assert "Gemini, modello gemini-3.8-flash (2 disponibili)" in response.text  # la lista si carica da sola al salvataggio
    assert service.ai.cached_models("gemini") == ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
    html = client.get("/ia").text
    assert '<select id="gemini_model"' in html and '<select id="gemini_fallback_model"' in html  # ora sono menu, non caselle di testo
    client.post("/ia", data={**base, "gemini_api_key": "", "gemini_model": "gemini-3.5-flash-lite", "gemini_fallback_model": ""})
    assert service.settings.get("gemini_api_key") == "AIza-1"  # lasciata vuota: invariata
    assert service.settings.get("gemini_model") == "gemini-3.5-flash-lite" and service.settings.get("gemini_fallback_model") == ""


def test_gemini_is_no_longer_on_the_api_keys_page(client, service):
    login(client)
    html = client.get("/api").text
    assert "gemini_api_key" not in html and "Modelli IA" in html and "ricerca web" not in html.lower()
    ia = client.get("/ia").text
    assert 'name="gemini_api_key"' in ia and 'name="gemini_model"' in ia and 'name="gemini_fallback_model"' in ia


def test_secrets_are_never_rendered(client, service):
    service.settings.update({"gemini_api_key": "AIza-non-mostrare"})
    login(client)
    assert "AIza-non-mostrare" not in client.get("/api").text


def test_add_and_remove_user(client, service):
    token = login(client)
    client.post("/utenti", data={"csrf": token, "name": "Mario", "telegram_id": "111", "role": "papà"})
    user = service.users.by_telegram_id(111)
    assert user and user.role == "papà"
    assert "Mario" in client.get("/utenti").text
    client.post(f"/utenti/{user.id}/elimina", data={"csrf": token})
    assert service.users.by_telegram_id(111) is None


def test_bad_telegram_id_shows_an_error(client, service):
    token = login(client)
    response = client.post("/utenti", data={"csrf": token, "name": "Mario", "telegram_id": "abc"})
    assert service.users.all() == []
    # il POST segue il redirect: il messaggio compare nella pagina di arrivo
    assert "deve essere un numero" in response.text


def test_browse_create_and_choose_folder(client, service, root):
    token = login(client)
    assert "Documenti" in client.get("/cartella").text
    client.post("/cartella/nuova", data={"csrf": token, "here": "Documenti", "name": "Salute"})
    assert (root / "Documenti" / "Salute").is_dir()
    client.post("/cartella/scegli", data={"csrf": token, "here": "Documenti/Salute"})
    assert service.settings.get("documents_dir") == "Documenti/Salute"


def test_folder_page_cannot_browse_outside_root(client):
    login(client)
    response = client.get("/cartella?p=../..")
    assert response.status_code == 200
    assert "fuori dalla cartella radice" in response.text


def test_cannot_choose_folder_outside_root(client, service):
    token = login(client)
    client.post("/cartella/scegli", data={"csrf": token, "here": "../.."})
    assert service.settings.get("documents_dir") == ""


def test_missing_root_is_reported(tmp_path):
    from famiglia.service import Service

    service = Service(tmp_path / "data", "chiave-di-test", tmp_path / "non-montata")
    set_password(service.settings, PASSWORD)
    client = TestClient(create_app(service, secret_key="chiave-di-test", admin_user="admin"))
    login(client)
    assert "non è montata" in client.get("/cartella").text


# --- Google Calendar -----------------------------------------------------------


@pytest.fixture
def cal_client(tmp_path, root):
    """Pannello con Composio finto: Mario ha il calendario collegato, Anna no."""
    from famiglia.service import Service
    from helpers import FakeComposio

    composio = FakeComposio(connected={"famiglia-111"})
    service = Service(tmp_path / "data", "chiave-di-test", root, composio_factory=lambda key: composio)
    service.settings.update({"composio_api_key": "ak_test"})
    service.mario = service.users.add(111, "Mario")
    service.anna = service.users.add(222, "Anna")
    set_password(service.settings, PASSWORD)
    client = TestClient(create_app(service, secret_key="chiave-di-test", admin_user="admin"))
    return client, service, composio


def test_users_page_shows_who_is_connected(cal_client):
    client, service, _ = cal_client
    login(client)
    html = client.get("/utenti").text
    assert html.count("collegato</span>") == 2  # «collegato» e «non collegato»
    assert f"/utenti/{service.mario.id}/calendario" in html and f"/utenti/{service.anna.id}/collega" in html
    assert f"/utenti/{service.mario.id}/collega" not in html
    assert html.count("↻ Aggiorna") == 2  # un pulsante per ogni utente


def test_users_page_without_composio_key_explains_it(client, service):
    login(client)
    service.users.add(111, "Mario")
    assert "serve la chiave Composio" in client.get("/utenti").text


def test_users_page_survives_composio_being_down_and_shows_the_real_reason(cal_client):
    client, service, composio = cal_client
    composio.connected_accounts.list = lambda **kw: (_ for _ in ()).throw(ConnectionError("Composio giù"))
    login(client)
    response = client.get("/utenti")
    assert response.status_code == 200 and "Anna" in response.text and "Mario" in response.text
    assert response.text.count("non verificabile") == 2
    assert "ConnectionError: Composio giù" in response.text  # la causa vera, non un messaggio generico


def test_refresh_button_rechecks_and_picks_up_a_connection_made_in_the_meantime(cal_client):
    client, service, composio = cal_client
    token = login(client)
    assert "non ancora collegato" in client.post(f"/utenti/{service.anna.id}/aggiorna", data={"csrf": token}).text
    composio.connected.add("famiglia-222")  # Anna collega il suo Google
    response = client.post(f"/utenti/{service.anna.id}/aggiorna", data={"csrf": token})
    assert "Anna: Google Calendar collegato" in response.text
    assert client.get("/utenti").text.count("non collegato") == 0


def test_refresh_reports_the_reason_when_the_check_fails(cal_client):
    client, service, composio = cal_client
    token = login(client)
    composio.connected_accounts.list = lambda **kw: (_ for _ in ()).throw(ConnectionError("Composio giù"))
    response = client.post(f"/utenti/{service.mario.id}/aggiorna", data={"csrf": token})
    assert "Mario: non riesco a verificare" in response.text and "Composio giù" in response.text


def test_refresh_requires_login_and_csrf(cal_client):
    client, service, _ = cal_client
    assert client.post(f"/utenti/{service.anna.id}/aggiorna", follow_redirects=False).status_code == 303
    login(client)
    assert client.post(f"/utenti/{service.anna.id}/aggiorna").status_code == 403


# --- Cartella predefinita ------------------------------------------------------


def test_default_folder_is_shown_when_none_is_chosen(client, service):
    login(client)
    assert "predefinita" in client.get("/cartella").text and "Documenti" in client.get("/cartella").text
    assert "Cartella dei documenti" not in client.get("/").text  # non è più un requisito per partire


def test_back_to_the_default_folder(client, service):
    token = login(client)
    service.settings.update({"documents_dir": "Foto"})
    assert "Torna alla predefinita" in client.get("/cartella").text
    client.post("/cartella/predefinita", data={"csrf": token})
    assert service.settings.get("documents_dir") == ""


def test_an_unusable_chosen_folder_is_flagged_in_the_panel(client, service, root):
    login(client)
    (root / "rotta").write_text("file, non cartella")
    service.settings.update({"documents_dir": "rotta"})
    for page in ("/cartella", "/"):
        html = client.get(page).text
        assert "utilizzabile" in html and "Documenti" in html


def test_gemini_model_names_are_validated(client, service):
    token = login(client)
    base = {"csrf": token, "ai_docs_provider": "gemini", "ai_chat_provider": "gemini"}
    for bad in ({"gemini_model": "nome con spazi"}, {"gemini_model": "gemini-3.8-flash", "gemini_fallback_model": "x;y z"}):
        assert "Modello Gemini: inserisci un nome valido" in client.post("/ia", data={**base, **bad}).text
    assert service.settings.get("gemini_model") == "gemini-3.8-flash"


def test_connect_page_shows_link_and_qr(cal_client):
    client, service, composio = cal_client
    token = login(client)
    response = client.post(f"/utenti/{service.anna.id}/collega", data={"csrf": token})
    assert response.status_code == 200
    assert "https://connect.composio.dev/link/ln_abc" in response.text and "<svg" in response.text
    assert composio.authorized == ["famiglia-222:googlecalendar"]


def test_connect_requires_csrf_and_login(cal_client):
    client, service, _ = cal_client
    assert client.post(f"/utenti/{service.anna.id}/collega", follow_redirects=False).status_code == 303
    login(client)
    assert client.post(f"/utenti/{service.anna.id}/collega").status_code == 403
    assert client.post("/utenti/999/collega", data={"csrf": csrf(client, "/")}).status_code == 404


def test_choose_calendar(cal_client):
    client, service, _ = cal_client
    token = login(client)
    page = client.get(f"/utenti/{service.mario.id}/calendario").text
    assert "Famiglia" in page and "principale" in page
    client.post(f"/utenti/{service.mario.id}/calendario", data={"csrf": token, "calendar_id": "famiglia@group.calendar.google.com"})
    assert service.users.get(service.mario.id).calendar_id == "famiglia@group.calendar.google.com"


def test_cannot_choose_a_calendar_that_is_not_in_the_list(cal_client):
    client, service, _ = cal_client
    token = login(client)
    response = client.post(f"/utenti/{service.mario.id}/calendario", data={"csrf": token, "calendar_id": "altrui@example.com"})
    assert "Calendario non valido" in response.text
    assert service.users.get(service.mario.id).calendar_id == "primary"


def test_consumer_key_ck_is_refused_with_an_explanation(client, service):
    token = login(client)
    response = client.post("/api", data={"csrf": token, "gemini_model": "gemini-3.8-flash", "composio_api_key": "ck_abcdef123456"})
    assert "Project API key" in response.text and "ak_" in response.text
    assert service.settings.get("composio_api_key") == ""


def test_project_key_ak_is_accepted(client, service):
    token = login(client)
    client.post("/api", data={"csrf": token, "gemini_model": "gemini-3.8-flash", "composio_api_key": "ak_abcdef123456"})
    assert service.settings.get("composio_api_key") == "ak_abcdef123456"


# --- Nome e cognome reali ------------------------------------------------------


def test_add_user_with_real_name_shows_it_in_the_list(client, service):
    token = login(client)
    client.post("/utenti", data={"csrf": token, "first_name": "Mario", "last_name": "Rossi", "telegram_id": "111", "name": "Papà", "role": "papà"})
    user = service.users.by_telegram_id(111)
    assert (user.first_name, user.last_name, user.name) == ("Mario", "Rossi", "Papà")
    html = client.get("/utenti").text
    assert "Mario Rossi" in html and "il bot lo chiama «Papà»" in html


def test_user_without_real_name_is_flagged_with_a_link_to_add_it(client, service):
    login(client)
    user = service.users.add(111, "Mario")
    html = client.get("/utenti").text
    assert "manca il nome reale" in html and f"/utenti/{user.id}/modifica" in html


def test_edit_user_page_and_save(client, service):
    token = login(client)
    user = service.users.add(111, "Mario")
    page = client.get(f"/utenti/{user.id}/modifica").text
    assert 'value="Mario"' in page and "111" in page
    client.post(f"/utenti/{user.id}/modifica", data={"csrf": token, "first_name": "Mario", "last_name": "Rossi", "name": "Papà", "role": "papà"})
    updated = service.users.get(user.id)
    assert (updated.full_name, updated.name, updated.role, updated.telegram_id) == ("Mario Rossi", "Papà", "papà", 111)


def test_edit_with_invalid_data_shows_the_error_and_changes_nothing(client, service):
    token = login(client)
    user = service.users.add(111, "Mario")
    response = client.post(f"/utenti/{user.id}/modifica", data={"csrf": token, "name": "", "first_name": "", "last_name": ""})
    assert "almeno il nome" in response.text and service.users.get(user.id).name == "Mario"


def test_edit_requires_login_and_csrf_and_a_real_user(client, service):
    user = service.users.add(111, "Mario")
    assert client.get(f"/utenti/{user.id}/modifica", follow_redirects=False).status_code == 303
    login(client)
    assert client.post(f"/utenti/{user.id}/modifica", data={"name": "X"}).status_code == 403
    assert client.get("/utenti/999/modifica").status_code == 404


# --- Prova Gemini e ricerca web ------------------------------------------------


def test_gemini_probe_button_shows_each_model_with_the_real_reason(tmp_path, root):
    import httpx

    from famiglia.service import Service

    def respond(request):
        if "lite" in request.url.path:
            return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota"}})
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}]})

    service = Service(tmp_path / "data", "chiave-di-test", root, gemini_http=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    service.settings.update({"gemini_api_key": "AIza-test"})
    set_password(service.settings, PASSWORD)
    client = TestClient(create_app(service, secret_key="chiave-di-test", admin_user="admin"))
    token = login(client)
    html = client.post("/ia/prova-gemini", data={"csrf": token}).text
    assert "Modello principale (gemini-3.8-flash): funziona" in html
    assert "Modello di riserva (gemini-3.5-flash-lite): 429" in html and "exceeded your current quota" in html
    assert "Ricerca web" not in html


def test_gemini_probe_without_a_key_says_so_and_requires_login_and_csrf(client):
    assert client.post("/ia/prova-gemini", follow_redirects=False).status_code == 303
    token = login(client)
    assert client.post("/ia/prova-gemini").status_code == 403
    assert "manca la chiave Gemini" in client.post("/ia/prova-gemini", data={"csrf": token}).text


def test_there_is_no_web_search_option_anywhere(client, service):
    login(client)
    for page in ("/api", "/ia", "/"):
        assert "ricerca web" not in client.get(page).text.lower()
    from famiglia.settings import DEFAULTS

    assert "consult_web_search" not in DEFAULTS


# --- Coordinatore --------------------------------------------------------------


def test_choose_and_clear_the_coordinator(cal_client):
    client, service, _ = cal_client
    token = login(client)
    assert 'value="">Nessuno' in client.get("/utenti").text
    response = client.post("/utenti/coordinatore", data={"csrf": token, "user_id": str(service.anna.id)})
    assert "Anna riceverà sul suo calendario le visite di tutta la famiglia" in response.text
    assert service.settings.get("coordinator_user_id") == str(service.anna.id)
    html = client.get("/utenti").text
    assert 'coordinatore</span>' in html and f'value="{service.anna.id}" selected' in html
    client.post("/utenti/coordinatore", data={"csrf": token, "user_id": ""})
    assert service.settings.get("coordinator_user_id") == ""


@pytest.mark.parametrize("bad", ["999", "abc", "-1"])
def test_invalid_coordinator_is_refused(cal_client, bad):
    client, service, _ = cal_client
    token = login(client)
    assert "Utente non valido" in client.post("/utenti/coordinatore", data={"csrf": token, "user_id": bad}).text
    assert service.settings.get("coordinator_user_id") == ""


def test_coordinator_without_a_connected_calendar_is_flagged(cal_client):
    client, service, _ = cal_client
    token = login(client)
    client.post("/utenti/coordinatore", data={"csrf": token, "user_id": str(service.anna.id)})  # Anna non è collegata
    assert "non risulta collegato" in client.get("/utenti").text
    client.post("/utenti/coordinatore", data={"csrf": token, "user_id": str(service.mario.id)})  # Mario sì
    assert "non risulta collegato" not in client.get("/utenti").text


def test_removing_the_coordinator_clears_the_setting(cal_client):
    client, service, _ = cal_client
    token = login(client)
    client.post("/utenti/coordinatore", data={"csrf": token, "user_id": str(service.anna.id)})
    client.post(f"/utenti/{service.anna.id}/elimina", data={"csrf": token})
    assert service.settings.get("coordinator_user_id") == ""


def test_coordinator_route_requires_login_and_csrf(cal_client):
    client, service, _ = cal_client
    assert client.post("/utenti/coordinatore", data={"user_id": "1"}, follow_redirects=False).status_code == 303
    login(client)
    assert client.post("/utenti/coordinatore", data={"user_id": "1"}).status_code == 403


# --- Modelli IA ----------------------------------------------------------------

GEMINI_LIST = {"models": [
    {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
]}


@pytest.fixture
def gemini_client(tmp_path, root):
    """Pannello con Gemini simulato: nessuna richiesta vera a Google."""
    import httpx

    from famiglia.service import Service

    service = Service(
        tmp_path / "data", "chiave-di-test", root,
        gemini_http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=GEMINI_LIST))),
    )
    set_password(service.settings, PASSWORD)
    return TestClient(create_app(service, secret_key="chiave-di-test", admin_user="admin")), service


GROQ_LIST = {"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "meta-llama/llama-4-scout-17b-16e-instruct"}, {"id": "whisper-large-v3"}, {"id": "playai-tts"}]}
DEEPSEEK_LIST = {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}


@pytest.fixture
def ia_client(tmp_path, root):
    """Pannello con Groq e DeepSeek finti: l'elenco dei modelli dipende dal servizio interpellato."""
    import httpx

    from famiglia.service import Service

    def respond(request):
        if request.url.path.endswith("/models"):
            body = GROQ_LIST if "groq" in request.url.host else DEEPSEEK_LIST
            return httpx.Response(401, json={"error": {"message": "Invalid API Key"}}) if request.headers["authorization"].endswith("SBAGLIATA") else httpx.Response(200, json=body)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    service = Service(tmp_path / "data", "chiave-di-test", root, ai_http=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    set_password(service.settings, PASSWORD)
    client = TestClient(create_app(service, secret_key="chiave-di-test", admin_user="admin"))
    return client, service


def test_ai_page_renders_and_requires_login(ia_client):
    client, _ = ia_client
    assert client.get("/ia", follow_redirects=False).status_code == 303
    login(client)
    html = client.get("/ia").text
    assert "Modelli IA" in html and "Salva e cerca i modelli" in html and "Groq" in html and "DeepSeek" in html


def test_saving_a_key_finds_the_models_and_picks_them_by_itself(ia_client):
    client, service = ia_client
    token = login(client)
    response = client.post("/ia", data={"csrf": token, "groq_api_key": "gsk_buona", "ai_docs_provider": "groq", "ai_chat_provider": "groq"})
    assert "scelto meta-llama/llama-4-scout-17b-16e-instruct" in response.text and "scelto llama-3.3-70b-versatile" in response.text
    assert (service.ai.provider("docs"), service.ai.model("docs")) == ("groq", "meta-llama/llama-4-scout-17b-16e-instruct")
    assert service.ai.model("chat") == "llama-3.3-70b-versatile"
    html = client.get("/ia").text
    assert "4 modelli trovati" not in html and "3 modelli trovati" in html or "modelli trovati nel servizio" in html
    assert "whisper-large-v3" in html  # ora c'è il menu con l'elenco


def test_changing_the_service_drops_the_old_model_and_picks_a_new_one(ia_client):
    client, service = ia_client
    token = login(client)
    client.post("/ia", data={"csrf": token, "groq_api_key": "gsk", "deepseek_api_key": "sk", "ai_docs_provider": "groq", "ai_chat_provider": "groq"})
    client.post("/ia", data={"csrf": token, "ai_docs_provider": "groq", "ai_chat_provider": "deepseek",
                             "ai_chat_model_select": "llama-3.3-70b-versatile"})  # il vecchio valore del menu non vale più
    assert (service.ai.provider("chat"), service.ai.model("chat")) == ("deepseek", "deepseek-chat")
    assert service.ai.model("docs") == "meta-llama/llama-4-scout-17b-16e-instruct"  # invariato


def test_a_typed_model_name_wins_over_the_menu_and_is_kept_even_if_not_listed(ia_client):
    client, service = ia_client
    token = login(client)
    client.post("/ia", data={"csrf": token, "groq_api_key": "gsk", "ai_docs_provider": "groq", "ai_chat_provider": "groq"})
    response = client.post("/ia", data={"csrf": token, "ai_docs_provider": "groq", "ai_chat_provider": "groq",
                                        "ai_chat_model_select": "llama-3.3-70b-versatile", "ai_chat_model_manual": "nome-nuovo-non-elencato"})
    assert service.ai.model("chat") == "nome-nuovo-non-elencato"
    assert "«nome-nuovo-non-elencato» non compare" in response.text and "Prova" in response.text


@pytest.mark.parametrize(
    "data,fragment",
    [
        ({"custom_base_url": "ftp://esempio"}, "http:// o https://"),
        ({"custom_base_url": "javascript:alert(1)"}, "http:// o https://"),
        ({"ai_chat_provider": "sconosciuto"}, "Servizio non valido"),
        ({"ai_chat_provider": "gemini", "ai_chat_model_manual": "nome con spazi"}, "caratteri non validi"),
        ({"ai_chat_provider": "gemini", "ai_chat_model_manual": "x;rm -rf"}, "caratteri non validi"),
    ],
)
def test_invalid_input_is_refused_without_saving(ia_client, data, fragment):
    client, service = ia_client
    token = login(client)
    body = {"csrf": token, "ai_docs_provider": "gemini", "ai_chat_provider": "gemini", **data}
    assert fragment in client.post("/ia", data=body).text
    assert service.settings.get("custom_base_url") == "" and service.ai.provider("chat") == "gemini"


def test_a_wrong_key_shows_the_reason_and_breaks_nothing(ia_client):
    client, service = ia_client
    token = login(client)
    response = client.post("/ia", data={"csrf": token, "groq_api_key": "SBAGLIATA", "ai_docs_provider": "groq", "ai_chat_provider": "gemini"})
    assert response.status_code == 200 and "La chiave di Groq non è valida" in response.text
    assert service.ai.model("docs") == ""


def test_keys_are_encrypted_kept_when_left_blank_and_never_rendered(ia_client):
    client, service = ia_client
    token = login(client)
    client.post("/ia", data={"csrf": token, "groq_api_key": "gsk_segretissima", "custom_base_url": "http://192.168.0.50:11434/v1/", "custom_api_key": "ollama"})
    # come il browser: il campo indirizzo è precompilato e la casella della chiave resta vuota
    client.post("/ia", data={"csrf": token, "groq_api_key": "", "custom_base_url": service.settings.get("custom_base_url"), "ai_docs_provider": "gemini", "ai_chat_provider": "gemini"})
    assert service.settings.get("groq_api_key") == "gsk_segretissima" and service.settings.get("custom_base_url") == "http://192.168.0.50:11434/v1"
    stored = service.db.execute("SELECT value FROM settings WHERE key = 'groq_api_key'")[0]["value"]
    assert "gsk_segretissima" not in stored
    assert "gsk_segretissima" not in client.get("/ia").text


def test_probe_buttons_report_the_outcome_per_role(ia_client):
    client, service = ia_client
    token = login(client)
    client.post("/ia", data={"csrf": token, "groq_api_key": "gsk", "ai_docs_provider": "groq", "ai_chat_provider": "groq"})
    docs = client.post("/ia/prova/docs", data={"csrf": token}).text
    assert "Lettura dei documenti · Groq · meta-llama/llama-4-scout-17b-16e-instruct: funziona" in docs
    assert "Domande e risposte · Groq · llama-3.3-70b-versatile: funziona" in client.post("/ia/prova/chat", data={"csrf": token}).text


def test_probe_requires_login_csrf_and_a_valid_role(ia_client):
    client, _ = ia_client
    assert client.post("/ia/prova/chat", follow_redirects=False).status_code == 303
    token = login(client)
    assert client.post("/ia/prova/chat").status_code == 403
    assert client.post("/ia/prova/altro", data={"csrf": token}).status_code == 404


def test_status_page_shows_which_service_answers(ia_client):
    client, service = ia_client
    token = login(client)
    html = client.get("/").text
    assert "Intelligenza artificiale" in html and "Google Gemini" in html
    client.post("/ia", data={"csrf": token, "groq_api_key": "gsk", "ai_docs_provider": "gemini", "ai_chat_provider": "groq"})
    html = client.get("/").text
    assert "Groq" in html and "llama-3.3-70b-versatile" in html


def test_repick_button_redoes_the_automatic_choice(ia_client):
    client, service = ia_client
    token = login(client)
    client.post("/ia", data={"csrf": token, "groq_api_key": "gsk", "ai_docs_provider": "groq", "ai_chat_provider": "groq"})
    service.settings.update({"ai_docs_model": "modello-sbagliato-senza-visione"})
    response = client.post("/ia/auto", data={"csrf": token})
    assert "che legge le immagini" in response.text
    assert service.ai.model("docs") == "meta-llama/llama-4-scout-17b-16e-instruct"


def test_repick_requires_login_and_csrf(ia_client):
    client, _ = ia_client
    assert client.post("/ia/auto", follow_redirects=False).status_code == 303
    login(client)
    assert client.post("/ia/auto").status_code == 403


def test_context_size_field_is_saved_and_validated(ia_client):
    client, service = ia_client
    token = login(client)
    base = {"csrf": token, "ai_docs_provider": "gemini", "ai_chat_provider": "gemini"}
    assert 'value="9000"' in client.get("/ia").text
    client.post("/ia", data={**base, "ai_context_chars": "15000"})
    assert service.settings.get("ai_context_chars") == "15000"
    for bad in ("100", "999999", "molti"):
        response = client.post("/ia", data={**base, "ai_context_chars": bad})
        assert "tra 2000 e 200000" in response.text and service.settings.get("ai_context_chars") == "15000"
    client.post("/ia", data=base)  # campo assente: resta il valore attuale
    assert service.settings.get("ai_context_chars") == "15000"


# --- Chiavi richieste secondo i servizi scelti ---------------------------------


def test_missing_keys_follow_the_services_actually_chosen(service):
    assert service.settings.missing_for_run() == ["Token del bot Telegram", "Chiave di Google Gemini"]
    service.settings.update({"ai_docs_provider": "groq", "ai_chat_provider": "groq"})
    assert service.settings.missing_for_run() == ["Token del bot Telegram", "Chiave di Groq"]  # una volta sola, e non più Gemini
    service.settings.update({"ai_chat_provider": "custom", "groq_api_key": "gsk", "bot_token": "1:abc"})
    assert service.settings.missing_for_run() == ["Chiave di Altro (compatibile OpenAI)", "Indirizzo del servizio compatibile OpenAI"]
    service.settings.update({"custom_api_key": "k", "custom_base_url": "http://x/v1"})
    assert service.settings.missing_for_run() == []


def test_status_page_asks_for_the_key_of_the_chosen_service(client, service):
    login(client)
    service.settings.update({"ai_chat_provider": "deepseek"})
    html = client.get("/").text
    assert "Chiave di DeepSeek" in html and "Chiave di Google Gemini" in html  # i documenti sono ancora su Gemini


# --- Caricamento automatico dei modelli e finestra con la rotella ----------------


def test_the_ai_page_wires_the_automatic_loading_and_the_spinner(client):
    login(client)
    html = client.get("/ia").text
    assert 'id="carica"' in html and 'class="spinner"' in html and 'role="status"' in html  # la finestra con la rotella
    assert html.count('class="provider-select"') == 2  # un menu per funzione
    for provider in ("gemini", "groq", "deepseek", "custom"):
        assert f'data-key-field="{provider}_api_key"' in html
    assert "requestSubmit" in html  # il cambio di servizio invia il modulo che carica i modelli
    assert html.count("data-carica=") >= 5  # anche i salvataggi e le prove mostrano la rotella se ci mettono troppo


def test_a_provider_with_a_saved_key_is_marked_so_the_page_can_load_at_once(ia_client):
    client, service = ia_client
    token = login(client)
    assert 'data-has-key=""' in client.get("/ia").text
    client.post("/ia", data={"csrf": token, "groq_api_key": "gsk", "ai_docs_provider": "gemini", "ai_chat_provider": "gemini"})
    html = client.get("/ia").text
    assert 'value="groq" data-key-field="groq_api_key" data-has-key="1"' in html


def test_a_key_not_yet_entered_is_shown_as_a_note_not_an_error(client, service):
    token = login(client)
    html = client.post("/ia", data={"csrf": token, "ai_docs_provider": "groq", "ai_chat_provider": "groq"}).text
    assert "Groq: chiave non impostata, elenco dei modelli non caricato." in html
    assert 'class="flash info"' in html and 'class="flash error">Groq' not in html
