"""Visite con i pulsanti: nuova, elenco, modifica, elimina. Nessuna intelligenza artificiale nel percorso."""

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from telegram.ext import Application

from famiglia import clock
from famiglia.bot import MENU, UNDO_PREFIX
from famiglia.service import Service
from famiglia.visits import CONFIRM, FLOW_TTL, NEW, Visits, wants_list
from famiglia.when import WhenError, is_past, make_title, parse_when

from helpers import FakeAnswerer, FakeComposio, FakeReader, appuntamento, referto

TODAY = date(2026, 9, 19)
NOW = datetime(2026, 9, 19, 10, 0, tzinfo=clock.TIMEZONE)


# --- Data e ora scritte a mano ------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("26/10 alle 15", ("2026-10-26", "15:00")),
        ("26 ottobre 15:30", ("2026-10-26", "15:30")),
        ("26 Ottobre, alle ore 15", ("2026-10-26", "15:00")),
        ("domani alle 9", ("2026-09-20", "09:00")),
        ("dopodomani", ("2026-09-21", "")),
        ("oggi alle 18", ("2026-09-19", "18:00")),
        ("26/10/2026 15", ("2026-10-26", "15:00")),
        ("26/10/26 alle 15", ("2026-10-26", "15:00")),
        ("26-10 alle 9.30", ("2026-10-26", "09:30")),
        ("26/10 9.30", ("2026-10-26", "09:30")),
        ("alle 9 e mezza il 12/11", ("2026-11-12", "09:30")),
        ("1° novembre alle 10", ("2026-11-01", "10:00")),
        ("3 novembre 2026", ("2026-11-03", "")),
        ("26 ott ore 8.05", ("2026-10-26", "08:05")),
        ("il 26 ottobre 15", ("2026-10-26", "15:00")),
    ],
)
def test_dates_and_times_written_by_hand_are_read(text, expected):
    assert parse_when(text, TODAY) == expected


def test_without_a_year_a_date_that_already_passed_means_next_year():
    assert parse_when("26/03 alle 15", TODAY) == ("2027-03-26", "15:00")  # a settembre, «26 marzo» è l'anno prossimo
    assert parse_when("19/09", TODAY) == ("2026-09-19", "")  # oggi è ancora oggi


@pytest.mark.parametrize(
    "text",
    ["boh", "", "31/02 alle 15", "26/13", "26/10 alle 25", "26/10 alle 15:75", "26/10 15 16", "26 ottobre 15 e 7"],
)
def test_what_cannot_be_read_with_certainty_is_refused_not_guessed(text):
    with pytest.raises(WhenError):
        parse_when(text, TODAY)


def test_extra_words_are_ignored_but_the_confirmation_screen_shows_what_was_understood():
    assert parse_when("domani mattina", TODAY) == ("2026-09-20", "")


def test_a_digit_that_is_not_understood_is_never_silently_dropped():
    with pytest.raises(WhenError):
        parse_when("26/10 alle 15 stanza 7", TODAY)


def test_is_past_uses_the_time_only_when_there_is_one():
    assert is_past("2026-09-19", "09:59", NOW) and not is_past("2026-09-19", "10:01", NOW)
    assert not is_past("2026-09-19", "", NOW) and is_past("2026-09-18", "", NOW)


@pytest.mark.parametrize(
    "text,title",
    [
        ("dalla dottoressa Rossi", "Visita dalla dottoressa Rossi"),
        ("oculista", "Visita oculista"),
        ("prelievo del sangue", "Prelievo del sangue"),
        ("Ecografia addome", "Ecografia addome"),
        ("  ", "Visita medica"),
    ],
)
def test_titles_read_naturally_on_the_calendar(text, title):
    assert make_title(text) == title


@pytest.mark.parametrize(
    "text,action",
    [
        ("modifica appuntamento", "edit"),
        ("Elimina appuntamento", "delete"),
        ("  elimina   appuntamento. ", "delete"),
        ("modifica l'appuntamento", "edit"),
        ("elimina appuntamenti", "delete"),
    ],
)
def test_only_the_two_exact_phrases_open_the_lists(text, action):
    assert wants_list(text) == action


