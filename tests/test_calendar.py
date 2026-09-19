from datetime import datetime

import pytest

from famiglia import clock
from famiglia.calendar import CalendarError
from famiglia.service import Service

from helpers import FakeComposio, FakeReader, appuntamento, referto

JPEG = b"\xff\xd8\xff fake jpeg"
NOW = datetime(2026, 9, 19, 10, 0, tzinfo=clock.TIMEZONE)


@pytest.fixture
def make(tmp_path, root):
    def _make(composio=None, extraction=None, key="ak_test"):
        composio = composio or FakeComposio()
        svc = Service(
            tmp_path / "data", "chiave-di-test", root, reader=FakeReader(extraction), composio_factory=lambda k: composio
        )
        svc.settings.update({"documents_dir": "Documenti", "composio_api_key": key})
        svc.mario = svc.users.add(111, "Mario Rossi")
        svc.composio = composio
        return svc

    return _make


# --- Calendar ------------------------------------------------------------------


async def test_event_is_created_on_the_users_calendar_without_meet_link(make):
    svc = make(FakeComposio(connected={"famiglia-111"}))
    event_id = await svc.calendar.create_event(
        svc.mario, "2026-11-03T09:30", "Visita cardiologica", "Ospedale Nord", "Portare la tessera", now=NOW
    )
    assert event_id == "evt123"
    slug, args, user_id = svc.composio.executed[0]
    assert (slug, user_id) == ("GOOGLECALENDAR_CREATE_EVENT", "famiglia-111")
    assert args == {
        "calendar_id": "primary",
        "summary": "Visita cardiologica",
        "start_datetime": "2026-11-03T09:30:00",
        "timezone": "Europe/Rome",
        "event_duration_hour": 1,
        "event_duration_minutes": 0,
        "location": "Ospedale Nord",
        "description": "Portare la tessera",
        "create_meeting_room": False,
        "send_updates": "none",
    }


async def test_empty_place_and_notes_are_not_sent(make):
    svc = make(FakeComposio(connected={"famiglia-111"}))
    await svc.calendar.create_event(svc.mario, "2026-11-03T09:30", "Visita", now=NOW)
    args = svc.composio.executed[0][1]
    assert "location" not in args and "description" not in args


async def test_event_without_time_is_marked_to_be_confirmed(make):
    svc = make(FakeComposio(connected={"famiglia-111"}))
    await svc.calendar.create_event(svc.mario, "2026-11-03", "Visita", now=NOW)
    args = svc.composio.executed[0][1]
    assert args["summary"] == "Visita (orario da confermare)" and args["start_datetime"] == "2026-11-03T09:00:00"


async def test_past_date_is_not_scheduled_but_today_without_time_is(make):
    svc = make(FakeComposio(connected={"famiglia-111"}))
    with pytest.raises(CalendarError, match="già passata"):
        await svc.calendar.create_event(svc.mario, "2026-09-18T09:30", "Visita", now=NOW)
    with pytest.raises(CalendarError, match="già passata"):
        await svc.calendar.create_event(svc.mario, "2026-09-19T09:30", "Visita", now=NOW)  # oggi ma alle 9:30, ora sono le 10
    await svc.calendar.create_event(svc.mario, "2026-09-19", "Visita", now=NOW)  # oggi, ora da confermare
    assert len(svc.composio.executed) == 1


async def test_not_connected_user_gets_a_clear_message_and_no_event(make):
    svc = make(FakeComposio(connected=set()))
    with pytest.raises(CalendarError, match="non è ancora collegato"):
        await svc.calendar.create_event(svc.mario, "2026-11-03T09:30", "Visita", now=NOW)
    assert svc.composio.executed == []


