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