@pytest.mark.parametrize(
    "text",
    [
        "ho appuntamenti per il 26 marzo alle 15?",
        "ho un appuntamento il 26 marzo alle 15",
        "metti una visita dalla dottoressa il 26 ottobre alle 15",
        "prenota una visita",
        "elimina appuntamento del 26 ottobre",
        "vorrei modificare l'appuntamento",
        "appuntamento",
        "modifica",
    ],
)
def test_no_other_sentence_is_taken_for_a_command(text):
    assert wants_list(text) is None


# --- Servizio con calendario finto ------------------------------------------------------------------


@pytest.fixture
def make(tmp_path, root):
    def _make(users=2, composio=None, coordinator=False):
        composio = composio or FakeComposio(connected={"famiglia-111", "famiglia-222"})
        reader = FakeReader(appuntamento(date="2099-11-03"))
        answerer = FakeAnswerer("Risposta alla domanda.")
        svc = Service(tmp_path / "data", "chiave-di-test", root, reader=reader, answerer=answerer, composio_factory=lambda k: composio)
        svc.settings.update({"documents_dir": "Documenti", "composio_api_key": "ak_test"})
        svc.mario = svc.users.add(111, "Mario Rossi", "papà")
        svc.anna = svc.users.add(222, "Anna Bianchi", "mamma") if users > 1 else None
        if coordinator:
            svc.settings.update({"coordinator_user_id": str(svc.anna.id)})
        svc.composio, svc.answerer, svc.reader = composio, answerer, reader
        svc.visits = Visits(svc.users, svc.documents, now=lambda: NOW)
        return svc

    return _make


def created(svc):
    return [(args["summary"], user) for slug, args, user in svc.composio.executed if slug == "GOOGLECALENDAR_CREATE_EVENT"]


def deleted(svc):
    return [(args["event_id"], user) for slug, args, user in svc.composio.executed if slug == "GOOGLECALENDAR_DELETE_EVENT"]


async def walk_new_visit(svc, user, when="26/10/2099 alle 15", title="dalla dottoressa Rossi", patient=None, state=None):
    state = {} if state is None else state
    svc.visits.start_new(user, state)
    if state["step"] == "patient":  # con più persone si sceglie per chi: di solito per sé stessi
        await svc.visits.on_button(user, state, f"v:p:{(patient or user).id}")
    await svc.visits.on_text(user, state, when)
    summary = await svc.visits.on_text(user, state, title)
    return summary, state


# --- Nuova visita -------------------------------------------------------------------------------------


async def test_new_visit_asks_for_whom_when_there_is_more_than_one_person(make):
    svc = make()
    state = {}
    reply = svc.visits.start_new(svc.mario, state)
    labels = [label for row in reply.buttons for label, _ in row]
    assert labels == ["Per me", "Anna Bianchi", "✖️ Annulla"]


async def test_new_visit_skips_the_question_when_there_is_only_one_person(make):
    svc = make(users=1)
    reply = svc.visits.start_new(svc.mario, {})
    assert "Per quando" in reply.text


async def test_new_visit_full_walk_saves_the_visit_and_puts_it_on_the_calendar(make):
    svc = make()
    summary, state = await walk_new_visit(svc, svc.mario)
    assert "Controlla se va bene" in summary.text and "26/10/2099 alle 15:00" in summary.text
    assert "Visita dalla dottoressa Rossi" in summary.text
    assert [b for row in summary.buttons for b in row] == [("✅ Conferma", CONFIRM), ("✖️ Annulla", "v:x")]
    assert svc.db.execute("SELECT * FROM appointments") == []  # finché non si conferma non si salva niente

    done = await svc.visits.on_button(svc.mario, state, CONFIRM)
    assert "Ho salvato la visita «Visita dalla dottoressa Rossi» di Mario Rossi per il 26/10/2099 alle 15:00" in done.text
    assert "aggiunta al calendario di Mario Rossi" in done.text
    (row,) = svc.db.execute("SELECT * FROM appointments")
    assert (row["starts_at"], row["user_id"], row["event_id"]) == ("2099-10-26T15:00", svc.mario.id, "evt123")
    assert created(svc) == [("Visita dalla dottoressa Rossi", "famiglia-111")]
    assert [label for row_ in done.buttons for label, _ in row_] == ["✏️ Modifica", "🗑️ Elimina"]
    assert state == {}


