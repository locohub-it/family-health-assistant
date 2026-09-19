import json

import pytest

from famiglia.ai import KINDS


def add_groq(svc, **kwargs):
    args = {"name": "Groq", "kind": "openai", "base_url": "https://api.groq.com/openai/v1", "api_key": "gsk_segretissima"}
    return svc.ai_services.add(**{**args, **kwargs})


# --- Aggiunta e validazione ----------------------------------------------------


def test_add_and_read_back_a_service(service):
    added = add_groq(service)
    assert (added.name, added.kind, added.base_url, added.api_key, added.models) == ("Groq", "openai", "https://api.groq.com/openai/v1", "gsk_segretissima", ())
    assert added.has_key and service.ai_services.get(added.id) == added
    endpoint = added.endpoint("llama-x")
    assert (endpoint.kind, endpoint.label, endpoint.base_url, endpoint.api_key, endpoint.model) == ("openai", "Groq", "https://api.groq.com/openai/v1", "gsk_segretissima", "llama-x")


def test_the_api_key_is_encrypted_at_rest(service):
    add_groq(service)
    stored = service.db.execute("SELECT api_key FROM ai_services")[0]["api_key"]
    assert stored and "gsk_segretissima" not in stored
    assert service.settings.decrypt(stored) == "gsk_segretissima"


def test_a_key_that_cannot_be_decrypted_is_treated_as_missing(service):
    added = add_groq(service)
    service.db.execute("UPDATE ai_services SET api_key = 'non cifrata' WHERE id = ?", (added.id,))
    assert service.ai_services.get(added.id).api_key == "" and not service.ai_services.get(added.id).has_key


def test_names_addresses_and_kind_are_normalised_and_validated(service):
    added = add_groq(service, name="  Il   mio   Groq ", base_url="  https://api.groq.com/openai/v1/  ")
    assert added.name == "Il mio Groq" and added.base_url == "https://api.groq.com/openai/v1"  # senza barra finale
    for kwargs, fragment in [
        ({"name": ""}, "nome è obbligatorio"), ({"name": "x" * 41}, "massimo 40"), ({"kind": "boh"}, "Tipo di servizio non valido"),
        ({"name": "il mio groq"}, "Esiste già"), ({"name": "Altro", "base_url": "ftp://x"}, "http:// o https://"),
        ({"name": "Altro", "base_url": "javascript:alert(1)"}, "http:// o https://"), ({"name": "Altro", "base_url": ""}, "http:// o https://"),
    ]:
        with pytest.raises(ValueError, match=fragment):
            add_groq(service, **kwargs)


def test_gemini_needs_a_key_but_not_an_address_and_a_local_server_needs_no_key(service):
    with pytest.raises(ValueError, match="serve la chiave"):
        service.ai_services.add("Gemini", "gemini", "", "")
    gemini = service.ai_services.add("Gemini", "gemini", "https://ignorato.example", "AIza-1")
    assert gemini.base_url == ""  # l'indirizzo di Gemini è incorporato nella libreria
    ollama = service.ai_services.add("Ollama", "openai", "http://localhost:11434/v1", "")
    assert ollama.api_key == "" and not ollama.has_key


def test_kinds_offered_to_the_admin():
    assert set(KINDS) == {"openai", "gemini"}


# --- Modifica ------------------------------------------------------------------


def test_update_keeps_the_key_when_left_blank_and_replaces_it_otherwise(service):
    added = add_groq(service)
    renamed = service.ai_services.update(added.id, "Groq gratuito", "openai", added.base_url, "")
    assert renamed.name == "Groq gratuito" and renamed.api_key == "gsk_segretissima"
    assert service.ai_services.update(added.id, "Groq gratuito", "openai", added.base_url, "gsk_nuova").api_key == "gsk_nuova"


def test_update_keeps_the_models_unless_the_address_or_kind_changes(service):
    added = add_groq(service)
    service.ai_services.set_models(added.id, ["a", "b"])
    assert service.ai_services.update(added.id, "Groq", "openai", added.base_url).models == ("a", "b")
    moved = service.ai_services.update(added.id, "Groq", "openai", "https://altro.example/v1")
    assert moved.models == ()  # elenco di un altro servizio: non vale più


def test_update_validates_and_does_not_collide_with_itself(service):
    first, second = add_groq(service), add_groq(service, name="DeepSeek", base_url="https://api.deepseek.com")
    service.ai_services.update(first.id, "Groq", "openai", first.base_url)  # stesso nome di prima: ok
    with pytest.raises(ValueError, match="Esiste già"):
        service.ai_services.update(second.id, "groq", "openai", second.base_url)
    with pytest.raises(ValueError, match="non trovato"):
        service.ai_services.update(999, "X", "openai", "http://x")
    keyless = service.ai_services.add("Locale", "openai", "http://localhost:11434/v1", "")  # senza chiave
    with pytest.raises(ValueError, match="serve la chiave"):
        service.ai_services.update(keyless.id, "Locale", "gemini", "", "")  # Gemini non può stare senza


def test_models_are_stored_as_a_list(service):
    added = add_groq(service)
    service.ai_services.set_models(added.id, ["m1", "m2"])
    assert service.ai_services.get(added.id).models == ("m1", "m2")
    service.db.execute("UPDATE ai_services SET models = 'non json' WHERE id = ?", (added.id,))
    assert service.ai_services.get(added.id).models == ()


def test_services_are_listed_by_name(service):
    for name in ("Zeta", "alfa", "Beta"):
        add_groq(service, name=name)
    assert [s.name for s in service.ai_services.all()] == ["alfa", "Beta", "Zeta"]


# --- Rimozione ---------------------------------------------------------------------


