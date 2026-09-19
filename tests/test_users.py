import pytest


def test_only_added_users_are_approved(service):
    service.users.add(111, "Mario", "papà")
    assert service.users.by_telegram_id(111).name == "Mario"
    assert service.users.by_telegram_id(222) is None


@pytest.mark.parametrize("telegram_id,name", [(0, "Mario"), (-5, "Mario"), (1, ""), (1, "   "), (1, "x" * 61)])
def test_invalid_users_are_rejected(service, telegram_id, name):
    with pytest.raises(ValueError):
        service.users.add(telegram_id, name)


def test_duplicate_telegram_id_is_rejected(service):
    service.users.add(111, "Mario")
    with pytest.raises(ValueError):
        service.users.add(111, "Altro")


def test_removing_a_user_removes_their_records(service):
    user = service.users.add(111, "Mario")
    doc = service.db.execute_returning_id(
        "INSERT INTO documents (user_id, sender_telegram_id, kind, created_at) VALUES (?, 111, 'referto', 0)", (user.id,)
    )
    service.db.execute(
        "INSERT INTO lab_results (document_id, user_id, name, value) VALUES (?, ?, 'Glicemia', '95')", (doc, user.id)
    )
    service.users.remove(user.id)
    assert service.db.execute("SELECT * FROM documents") == []
    assert service.db.execute("SELECT * FROM lab_results") == []


def test_composio_user_id_is_stable(service):
    assert service.users.add(111, "Mario").composio_user_id == "famiglia-111"


def test_secrets_are_encrypted_at_rest(service):
    service.settings.update({"gemini_api_key": "AIza-segreta"})
    stored = service.db.execute("SELECT value FROM settings WHERE key = 'gemini_api_key'")[0]["value"]
    assert "AIza-segreta" not in stored
    assert service.settings.get("gemini_api_key") == "AIza-segreta"


# --- Nome e cognome reali ------------------------------------------------------


def test_real_name_is_stored_and_used_as_full_name(service):
    user = service.users.add(111, "Papà", "papà", "Mario", "Rossi")
    assert (user.first_name, user.last_name, user.name) == ("Mario", "Rossi", "Papà")
    assert user.full_name == "Mario Rossi"


def test_full_name_falls_back_to_the_short_name(service):
    assert service.users.add(111, "Papà").full_name == "Papà"


def test_short_name_defaults_to_the_first_name(service):
    user = service.users.add(111, first_name="Maria Grazia", last_name="Bianchi")
    assert user.name == "Maria Grazia" and user.full_name == "Maria Grazia Bianchi"


def test_names_are_cleaned_of_extra_spaces(service):
    user = service.users.add(111, "  Papà ", first_name="  Mario   ", last_name=" De   Luca ")
    assert (user.name, user.first_name, user.last_name) == ("Papà", "Mario", "De Luca")


@pytest.mark.parametrize("kwargs", [{"first_name": "x" * 61}, {"name": "ok", "last_name": "x" * 61}, {}])
def test_invalid_real_names_are_rejected(service, kwargs):
    with pytest.raises(ValueError):
        service.users.add(111, **kwargs)


def test_update_changes_names_but_keeps_telegram_id_and_calendar(service):
    user = service.users.add(111, "Mario")
    service.users.set_calendar(user.id, "mario@example.com")
    updated = service.users.update(user.id, "Papà", "papà", "Mario", "Rossi")
    assert (updated.name, updated.role, updated.full_name) == ("Papà", "papà", "Mario Rossi")
    assert updated.telegram_id == 111 and updated.calendar_id == "mario@example.com"


def test_update_rejects_bad_data_and_unknown_users(service):
    user = service.users.add(111, "Mario")
    with pytest.raises(ValueError):
        service.users.update(user.id, "", "", "", "")
    with pytest.raises(ValueError, match="non trovato"):
        service.users.update(999, "X", "", "", "")
    assert service.users.get(user.id).name == "Mario"


def test_an_existing_database_without_the_new_columns_is_upgraded_without_losing_users(tmp_path):
    import sqlite3

    from famiglia.db import Database
    from famiglia.users import UserStore

    path = tmp_path / "vecchio.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL UNIQUE, "
        "name TEXT NOT NULL, role TEXT NOT NULL DEFAULT '', calendar_id TEXT NOT NULL DEFAULT 'primary')"
    )
    old.execute("INSERT INTO users (telegram_id, name, role, calendar_id) VALUES (111, 'Mario', 'papà', 'mario@example.com')")
    old.commit()
    old.close()

    store = UserStore(Database(path))  # l'apertura aggiunge le colonne
    user = store.by_telegram_id(111)
    assert (user.name, user.role, user.calendar_id) == ("Mario", "papà", "mario@example.com")
    assert (user.first_name, user.last_name, user.full_name) == ("", "", "Mario")
    assert store.update(user.id, "Mario", "papà", "Mario", "Rossi").full_name == "Mario Rossi"
    Database(path)  # una seconda apertura non deve fallire (colonne già presenti)