async def test_a_visit_for_another_family_member_goes_on_their_calendar(make):
    svc = make()
    summary, state = await walk_new_visit(svc, svc.mario, patient=svc.anna)
    assert "Anna Bianchi" in summary.text
    done = await svc.visits.on_button(svc.mario, state, CONFIRM)
    assert "di Anna Bianchi" in done.text and created(svc) == [("Visita dalla dottoressa Rossi", "famiglia-222")]


async def test_the_coordinator_gets_a_copy_with_the_patient_name(make):
    svc = make(coordinator=True)
    _, state = await walk_new_visit(svc, svc.mario)
    done = await svc.visits.on_button(svc.mario, state, CONFIRM)
    assert "coordinatore" in done.text
    assert created(svc) == [("Visita dalla dottoressa Rossi", "famiglia-111"), ("Visita dalla dottoressa Rossi – Mario Rossi", "famiglia-222")]


async def test_a_visit_without_a_time_is_marked_to_be_confirmed(make):
    svc = make()
    summary, state = await walk_new_visit(svc, svc.mario, when="26/10/2099")
    assert "orario da confermare" in summary.text
    done = await svc.visits.on_button(svc.mario, state, CONFIRM)
    assert svc.db.execute("SELECT starts_at FROM appointments")[0]["starts_at"] == "2099-10-26" and "da confermare" in done.text


async def test_the_visit_is_saved_even_without_a_calendar(make):
    svc = make()
    svc.settings.update({"composio_api_key": ""})
    _, state = await walk_new_visit(svc, svc.mario)
    done = await svc.visits.on_button(svc.mario, state, CONFIRM)
    assert "Ho salvato la visita" in done.text and "calendario" not in done.text
    assert len(svc.db.execute("SELECT * FROM appointments")) == 1


async def test_cancelling_saves_nothing(make):
    svc = make()
    _, state = await walk_new_visit(svc, svc.mario)
    reply = await svc.visits.on_button(svc.mario, state, "v:x")
    assert "non ho cambiato niente" in reply.text and state == {}
    assert svc.db.execute("SELECT * FROM appointments") == [] and svc.composio.executed == []


@pytest.mark.parametrize("bad,fragment", [("boh", "Non ho capito la data"), ("26/10/2020", "già passata"), ("ieri", "Non ho capito")])
async def test_an_unreadable_or_past_date_is_asked_again_without_moving_on(make, bad, fragment):
    svc = make(users=1)
    state = {}
    svc.visits.start_new(svc.mario, state)
    reply = await svc.visits.on_text(svc.mario, state, bad)
    assert fragment in reply.text and state["step"] == "when"
    assert reply.buttons == [[("✖️ Annulla", "v:x")]]


async def test_a_confirm_button_without_a_walk_in_progress_is_harmless(make):
    svc = make()
    reply = await svc.visits.on_button(svc.mario, {}, CONFIRM)
    assert "scaduta" in reply.text and svc.db.execute("SELECT * FROM appointments") == []


async def test_a_walk_left_half_way_expires_so_it_does_not_swallow_a_later_question(make):
    svc = make(users=1)
    state = {}
    svc.visits.start_new(svc.mario, state)
    state["at"] -= FLOW_TTL + 1
    assert await svc.visits.on_text(svc.mario, state, "Come sto?") is None and state == {}


async def test_an_old_confirm_button_is_refused_after_expiry(make):
    svc = make(users=1)
    _, state = await walk_new_visit(svc, svc.mario)
    state["at"] -= FLOW_TTL + 1
    assert "scaduta" in (await svc.visits.on_button(svc.mario, state, CONFIRM)).text
    assert svc.db.execute("SELECT * FROM appointments") == []