def test_removing_a_service_empties_the_slots_that_used_it(service):
    groq, gemini = add_groq(service), service.ai_services.add("Gemini", "gemini", "", "AIza")
    service.settings.update({
        "ai_docs_service": str(groq.id), "ai_docs_model": "m1", "ai_docs_backup_service": str(gemini.id), "ai_docs_backup_model": "g1",
        "ai_chat_service": str(gemini.id), "ai_chat_model": "g2", "ai_chat_backup_service": str(groq.id), "ai_chat_backup_model": "m2",
    })
    service.ai_services.remove(groq.id)
    read = service.settings.get
    assert (read("ai_docs_service"), read("ai_docs_model"), read("ai_chat_backup_service"), read("ai_chat_backup_model")) == ("", "", "", "")
    assert (read("ai_docs_backup_service"), read("ai_docs_backup_model"), read("ai_chat_service")) == (str(gemini.id), "g1", str(gemini.id))
    assert service.ai_services.get(groq.id) is None and service.ai.slot("docs")[0] is None


# --- Passaggio dalla configurazione precedente -------------------------------------------


def legacy(svc, **values):
    svc.settings.update(values)
    svc.settings.update({"ai_migrated": ""})  # come se il database fosse di una versione precedente


def test_a_fresh_install_migrates_to_nothing_and_only_once(service):
    service.ai_services.migrate_legacy()
    assert service.ai_services.all() == [] and service.settings.get("ai_migrated") == "1"
    service.settings.update({"groq_api_key": "gsk_dopo"})
    service.ai_services.migrate_legacy()  # già fatto: non si ripete
    assert service.ai_services.all() == []


def test_the_previous_setup_with_gemini_and_groq_becomes_services_and_choices(service):
    legacy(service, gemini_api_key="AIza-vecchia", gemini_model="gemini-3.8-flash", gemini_fallback_model="gemini-3.5-flash-lite",
           groq_api_key="gsk-vecchia", ai_docs_provider="groq", ai_docs_model="qwen/qwen3.8-27b",
           ai_chat_provider="groq", ai_chat_model="openai/gpt-oss-120b",
           ai_models_cache=json.dumps({"groq": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"], "gemini": ["gemini-3.8-flash"]}))
    service.ai_services.migrate_legacy()

    by_name = {s.name: s for s in service.ai_services.all()}
    assert set(by_name) == {"Google Gemini", "Groq"}
    assert (by_name["Groq"].kind, by_name["Groq"].base_url, by_name["Groq"].api_key) == ("openai", "https://api.groq.com/openai/v1", "gsk-vecchia")
    assert (by_name["Google Gemini"].kind, by_name["Google Gemini"].api_key) == ("gemini", "AIza-vecchia")
    assert by_name["Groq"].models == ("qwen/qwen3.8-27b", "openai/gpt-oss-120b") and by_name["Google Gemini"].models == ("gemini-3.8-flash",)

    docs, chat = service.ai.slot("docs"), service.ai.slot("chat")
    assert (docs[0].name, docs[1]) == ("Groq", "qwen/qwen3.8-27b") and (chat[0].name, chat[1]) == ("Groq", "openai/gpt-oss-120b")
    assert service.ai.slot("docs", True)[0] is None  # la riserva vecchia era di Gemini: qui i ruoli erano su Groq


def test_a_gemini_setup_keeps_its_model_and_its_reserve_as_the_backup(service):
    legacy(service, gemini_api_key="AIza-vecchia", gemini_model="gemini-3.8-flash", gemini_fallback_model="gemini-3.5-flash-lite")
    service.ai_services.migrate_legacy()
    for role in ("docs", "chat"):
        primary, backup = service.ai.slot(role), service.ai.slot(role, True)
        assert (primary[0].name, primary[1]) == ("Google Gemini", "gemini-3.8-flash")  # il modello predefinito diventa quello scelto
        assert (backup[0].name, backup[1]) == ("Google Gemini", "gemini-3.5-flash-lite")


def test_the_old_settings_are_emptied_after_the_migration(service):
    legacy(service, gemini_api_key="AIza-vecchia", groq_api_key="gsk", custom_api_key="k", custom_base_url="http://x/v1", ai_models_cache="{}")
    service.ai_services.migrate_legacy()
    for key in ("gemini_api_key", "groq_api_key", "deepseek_api_key", "custom_api_key", "custom_base_url", "ai_models_cache", "gemini_fallback_model"):
        assert service.settings.get(key) == "", key
    assert {s.name for s in service.ai_services.all()} == {"Google Gemini", "Groq", "Altro servizio"}
    assert service.ai_services.all()[0].api_key  # le chiavi ci sono, ora nei servizi (cifrate)


def test_a_custom_service_without_an_address_is_not_migrated(service):
    legacy(service, custom_api_key="k")
    service.ai_services.migrate_legacy()
    assert service.ai_services.all() == []


def test_a_broken_models_cache_does_not_stop_the_migration(service):
    legacy(service, groq_api_key="gsk", ai_models_cache="non json")
    service.ai_services.migrate_legacy()
    assert [s.name for s in service.ai_services.all()] == ["Groq"]


def test_migration_runs_at_startup_on_an_existing_database(tmp_path, root):
    from famiglia.service import Service

    old = Service(tmp_path / "data", "chiave-di-test", root)
    old.settings.update({"groq_api_key": "gsk-vecchia", "ai_chat_provider": "groq", "ai_chat_model": "openai/gpt-oss-120b", "ai_migrated": ""})
    reopened = Service(tmp_path / "data", "chiave-di-test", root)  # il riavvio dopo l'aggiornamento
    assert [s.name for s in reopened.ai_services.all()] == ["Groq"]
    assert reopened.ai.slot("chat")[1] == "openai/gpt-oss-120b"
