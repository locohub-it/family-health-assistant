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