async def test_an_invalid_person_in_the_button_is_refused(make):
    svc = make()
    state = {}
    svc.visits.start_new(svc.mario, state)
    assert "scaduta" in (await svc.visits.on_button(svc.mario, state, "v:p:9999")).text


async def test_a_question_is_never_taken_for_a_visit_when_no_walk_is_in_progress(make):
    svc = make()
    state = {}
    for text in ("ho appuntamenti per il 26 marzo alle 15?", "metti una visita il 26 ottobre alle 15", "Quando è la mia visita?"):
        assert await svc.visits.on_text(svc.mario, state, text) is None
    assert svc.db.execute("SELECT * FROM appointments") == [] and svc.composio.executed == []


# --- Elenco, modifica ed eliminazione -----------------------------------------------------------------------


async def saved(svc, user=None, patient=None, when="26/10/2099 alle 15", title="dalla dottoressa Rossi"):
    user = user or svc.mario
    _, state = await walk_new_visit(svc, user, when=when, title=title, patient=patient)
    done = await svc.visits.on_button(user, state, CONFIRM)
    return svc.db.execute("SELECT id FROM appointments ORDER BY id DESC LIMIT 1")[0]["id"], done


async def test_the_list_shows_upcoming_visits_with_both_buttons(make):
    svc = make(users=1)
    later, _ = await saved(svc, when="10/12/2099 alle 9", title="oculista")
    sooner, _ = await saved(svc)
    cards = svc.visits.list_cards(svc.mario)
    assert cards[0].text == "Ecco le tue prossime visite:"
    assert [c.buttons[0][0][1] for c in cards[1:]] == [f"a:e:{sooner}", f"a:e:{later}"]  # la più vicina per prima
    assert all(len(c.buttons[0]) == 2 for c in cards[1:])
    assert "26/10/2099 alle 15:00" in cards[1].text and "Visita dalla dottoressa Rossi" in cards[1].text


async def test_each_typed_phrase_shows_only_its_own_button(make):
    svc = make(users=1)
    await saved(svc)
    edit_cards, delete_cards = svc.visits.list_cards(svc.mario, "edit"), svc.visits.list_cards(svc.mario, "delete")
    assert "modificare" in edit_cards[0].text and [l for l, _ in edit_cards[1].buttons[0]] == ["✏️ Modifica"]
    assert "eliminare" in delete_cards[0].text and [l for l, _ in delete_cards[1].buttons[0]] == ["🗑️ Elimina"]


async def test_the_list_is_empty_with_a_way_to_add_one(make):
    svc = make(users=1)
    (reply,) = svc.visits.list_cards(svc.mario)
    assert "Non ho visite in programma" in reply.text and reply.buttons == [[("➕ Segna una visita", NEW)]]


async def test_past_visits_are_not_listed(make):
    svc = make(users=1)
    svc.records.add_appointment(svc.records.add_document(svc.mario.id, 111, "appuntamento", "", "", "", "{}"), svc.mario.id, "2020-01-01T10:00", "Vecchia", "", "")
    assert "Non ho visite" in svc.visits.list_cards(svc.mario)[0].text


async def test_edit_menu_offers_date_type_and_place(make):
    svc = make(users=1)
    aid, _ = await saved(svc)
    menu = await svc.visits.on_button(svc.mario, {}, f"a:e:{aid}")
    assert "Cosa vuoi cambiare?" in menu.text
    assert [label for row in menu.buttons for label, _ in row] == ["📅 Data e ora", "📝 Tipo di visita", "📍 Dove si fa", "↩️ Indietro"]


async def test_changing_the_date_recreates_the_event_on_the_calendar(make):
    svc = make(users=1)
    aid, _ = await saved(svc)
    state = {}
    await svc.visits.on_button(svc.mario, state, f"a:ew:{aid}")
    reply = await svc.visits.on_text(svc.mario, state, "3 novembre 2099 alle 9:30")
    assert "03/11/2099 alle 09:30" in reply.text and "Ho aggiornato il calendario di Mario Rossi" in reply.text
    assert svc.db.execute("SELECT starts_at, event_id FROM appointments")[0]["starts_at"] == "2099-11-03T09:30"
    assert deleted(svc) == [("evt123", "famiglia-111")]  # la vecchia
    assert len(created(svc)) == 2 and state == {}
    assert svc.db.execute("SELECT event_id FROM appointments")[0]["event_id"] == "evt124"  # la nuova