async def test_composio_refusal_and_crash_become_friendly_errors(make):
    svc = make(FakeComposio(connected={"famiglia-111"}, execute_error="quota superata"))
    with pytest.raises(CalendarError) as exc:
        await svc.calendar.create_event(svc.mario, "2026-11-03T09:30", "Visita", now=NOW)
    assert "rifiutato" in exc.value.user_message and "quota superata" in str(exc.value)

    def boom(key):
        raise ConnectionError("rete assente")

    svc.calendar._factory, svc.calendar._client = boom, None
    with pytest.raises(CalendarError) as crash:
        await svc.calendar.is_connected(svc.mario)
    assert "Non riesco a usare Google Calendar" in crash.value.user_message
    assert "rete assente" in str(crash.value)  # il dettaglio tecnico resta per il registro, non per l'utente


async def test_without_composio_key_calendar_is_disabled(make):
    svc = make(key="")
    assert not svc.calendar.enabled
    with pytest.raises(CalendarError, match="manca la chiave Composio"):
        await svc.calendar.connect_link(svc.mario)


async def test_connect_link_and_connected_users(make):
    svc = make(FakeComposio(connected={"famiglia-111"}))
    anna = svc.users.add(222, "Anna")
    assert await svc.calendar.connect_link(anna) == "https://connect.composio.dev/link/ln_abc"
    assert svc.composio.authorized == ["famiglia-222:googlecalendar"]
    assert await svc.calendar.connected_user_ids([svc.mario, anna]) == {"famiglia-111"}
    assert svc.composio.list_calls[0]["toolkit_slugs"] == ["googlecalendar"]


async def test_expired_or_revoked_connection_does_not_count_as_connected(make):
    svc = make(FakeComposio(connected=set(), expired={"famiglia-111"}))
    assert await svc.calendar.is_connected(svc.mario) is False
    with pytest.raises(CalendarError, match="non è ancora collegato"):
        await svc.calendar.create_event(svc.mario, "2026-11-03T09:30", "Visita", now=NOW)


async def test_list_calendars(make):
    svc = make(FakeComposio(connected={"famiglia-111"}))
    calendars = await svc.calendar.list_calendars(svc.mario)
    assert [(c.id, c.name, c.primary) for c in calendars] == [
        ("mario@example.com", "Mario", True),
        ("famiglia@group.calendar.google.com", "Famiglia", False),
    ]


# --- Integrazione con i documenti ----------------------------------------------


