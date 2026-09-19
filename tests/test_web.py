import re

import pytest
from fastapi.testclient import TestClient

from famiglia.auth import set_password
from famiglia.web.app import create_app

PASSWORD = "password-lunga-123"
PAGES = ("/", "/api", "/utenti", "/cartella", "/password")


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


def test_save_keys_and_blank_secret_is_kept(client, service):
    token = login(client)
    client.post("/api", data={"csrf": token, "gemini_api_key": "AIza-1", "bot_token": "1:abc", "gemini_model": "gemini-3.8-flash"})
    client.post("/api", data={"csrf": token, "gemini_api_key": "", "bot_token": "", "gemini_model": "gemini-3.1-pro-preview"})
    assert service.settings.get("gemini_api_key") == "AIza-1"
    assert service.settings.get("bot_token") == "1:abc"
    assert service.settings.get("gemini_model") == "gemini-3.1-pro-preview"


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


def test_fallback_model_can_be_saved_and_emptied(client, service):
    token = login(client)
    client.post("/api", data={"csrf": token, "gemini_model": "gemini-3.8-flash", "gemini_fallback_model": "gemini-3.5-flash-lite"})
    assert service.settings.get("gemini_fallback_model") == "gemini-3.5-flash-lite"
    client.post("/api", data={"csrf": token, "gemini_model": "gemini-3.8-flash", "gemini_fallback_model": ""})
    assert service.settings.get("gemini_fallback_model") == ""
    response = client.post("/api", data={"csrf": token, "gemini_model": "gemini-3.8-flash", "gemini_fallback_model": "nome con spazi"})
    assert "Modello di riserva" in response.text


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


def test_probe_button_shows_each_result_with_the_real_reason(tmp_path, root):
    import httpx

    from famiglia.service import Service

    def respond(request):
        if b"googleSearch" in request.content:
            return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota"}})
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}]})

    service = Service(tmp_path / "data", "chiave-di-test", root, gemini_http=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    service.settings.update({"gemini_api_key": "AIza-test"})
    set_password(service.settings, PASSWORD)
    client = TestClient(create_app(service, secret_key="chiave-di-test", admin_user="admin"))
    token = login(client)
    html = client.post("/api/prova", data={"csrf": token}).text
    assert "Modello principale (gemini-3.8-flash): funziona" in html
    assert "Modello di riserva (gemini-3.5-flash-lite): funziona" in html
    assert "Ricerca web con gemini-3.8-flash: 429" in html and "exceeded your current quota" in html


def test_probe_without_a_key_says_so_and_requires_login(client):
    assert client.post("/api/prova", follow_redirects=False).status_code == 303
    token = login(client)
    assert "manca la chiave Gemini" in client.post("/api/prova", data={"csrf": token}).text


def test_web_search_switch_is_saved(client, service):
    token = login(client)
    assert 'name="consult_web_search" value="1" checked' in client.get("/api").text  # attiva di default
    client.post("/api", data={"csrf": token, "gemini_model": "gemini-3.8-flash"})  # casella non spuntata
    assert service.settings.get("consult_web_search") == "0"
    client.post("/api", data={"csrf": token, "gemini_model": "gemini-3.8-flash", "consult_web_search": "1"})
    assert service.settings.get("consult_web_search") == "1"


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