async def test_changing_type_and_place_keeps_the_date(make):
    svc = make(users=1)
    aid, _ = await saved(svc)
    for code, text in (("et", "oculista"), ("ep", "Studio di via Roma 5")):
        state = {}
        await svc.visits.on_button(svc.mario, state, f"a:{code}:{aid}")
        reply = await svc.visits.on_text(svc.mario, state, text)
    row = svc.db.execute("SELECT * FROM appointments")[0]
    assert (row["title"], row["place"], row["starts_at"]) == ("Visita oculista", "Studio di via Roma 5", "2099-10-26T15:00")
    assert "Dove: Studio di via Roma 5" in reply.text


async def test_a_dash_removes_the_place(make):
    svc = make(users=1)
    aid, _ = await saved(svc)
    for text in ("Studio di via Roma 5", "-"):
        state = {}
        await svc.visits.on_button(svc.mario, state, f"a:ep:{aid}")
        await svc.visits.on_text(svc.mario, state, text)
    assert svc.db.execute("SELECT place FROM appointments")[0]["place"] == ""


async def test_the_coordinator_copy_follows_the_change(make):
    svc = make(coordinator=True)
    aid, _ = await saved(svc)
    state = {}
    await svc.visits.on_button(svc.mario, state, f"a:ew:{aid}")
    await svc.visits.on_text(svc.mario, state, "3 novembre 2099 alle 9")
    assert sorted(deleted(svc)) == [("evt123", "famiglia-111"), ("evt124", "famiglia-222")]
    assert [user for _, user in created(svc)][2:] == ["famiglia-111", "famiglia-222"]


@pytest.mark.parametrize("bad,fragment", [("boh", "Non ho capito la data"), ("1/1/2020", "già passata")])
async def test_a_bad_new_date_changes_nothing_and_asks_again(make, bad, fragment):
    svc = make(users=1)
    aid, _ = await saved(svc)
    state = {}
    await svc.visits.on_button(svc.mario, state, f"a:ew:{aid}")
    calls = len(svc.composio.executed)
    reply = await svc.visits.on_text(svc.mario, state, bad)
    assert fragment in reply.text and state["kind"] == "edit"
    assert svc.db.execute("SELECT starts_at FROM appointments")[0]["starts_at"] == "2099-10-26T15:00"
    assert len(svc.composio.executed) == calls


async def test_cancelling_an_edit_goes_back_to_the_card(make):
    svc = make(users=1)
    aid, _ = await saved(svc)
    state = {}
    await svc.visits.on_button(svc.mario, state, f"a:ew:{aid}")
    reply = await svc.visits.on_button(svc.mario, state, f"a:b:{aid}")
    assert "26/10/2099" in reply.text and state == {}


async def test_deleting_asks_first_then_removes_the_visit_and_the_event(make):
    svc = make(users=1)
    aid, _ = await saved(svc)
    ask = await svc.visits.on_button(svc.mario, {}, f"a:d:{aid}")
    assert "Vuoi eliminare" in ask.text and [l for l, _ in ask.buttons[0]] == ["🗑️ Sì, elimina", "No, tienila"]
    assert len(svc.db.execute("SELECT * FROM appointments")) == 1 and deleted(svc) == []  # domandare non toglie niente

    done = await svc.visits.on_button(svc.mario, {}, f"a:dy:{aid}")
    assert "Ho eliminato la visita" in done.text and "sparita anche dal calendario" in done.text
    assert svc.db.execute("SELECT * FROM appointments") == [] and svc.db.execute("SELECT * FROM documents") == []
    assert deleted(svc) == [("evt123", "famiglia-111")]


