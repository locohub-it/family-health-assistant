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

GROQ_LIST = {"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "meta-llama/llama-4-scout-17b-16e-instruct"}, {"id": "whisper-large-v3"}, {"id": "playai-tts"}]}
DEEPSEEK_LIST = {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}
GEMINI_LIST = {"models": [
    {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
]}
GROQ_ADDRESS = "https://api.groq.com/openai/v1"
DEEPSEEK_ADDRESS = "https://api.deepseek.com"


@pytest.fixture
def ia_client(tmp_path, root):
    """Pannello con Groq, DeepSeek e Gemini finti: l'elenco dei modelli dipende dal servizio interpellato."""
    import httpx

    from famiglia.service import Service

    def respond(request):
        if request.headers.get("authorization", "").endswith("SBAGLIATA"):
            return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=GROQ_LIST if "groq" in request.url.host else DEEPSEEK_LIST)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    def gemini(request):
        if request.headers.get("x-goog-api-key") == "SBAGLIATA":
            return httpx.Response(403, json={"error": {"code": 403, "message": "no", "status": "PERMISSION_DENIED"}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=GEMINI_LIST)
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}]})

    service = Service(
        tmp_path / "data", "chiave-di-test", root,
        ai_http=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        gemini_http=httpx.AsyncClient(transport=httpx.MockTransport(gemini)),
    )
    set_password(service.settings, PASSWORD)
    return TestClient(create_app(service, secret_key="chiave-di-test", admin_user="admin")), service


def add_service(client, token, name="Groq", kind="openai", base_url=GROQ_ADDRESS, api_key="gsk_segretissima"):
    return client.post("/ia/servizi", data={"csrf": token, "name": name, "kind": kind, "base_url": base_url, "api_key": api_key})


# --- La pagina: tre riquadri -----------------------------------------------------


def test_ai_page_has_three_numbered_boxes_and_no_leftover_of_the_old_layout(ia_client):
    client, _ = ia_client
    assert client.get("/ia", follow_redirects=False).status_code == 303
    login(client)
    html = client.get("/ia").text
    for title in ("Servizi AI", "Modelli da usare", "Verifica"):
        assert title in html
    assert "Aggiungi un servizio" in html and "Riserva (facoltativa)" in html and html.count("Principale") >= 2
    for gone in ('name="groq_api_key"', 'name="gemini_api_key"', 'name="custom_base_url"', "provider-select", "Prova Gemini"):
        assert gone not in html


def test_api_keys_page_only_has_telegram_and_composio(ia_client):
    client, _ = ia_client
    login(client)
    html = client.get("/api").text
    assert 'name="bot_token"' in html and 'name="composio_api_key"' in html and "/ia" in html
    for gone in ('name="gemini_api_key"', 'name="groq_api_key"', 'name="deepseek_api_key"', 'name="gemini_model"', "consult_web_search"):
        assert gone not in html
    assert "ricerca web" not in html.lower()


# --- Servizi: aggiunta ---------------------------------------------------------------


def test_the_first_service_added_becomes_the_service_of_every_function_with_models_picked(ia_client):
    client, service = ia_client
    token = login(client)
    response = add_service(client, token)
    text = response.text
    assert "Servizio «Groq» aggiunto" in text and "è ora il servizio principale per: Lettura dei documenti, Domande e risposte" in text
    assert "scelto meta-llama/llama-4-scout-17b-16e-instruct, che legge le immagini" in text and "scelto llama-3.3-70b-versatile" in text
    assert service.ai.slot("docs")[0].name == "Groq" and service.ai.slot("docs")[1] == "meta-llama/llama-4-scout-17b-16e-instruct"
    assert service.ai.slot("chat")[1] == "llama-3.3-70b-versatile"
    assert service.ai.slot("chat", True)[0] is None  # nessuna riserva imposta da soli


