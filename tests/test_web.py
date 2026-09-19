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
    client.post("/api", data={"csrf": token, "gemini_api_key": "AIza-1", "bot_token": "1:abc", "gemini_model": "gemini-2.5-flash"})
    client.post("/api", data={"csrf": token, "gemini_api_key": "", "bot_token": "", "gemini_model": "gemini-2.5-pro"})
    assert service.settings.get("gemini_api_key") == "AIza-1"
    assert service.settings.get("bot_token") == "1:abc"
    assert service.settings.get("gemini_model") == "gemini-2.5-pro"


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