async def test_declining_the_delete_keeps_everything(make):
    svc = make(users=1)
    aid, _ = await saved(svc)
    reply = await svc.visits.on_button(svc.mario, {}, f"a:b:{aid}")
    assert "26/10/2099" in reply.text and len(svc.db.execute("SELECT * FROM appointments")) == 1 and deleted(svc) == []


async def test_deleting_removes_the_coordinator_copy_too(make):
    svc = make(coordinator=True)
    aid, _ = await saved(svc)
    done = await svc.visits.on_button(svc.mario, {}, f"a:dy:{aid}")
    assert "dai calendari" in done.text and sorted(deleted(svc)) == [("evt123", "famiglia-111"), ("evt124", "famiglia-222")]


async def test_a_calendar_failure_while_deleting_still_removes_the_visit_and_says_so(make):
    composio = FakeComposio(connected={"famiglia-111"})
    svc = make(users=1, composio=composio)
    aid, _ = await saved(svc)
    real = composio._execute
    composio.tools.execute = lambda slug, *a, **kw: (_ for _ in ()).throw(ConnectionError("giù")) if slug.endswith("DELETE_EVENT") else real(slug, *a, **kw)
    done = await svc.visits.on_button(svc.mario, {}, f"a:dy:{aid}")
    assert "va cancellata a mano" in done.text and svc.db.execute("SELECT * FROM appointments") == []


async def test_deleting_a_visit_from_a_photo_keeps_the_photo_record(make, root):
    svc = make(users=1)
    outcome = await svc.documents.process(svc.mario, b"\xff\xd8\xff jpeg", "image/jpeg")
    aid = svc.db.execute("SELECT id FROM appointments")[0]["id"]
    await svc.visits.on_button(svc.mario, {}, f"a:dy:{aid}")
    assert svc.db.execute("SELECT * FROM appointments") == []
    document = svc.records.get_document(outcome.document_id)
    assert document is not None and (root / document["file_path"]).exists()  # la foto della prenotazione non si tocca


async def test_a_visit_from_a_photo_can_be_edited_too(make):
    svc = make(users=1)
    await svc.documents.process(svc.mario, b"\xff\xd8\xff jpeg", "image/jpeg")
    aid = svc.db.execute("SELECT id FROM appointments")[0]["id"]
    state = {}
    await svc.visits.on_button(svc.mario, state, f"a:ew:{aid}")
    await svc.visits.on_text(svc.mario, state, "5 dicembre 2099 alle 11")
    assert svc.db.execute("SELECT starts_at FROM appointments")[0]["starts_at"] == "2099-12-05T11:00"


async def test_nobody_can_touch_the_visits_of_someone_they_do_not_manage(make):
    svc = make()
    aid, _ = await saved(svc)  # visita di Mario, segnata da Mario
    for code in ("e", "ew", "d", "dy"):
        reply = await svc.visits.on_button(svc.anna, {}, f"a:{code}:{aid}")
        assert "non c'è più, oppure non puoi cambiarla" in reply.text
    assert len(svc.db.execute("SELECT * FROM appointments")) == 1 and deleted(svc) == []
    assert "Non ho visite" in svc.visits.list_cards(svc.anna)[0].text  # nemmeno nell'elenco


async def test_whoever_booked_for_a_relative_can_manage_that_visit(make):
    svc = make()
    aid, _ = await saved(svc, user=svc.mario, patient=svc.anna)  # Mario la segna per Anna
    assert "Anna Bianchi" in svc.visits.list_cards(svc.mario)[1].text
    assert "Ho eliminato" in (await svc.visits.on_button(svc.mario, {}, f"a:dy:{aid}")).text


async def test_buttons_of_a_visit_that_no_longer_exists_say_so(make):
    svc = make(users=1)
    aid, _ = await saved(svc)
    await svc.visits.on_button(svc.mario, {}, f"a:dy:{aid}")
    assert "non c'è più" in (await svc.visits.on_button(svc.mario, {}, f"a:e:{aid}")).text
    assert "non c'è più" in (await svc.visits.on_button(svc.mario, {}, f"a:dy:{aid}")).text