async def test_appointment_photo_ends_up_on_the_calendar(make):
    svc = make(FakeComposio(connected={"famiglia-111"}), appuntamento(date="2027-01-15", time="10:00"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "📅 L'ho aggiunta al calendario di Mario Rossi." in outcome.text
    assert svc.db.execute("SELECT event_id FROM appointments")[0]["event_id"] == "evt123"


async def test_appointment_without_time_says_which_hour_was_used(make):
    svc = make(FakeComposio(connected={"famiglia-111"}), appuntamento(date="2027-01-15", time=""))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "ho messo le 09:00, da confermare" in outcome.text


async def test_calendar_failure_does_not_lose_the_appointment(make):
    svc = make(FakeComposio(connected=set()), appuntamento(date="2027-01-15"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "⚠️ Il Google Calendar di questa persona non è ancora collegato." in outcome.text
    assert outcome.document_id and svc.db.execute("SELECT * FROM appointments")
    assert svc.db.execute("SELECT event_id FROM appointments")[0]["event_id"] == ""
    assert any(r["kind"] == "errore" and "non è ancora collegato" in r["detail"] for r in svc.recent_activity(5))


async def test_without_composio_key_the_appointment_is_saved_silently(make):
    svc = make(extraction=appuntamento(date="2027-01-15"), key="")
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "calendario" not in outcome.text and svc.db.execute("SELECT * FROM appointments")


async def test_lab_reports_never_touch_the_calendar(make):
    svc = make(FakeComposio(connected={"famiglia-111"}), referto())
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert svc.composio.executed == [] and svc.composio.list_calls == []


async def test_undo_deletes_the_calendar_event(make):
    svc = make(FakeComposio(connected={"famiglia-111"}), appuntamento(date="2027-01-15", time="10:00"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    message = await svc.documents.undo(svc.mario, outcome.document_id)
    assert "Ho tolto anche la visita dal calendario" in message
    slug, args, user_id = svc.composio.executed[-1]
    assert (slug, args["event_id"], user_id) == ("GOOGLECALENDAR_DELETE_EVENT", "evt123", "famiglia-111")
    assert svc.db.execute("SELECT * FROM documents") == []


async def test_undo_still_removes_the_document_if_the_calendar_fails(make):
    svc = make(FakeComposio(connected={"famiglia-111"}), appuntamento(date="2027-01-15", time="10:00"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    svc.composio.execute_error = "non autorizzato"
    message = await svc.documents.undo(svc.mario, outcome.document_id)
    assert "va cancellata a mano" in message
    assert svc.db.execute("SELECT * FROM documents") == []


# --- Chiave Composio sbagliata -------------------------------------------------


class AuthenticationError(Exception):
    """Come composio_client.AuthenticationError: porta lo status HTTP."""

    status_code = 401


async def test_invalid_composio_key_gets_a_clear_message_with_the_real_detail(make):
    composio = FakeComposio()

    def refuse(**kwargs):
        raise AuthenticationError("Error code: 401 - {'error': {'message': 'Invalid API key: ck_**Tf9l'}}")

    composio.connected_accounts.list = refuse
    svc = make(composio)
    with pytest.raises(CalendarError) as exc:
        await svc.calendar.is_connected(svc.mario)
    assert "chiave Composio non è valida" in exc.value.user_message and "ak_" in exc.value.user_message
    assert "Invalid API key: ck_**Tf9l" in str(exc.value)  # il dettaglio resta per il pannello


async def test_a_403_permission_error_gets_the_same_clear_message(make):
    composio = FakeComposio()

    class Forbidden(Exception):
        status_code = 403

    composio.toolkits.authorize = lambda **kw: (_ for _ in ()).throw(Forbidden("no"))
    svc = make(composio)
    with pytest.raises(CalendarError) as exc:
        await svc.calendar.connect_link(svc.mario)
    assert "non ha i permessi" in exc.value.user_message and "Forbidden: no" in str(exc.value)


# --- Coordinatore --------------------------------------------------------------


@pytest.fixture
def family(make):
    """Mario (paziente), Luca (coordinatore); entrambi con il calendario collegato."""

    def _family(extraction=None, connected=("famiglia-111", "famiglia-333"), coordinator=True):
        composio = FakeComposio(connected=set(connected))
        svc = make(composio, extraction or appuntamento(date="2027-01-15", time="10:00", notes="Portare la tessera"))
        svc.luca = svc.users.add(333, "Luca", "figlio", "Luca", "Rossi")
        if coordinator:
            svc.settings.update({"coordinator_user_id": str(svc.luca.id)})
        return svc

    return _family


def creates(svc):
    return [(user, args) for slug, args, user in svc.composio.executed if slug == "GOOGLECALENDAR_CREATE_EVENT"]


async def test_coordinator_gets_a_copy_with_the_patient_name(family):
    svc = family()
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    (patient_user, patient_args), (coord_user, coord_args) = creates(svc)
    assert patient_user == "famiglia-111" and patient_args["summary"] == "Visita cardiologica"
    assert coord_user == "famiglia-333" and coord_args["summary"] == "Visita cardiologica – Mario Rossi"
    assert coord_args["description"] == "Paziente: Mario Rossi\nPortare la tessera"
    assert coord_args["location"] == "Ospedale Nord" and coord_args["start_datetime"] == patient_args["start_datetime"]
    assert "📅 L'ho aggiunta al calendario di Mario Rossi." in outcome.text
    assert "📅 E anche a quello di Luca (coordinatore)." in outcome.text
    row = svc.db.execute("SELECT * FROM appointments")[0]
    assert (row["event_id"], row["coordinator_event_id"], row["coordinator_user_id"]) == ("evt123", "evt124", svc.luca.id)


async def test_no_duplicate_when_the_patient_is_the_coordinator(family):
    svc = family(coordinator=False)
    svc.settings.update({"coordinator_user_id": str(svc.mario.id)})
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert len(creates(svc)) == 1 and "coordinatore" not in outcome.text


async def test_without_a_coordinator_nothing_changes(family):
    svc = family(coordinator=False)
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert len(creates(svc)) == 1
    assert svc.db.execute("SELECT coordinator_event_id FROM appointments")[0]["coordinator_event_id"] == ""


async def test_coordinator_still_gets_it_if_the_patient_calendar_is_not_connected(family):
    svc = family(connected=("famiglia-333",))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert [u for u, _ in creates(svc)] == ["famiglia-333"]
    assert "⚠️ Il Google Calendar di questa persona non è ancora collegato." in outcome.text
    assert "📅 E anche a quello di Luca (coordinatore)." in outcome.text


async def test_patient_keeps_the_event_if_the_coordinator_calendar_is_not_connected(family):
    svc = family(connected=("famiglia-111",))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert [u for u, _ in creates(svc)] == ["famiglia-111"]
    assert "📅 L'ho aggiunta al calendario di Mario Rossi." in outcome.text
    assert "⚠️ Non sono riuscito ad aggiungerla al calendario di Luca: " in outcome.text
    assert svc.db.execute("SELECT event_id FROM appointments")[0]["event_id"] == "evt123"
    assert any("(coordinatore)" in r["detail"] for r in svc.recent_activity(10) if r["kind"] == "errore")


async def test_past_visit_gives_a_single_warning_and_no_calls(family):
    svc = family(appuntamento(date="2020-01-15", time="10:00"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert outcome.text.count("già passata") == 1 and creates(svc) == []


async def test_undo_removes_the_visit_from_both_calendars(family):
    svc = family()
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    message = await svc.documents.undo(svc.mario, outcome.document_id)
    deletes = [(user, args["event_id"]) for slug, args, user in svc.composio.executed if slug == "GOOGLECALENDAR_DELETE_EVENT"]
    assert deletes == [("famiglia-111", "evt123"), ("famiglia-333", "evt124")]
    assert "Ho tolto anche la visita dai calendari" in message


async def test_undo_uses_the_coordinator_that_actually_received_the_copy(family):
    svc = family()
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    anna = svc.users.add(444, "Anna")
    svc.settings.update({"coordinator_user_id": str(anna.id)})  # il coordinatore cambia dopo
    await svc.documents.undo(svc.mario, outcome.document_id)
    deletes = [user for slug, _, user in svc.composio.executed if slug == "GOOGLECALENDAR_DELETE_EVENT"]
    assert deletes == ["famiglia-111", "famiglia-333"]


async def test_undo_when_the_coordinator_was_removed_warns_but_removes_the_rest(family):
    svc = family()
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    svc.users.remove(svc.luca.id)
    message = await svc.documents.undo(svc.mario, outcome.document_id)
    assert "va cancellata a mano" in message
    assert [u for s, _, u in svc.composio.executed if s == "GOOGLECALENDAR_DELETE_EVENT"] == ["famiglia-111"]
    assert svc.db.execute("SELECT * FROM documents") == []


async def test_a_coordinator_setting_pointing_to_a_missing_user_is_ignored(family):
    svc = family(coordinator=False)
    svc.settings.update({"coordinator_user_id": "9999"})
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert len(creates(svc)) == 1 and "coordinatore" not in outcome.text


def test_existing_databases_get_the_coordinator_columns(tmp_path):
    import sqlite3

    from famiglia.db import Database

    path = tmp_path / "vecchio.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE appointments (id INTEGER PRIMARY KEY, document_id INTEGER, user_id INTEGER, starts_at TEXT, title TEXT, place TEXT, notes TEXT, event_id TEXT, created_at REAL)")
    old.execute("INSERT INTO appointments VALUES (1, 1, 1, '2027-01-15', 'Visita', '', '', 'evtX', 0)")
    old.commit()
    old.close()
    row = Database(path).execute("SELECT * FROM appointments")[0]
    assert (row["event_id"], row["coordinator_event_id"], row["coordinator_user_id"]) == ("evtX", "", 0)