def test_a_second_service_does_not_take_over_the_functions(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    response = add_service(client, token, name="DeepSeek", base_url=DEEPSEEK_ADDRESS, api_key="sk-deep")
    assert "Servizio «DeepSeek» aggiunto" in response.text and "è ora il servizio principale" not in response.text
    assert service.ai.slot("chat")[0].name == "Groq" and service.ai.slot("chat")[1] == "llama-3.3-70b-versatile"
    assert [s.name for s in service.ai_services.all()] == ["DeepSeek", "Groq"]


def test_a_gemini_service_needs_no_address_and_lists_its_models(ia_client):
    client, service = ia_client
    token = login(client)
    response = add_service(client, token, name="Gemini", kind="gemini", base_url="", api_key="AIza-1")
    assert "Servizio «Gemini» aggiunto" in response.text
    assert service.ai_services.all()[0].models == ("gemini-3.5-flash-lite", "gemini-3.8-flash")
    assert service.ai.slot("chat")[1] == "gemini-3.8-flash"  # la versione stabile più alta, non la lite


@pytest.mark.parametrize(
    "fields,fragment",
    [
        ({"name": ""}, "nome è obbligatorio"),
        ({"base_url": "ftp://esempio"}, "http:// o https://"),
        ({"base_url": "javascript:alert(1)"}, "http:// o https://"),
        ({"kind": "boh"}, "Tipo di servizio non valido"),
        ({"kind": "gemini", "api_key": ""}, "serve la chiave"),
    ],
)
def test_invalid_services_are_refused_and_nothing_is_saved(ia_client, fields, fragment):
    client, service = ia_client
    token = login(client)
    assert fragment in add_service(client, token, **fields).text
    assert service.ai_services.all() == []


def test_a_duplicate_name_is_refused(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    assert "Esiste già un servizio chiamato «groq»" in add_service(client, token, name="groq").text
    assert len(service.ai_services.all()) == 1


def test_a_wrong_key_is_reported_when_looking_for_the_models_but_the_service_is_kept(ia_client):
    client, service = ia_client
    token = login(client)
    response = add_service(client, token, api_key="SBAGLIATA")
    assert "Servizio «Groq» aggiunto" in response.text and "Groq: La chiave di Groq non è valida" in response.text
    assert service.ai.slot("chat")[1] == ""  # nessun modello scelto finché la chiave non funziona


def test_keys_are_never_rendered_and_are_encrypted(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    for page in ("/ia", f"/ia/servizi/{service.ai_services.all()[0].id}", "/"):
        assert "gsk_segretissima" not in client.get(page).text
    assert "gsk_segretissima" not in service.db.execute("SELECT api_key FROM ai_services")[0]["api_key"]
    assert 'class="badge ok">chiave impostata' in client.get("/ia").text


def test_service_routes_require_login_and_csrf(ia_client):
    client, _ = ia_client
    for method, url in (("post", "/ia/servizi"), ("get", "/ia/servizi/1"), ("post", "/ia/servizi/1"), ("post", "/ia/servizi/1/modelli"), ("post", "/ia/servizi/1/elimina")):
        assert getattr(client, method)(url, follow_redirects=False).status_code == 303
    token = login(client)
    assert add_service(client, token).status_code == 200
    for url in ("/ia/servizi", "/ia/servizi/1", "/ia/servizi/1/modelli", "/ia/servizi/1/elimina"):
        assert client.post(url, data={}).status_code == 403
    assert client.get("/ia/servizi/999").status_code == 404
    assert client.post("/ia/servizi/999/modelli", data={"csrf": token}).status_code == 404


# --- Servizi: modifica, aggiornamento elenco, rimozione ---------------------------------


def test_edit_a_service_keeping_or_replacing_the_key(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    target = service.ai_services.all()[0]
    page = client.get(f"/ia/servizi/{target.id}").text
    assert 'value="Groq"' in page and GROQ_ADDRESS in page and "lascia vuoto per non cambiarla" in page
    response = client.post(f"/ia/servizi/{target.id}", data={"csrf": token, "name": "Groq gratuito", "kind": "openai", "base_url": GROQ_ADDRESS, "api_key": ""})
    assert "Servizio «Groq gratuito» aggiornato" in response.text
    updated = service.ai_services.get(target.id)
    assert (updated.name, updated.api_key) == ("Groq gratuito", "gsk_segretissima")
    client.post(f"/ia/servizi/{target.id}", data={"csrf": token, "name": "Groq gratuito", "kind": "openai", "base_url": GROQ_ADDRESS, "api_key": "gsk_nuova"})
    assert service.ai_services.get(target.id).api_key == "gsk_nuova"


def test_editing_with_invalid_data_shows_the_error_on_the_edit_page(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    target = service.ai_services.all()[0]
    response = client.post(f"/ia/servizi/{target.id}", data={"csrf": token, "name": "Groq", "kind": "openai", "base_url": "boh", "api_key": ""})
    assert "http:// o https://" in response.text and 'value="Groq"' in response.text  # resta sulla pagina di modifica
    assert service.ai_services.get(target.id).base_url == GROQ_ADDRESS


def test_refresh_button_reports_how_many_models_were_found(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    target = service.ai_services.all()[0]
    assert "Groq: 4 modelli trovati" in client.post(f"/ia/servizi/{target.id}/modelli", data={"csrf": token}).text
    service.ai_services.update(target.id, "Groq", "openai", GROQ_ADDRESS, "SBAGLIATA")
    assert "La chiave di Groq non è valida" in client.post(f"/ia/servizi/{target.id}/modelli", data={"csrf": token}).text


def test_refresh_of_a_gemini_service_without_a_readable_key_is_a_neutral_note(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token, name="Gemini", kind="gemini", base_url="", api_key="AIza-1")
    target = service.ai_services.all()[0]
    service.db.execute("UPDATE ai_services SET api_key = '' WHERE id = ?", (target.id,))
    html = client.post(f"/ia/servizi/{target.id}/modelli", data={"csrf": token}).text
    assert 'class="flash info"' in html and "chiave non impostata" in html


def test_removing_a_service_empties_the_functions_that_used_it(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    target = service.ai_services.all()[0]
    html = client.post(f"/ia/servizi/{target.id}/elimina", data={"csrf": token}).text
    assert "Servizio «Groq» rimosso" in html and "— scegli un servizio —" in html
    assert service.ai_services.all() == [] and service.ai.slot("docs")[0] is None and service.ai.slot("chat")[1] == ""


# --- Modelli da usare: principale e riserva --------------------------------------------


def slots_form(token, **overrides):
    """Il modulo «Modelli da usare» come lo invia il browser: tutto invariato tranne quanto indicato."""
    return {"csrf": token, "ai_context_chars": "9000", **overrides}


def test_a_backup_on_another_service_can_be_chosen_and_its_model_is_picked_by_itself(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    add_service(client, token, name="Gemini", kind="gemini", base_url="", api_key="AIza-1")
    gemini = next(s for s in service.ai_services.all() if s.name == "Gemini")
    groq = next(s for s in service.ai_services.all() if s.name == "Groq")
    response = client.post("/ia", data=slots_form(
        token, ai_docs_service=str(groq.id), ai_docs_model_select="meta-llama/llama-4-scout-17b-16e-instruct",
        ai_chat_service=str(groq.id), ai_chat_model_select="llama-3.3-70b-versatile",
        ai_chat_backup_service=str(gemini.id), ai_docs_backup_service=str(gemini.id)))
    assert "Domande e risposte · riserva: scelto gemini-3.8-flash tra i 2 modelli di Gemini" in response.text
    assert service.ai.slot("chat", True)[0].name == "Gemini" and service.ai.slot("chat", True)[1] == "gemini-3.8-flash"
    assert service.ai.slot("docs", True)[1] == "gemini-3.8-flash"
    assert service.ai.slot("chat")[1] == "llama-3.3-70b-versatile"  # la principale è rimasta com'era


def test_changing_the_service_of_a_slot_drops_its_old_model_and_picks_a_new_one(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    add_service(client, token, name="DeepSeek", base_url=DEEPSEEK_ADDRESS, api_key="sk")
    deepseek = next(s for s in service.ai_services.all() if s.name == "DeepSeek")
    groq = next(s for s in service.ai_services.all() if s.name == "Groq")
    client.post("/ia", data=slots_form(token, ai_docs_service=str(groq.id), ai_docs_model_select="meta-llama/llama-4-scout-17b-16e-instruct",
                                       ai_chat_service=str(deepseek.id), ai_chat_model_select="llama-3.3-70b-versatile"))  # il vecchio valore del menu non vale più
    assert (service.ai.slot("chat")[0].name, service.ai.slot("chat")[1]) == ("DeepSeek", "deepseek-chat")
    assert service.ai.slot("docs")[1] == "meta-llama/llama-4-scout-17b-16e-instruct"  # invariato


def test_a_typed_model_name_wins_over_the_menu_and_is_kept_even_if_not_listed(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    groq = service.ai_services.all()[0]
    response = client.post("/ia", data=slots_form(
        token, ai_docs_service=str(groq.id), ai_docs_model_select="meta-llama/llama-4-scout-17b-16e-instruct",
        ai_chat_service=str(groq.id), ai_chat_model_select="llama-3.3-70b-versatile", ai_chat_model_manual="nome-nuovo-non-elencato"))
    assert service.ai.slot("chat")[1] == "nome-nuovo-non-elencato"
    assert "«nome-nuovo-non-elencato» non compare" in response.text and "Prova" in response.text


def test_removing_the_backup_choosing_no_service(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    groq = service.ai_services.all()[0]
    base = dict(ai_docs_service=str(groq.id), ai_docs_model_select="meta-llama/llama-4-scout-17b-16e-instruct", ai_chat_service=str(groq.id), ai_chat_model_select="llama-3.3-70b-versatile")
    client.post("/ia", data=slots_form(token, **base, ai_chat_backup_service=str(groq.id)))
    assert service.ai.slot("chat", True)[0] is not None
    client.post("/ia", data=slots_form(token, **base, ai_chat_backup_service=""))
    assert service.ai.slot("chat", True) == (None, "")


@pytest.mark.parametrize(
    "extra,fragment",
    [
        ({"ai_chat_service": "999"}, "Servizio non valido"),
        ({"ai_chat_service": "abc"}, "Servizio non valido"),
        ({"ai_chat_model_manual": "nome con spazi"}, "caratteri non validi"),
        ({"ai_chat_model_manual": "x;rm -rf"}, "caratteri non validi"),
        ({"ai_context_chars": "100"}, "tra 2000 e 200000"),
        ({"ai_context_chars": "999999"}, "tra 2000 e 200000"),
        ({"ai_context_chars": "molti"}, "tra 2000 e 200000"),
    ],
)
def test_invalid_choices_are_refused_without_saving_anything(ia_client, extra, fragment):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    groq = service.ai_services.all()[0]
    before = service.ai.slot("chat")
    body = {**slots_form(token, ai_docs_service=str(groq.id), ai_chat_service=str(groq.id)), **extra}
    assert fragment in client.post("/ia", data=body).text
    assert service.ai.slot("chat") == before and service.settings.get("ai_context_chars") == "9000"


def test_context_size_is_saved(ia_client):
    client, service = ia_client
    token = login(client)
    assert 'value="9000"' in client.get("/ia").text
    client.post("/ia", data=slots_form(token, ai_context_chars="15000"))
    assert service.settings.get("ai_context_chars") == "15000"


def test_the_page_offers_the_models_found_in_each_chosen_service(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    html = client.get("/ia").text
    assert "4 modelli trovati in Groq" in html and "whisper-large-v3" in html
    assert '<select id="ai_chat_service"' in html and '<select id="ai_chat_backup_service"' in html
    assert "Nessuna riserva" in html


# --- Prova, scelta da capo, stato ----------------------------------------------------


def test_probe_shows_the_outcome_for_primary_and_backup(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    add_service(client, token, name="Gemini", kind="gemini", base_url="", api_key="AIza-1")
    gemini = next(s for s in service.ai_services.all() if s.name == "Gemini")
    service.settings.update({"ai_chat_backup_service": str(gemini.id), "ai_chat_backup_model": "gemini-3.8-flash"})
    html = client.post("/ia/prova/chat", data={"csrf": token}).text
    assert "Domande e risposte · principale · Groq · llama-3.3-70b-versatile: funziona" in html
    assert "Domande e risposte · riserva · Gemini · gemini-3.8-flash: funziona" in html
    assert "Lettura dei documenti · principale · Groq · meta-llama/llama-4-scout-17b-16e-instruct: funziona" in client.post("/ia/prova/docs", data={"csrf": token}).text


def test_probe_says_what_is_missing_and_validates_the_role(ia_client):
    client, _ = ia_client
    assert client.post("/ia/prova/chat", follow_redirects=False).status_code == 303
    token = login(client)
    assert "Nessun servizio scelto" in client.post("/ia/prova/chat", data={"csrf": token}).text
    assert client.post("/ia/prova/chat").status_code == 403
    assert client.post("/ia/prova/altro", data={"csrf": token}).status_code == 404
    assert client.post("/ia/prova-gemini", data={"csrf": token}).status_code in (404, 405)  # la vecchia prova non c'è più


def test_repick_button_redoes_the_automatic_choice(ia_client):
    client, service = ia_client
    token = login(client)
    add_service(client, token)
    service.settings.update({"ai_docs_model": "modello-sbagliato-senza-visione"})
    response = client.post("/ia/auto", data={"csrf": token})
    assert "che legge le immagini" in response.text
    assert service.ai.slot("docs")[1] == "meta-llama/llama-4-scout-17b-16e-instruct"


def test_repick_requires_login_and_csrf(ia_client):
    client, _ = ia_client
    assert client.post("/ia/auto", follow_redirects=False).status_code == 303
    login(client)
    assert client.post("/ia/auto").status_code == 403


def test_status_page_shows_primary_and_backup_and_what_is_missing(ia_client):
    client, service = ia_client
    token = login(client)
    html = client.get("/").text
    assert "Servizio AI per «Lettura dei documenti»" in html and "servizio da scegliere" in html
    add_service(client, token)
    add_service(client, token, name="Gemini", kind="gemini", base_url="", api_key="AIza-1")
    gemini = next(s for s in service.ai_services.all() if s.name == "Gemini")
    service.settings.update({"ai_chat_backup_service": str(gemini.id), "ai_chat_backup_model": "gemini-3.8-flash"})
    html = client.get("/").text
    assert "Groq · llama-3.3-70b-versatile" in html and "riserva: Gemini · gemini-3.8-flash" in html
    assert "Servizio AI per" not in html and "Modello per" not in html


def test_the_ai_page_wires_the_automatic_loading_and_the_spinner(ia_client):
    client, service = ia_client
    login(client)
    html = client.get("/ia").text
    assert 'id="carica"' in html and 'class="spinner"' in html and 'role="status"' in html
    assert html.count('class="service-select"') == 4  # due funzioni, principale e riserva
    assert "requestSubmit" in html and html.count("data-carica=") >= 5
    assert 'id="new_kind"' in html and 'id="new_address"' in html  # per Gemini l'indirizzo si nasconde
    assert "indirizzi" in html and "api.groq.com" in html and "localhost:11434" in html  # suggerimenti, non obblighi