@pytest.mark.parametrize("data", ["", "x", "a:zz", "a:e:abc", "v:p:", "a:xx:1", "v:ok:1"])
async def test_garbage_button_codes_are_harmless(make, data):
    svc = make(users=1)
    reply = await svc.visits.on_button(svc.mario, {}, data)
    assert reply.text and svc.db.execute("SELECT * FROM appointments") == []


# --- Dal bot ----------------------------------------------------------------------------------------------------


class Message:
    def __init__(self, text=None):
        self.text, self.replies, self.photo, self.voice, self.audio, self.document = text, [], [], None, None, None
        self.chat = SimpleNamespace(send_action=self._noop)

    async def _noop(self, *args, **kwargs):
        pass

    async def reply_text(self, text, reply_markup=None, **kwargs):
        ack = SimpleNamespace(text=text, markup=reply_markup)

        async def edit_text(new, reply_markup=None):
            ack.text, ack.markup = new, reply_markup

        ack.edit_text = edit_text
        self.replies.append(ack)
        return ack


def update(user_id, message):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(type="private"), effective_message=message)


def context():
    return SimpleNamespace(user_data={})


def labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def codes(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class Tap:
    """Un tocco su un pulsante: registra la risposta con cui il bot sostituisce il messaggio."""

    def __init__(self, data):
        self.data, self.edited, self.answered = data, [], False
        self.message = Message()

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None):
        self.edited.append((text, reply_markup))


def tap_update(user_id, data):
    tap = Tap(data)
    return SimpleNamespace(callback_query=tap, effective_user=SimpleNamespace(id=user_id), effective_message=tap.message), tap


async def test_visita_command_starts_the_walk_and_the_next_texts_belong_to_it(make):
    svc = make(users=1)
    ctx = context()
    message = Message()
    await svc.bot._new_visit(update(111, message), ctx)
    assert "Per quando" in message.replies[0].text and "✖️ Annulla" in labels(message.replies[0].markup)

    for text in ("26/10/2099 alle 15", "dalla dottoressa Rossi"):
        reply = Message(text)
        await svc.bot._text(update(111, reply), ctx)
    assert "Controlla se va bene" in reply.replies[0].text and codes(reply.replies[0].markup) == [CONFIRM, "v:x"]
    assert svc.answerer.calls == [] and svc.db.execute("SELECT * FROM appointments") == []


async def test_the_confirm_button_saves_and_edits_the_message_in_place(make):
    svc = make(users=1)
    ctx = context()
    await svc.bot._new_visit(update(111, Message()), ctx)
    for text in ("26/10/2099 alle 15", "oculista"):
        await svc.bot._text(update(111, Message(text)), ctx)
    upd, tap = tap_update(111, CONFIRM)
    await svc.bot._visit_button(upd, ctx)
    text, markup = tap.edited[0]
    assert tap.answered and "Ho salvato la visita «Visita oculista»" in text and labels(markup) == ["✏️ Modifica", "🗑️ Elimina"]
    assert len(svc.db.execute("SELECT * FROM appointments")) == 1


async def test_a_question_written_after_the_walk_ended_is_answered_normally(make):
    svc = make(users=1)
    ctx = context()
    message = Message("ho appuntamenti per il 26 marzo alle 15?")
    await svc.bot._text(update(111, message), ctx)
    assert message.replies[0].text == "Risposta alla domanda." and svc.answerer.calls[0]["question"] == "ho appuntamenti per il 26 marzo alle 15?"
    assert svc.db.execute("SELECT * FROM appointments") == [] and svc.composio.executed == []


async def test_appuntamenti_command_lists_the_visits_with_buttons(make):
    svc = make(users=1)
    aid, _ = await saved(svc)
    message = Message()
    await svc.bot._list_visits(update(111, message), context())
    header, card = message.replies
    assert "prossime visite" in header.text and codes(card.markup) == [f"a:e:{aid}", f"a:d:{aid}"]


@pytest.mark.parametrize("phrase,button", [("modifica appuntamento", "✏️ Modifica"), ("elimina appuntamento", "🗑️ Elimina")])
async def test_the_typed_phrases_show_the_matching_list(make, phrase, button):
    svc = make(users=1)
    await saved(svc)
    message = Message(phrase)
    await svc.bot._text(update(111, message), context())
    assert labels(message.replies[1].markup) == [button] and svc.answerer.calls == []


async def test_a_typed_phrase_interrupts_a_walk_in_progress(make):
    svc = make(users=1)
    ctx = context()
    await svc.bot._new_visit(update(111, Message()), ctx)
    message = Message("elimina appuntamento")
    await svc.bot._text(update(111, message), ctx)
    assert ctx.user_data == {} and "Non ho visite" in message.replies[0].text


async def test_delete_flow_through_the_bot_buttons(make):
    svc = make(users=1)
    aid, _ = await saved(svc)
    ctx = context()
    upd, ask = tap_update(111, f"a:d:{aid}")
    await svc.bot._visit_button(upd, ctx)
    assert "Vuoi eliminare" in ask.edited[0][0]
    upd, done = tap_update(111, f"a:dy:{aid}")
    await svc.bot._visit_button(upd, ctx)
    assert "Ho eliminato la visita" in done.edited[0][0] and svc.db.execute("SELECT * FROM appointments") == []


async def test_a_button_pressed_by_someone_else_changes_nothing(make):
    svc = make()
    aid, _ = await saved(svc)
    upd, tap = tap_update(222, f"a:dy:{aid}")
    await svc.bot._visit_button(upd, context())
    assert "non puoi cambiarla" in tap.edited[0][0] and len(svc.db.execute("SELECT * FROM appointments")) == 1


async def test_two_people_never_share_the_same_walk(make):
    svc = make()
    mario_ctx, anna_ctx = context(), context()
    await svc.bot._new_visit(update(111, Message()), mario_ctx)
    message = Message("Come sto?")
    await svc.bot._text(update(222, message), anna_ctx)  # Anna non ha nessun passaggio in corso
    assert message.replies[0].text == "Risposta alla domanda."


async def test_a_calendar_failure_on_a_button_gets_a_friendly_reply(make):
    composio = FakeComposio(connected={"famiglia-111"})
    svc = make(users=1, composio=composio)
    aid, _ = await saved(svc)
    composio.connected_accounts.list = lambda **kw: (_ for _ in ()).throw(ConnectionError("giù"))
    ctx = context()
    upd, tap = tap_update(111, f"a:ew:{aid}")
    await svc.bot._visit_button(upd, ctx)
    message = Message("3 novembre 2099 alle 9")
    await svc.bot._text(update(111, message), ctx)
    assert message.replies[0].text  # una risposta c'è sempre, anche se il calendario non risponde


def test_new_handlers_and_menu_are_registered_behind_the_gate(make):
    svc = make(users=1)
    app = Application.builder().token("1:abc").build()
    svc.bot._register(app)
    assert min(app.handlers) == -1 and len(app.handlers[-1]) == 1
    names = {h.callback.__name__ for group in app.handlers.values() for h in group}
    assert {"_new_visit", "_list_visits", "_visit_button"} <= names
    assert [name for name, _ in MENU] == ["visita", "appuntamenti", "calendario"]


async def test_the_command_menu_is_published_and_a_failure_does_not_stop_the_bot(make):
    from telegram.error import TelegramError

    svc = make(users=1)
    sent = []

    async def set_my_commands(commands):
        sent.append(commands)

    await svc.bot._publish_menu(SimpleNamespace(bot=SimpleNamespace(set_my_commands=set_my_commands)))
    assert sent == [MENU]

    async def broken(commands):
        raise TelegramError("no")

    await svc.bot._publish_menu(SimpleNamespace(bot=SimpleNamespace(set_my_commands=broken)))  # non solleva


async def test_start_and_unknown_command_point_to_the_new_commands(make):
    svc = make(users=1)
    message = Message()
    await svc.bot._start(update(111, message), context())
    assert "/visita" in message.replies[0].text and "/appuntamenti" in message.replies[0].text
    other = Message()
    await svc.bot._unknown_command(update(111, other), context())
    assert "/visita" in other.replies[0].text
