import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import InvalidToken, NetworkError
from telegram.ext import ApplicationHandlerStop

from famiglia.bot import UNDO_PREFIX, _scrub
from famiglia.gemini import GeminiError
from famiglia.service import Service

from helpers import FakeReader, referto

JPEG = b"\xff\xd8\xff fake jpeg"


class FakeAck:
    def __init__(self, text):
        self.text, self.markup = text, None

    async def edit_text(self, text, reply_markup=None):
        self.text, self.markup = text, reply_markup


class FakeMessage:
    def __init__(self, photo=None, document=None, data=JPEG, download_error=None):
        self.photo, self.document = photo or [], document
        self.replies: list[FakeAck] = []
        self.chat = SimpleNamespace(send_action=self._noop)
        self._data, self._download_error = data, download_error

    async def _noop(self, *args, **kwargs):
        pass

    async def reply_text(self, text, **kwargs):
        ack = FakeAck(text)
        self.replies.append(ack)
        return ack

    def attachment(self, mime="image/jpeg", size=1000):
        async def get_file():
            if self._download_error:
                raise self._download_error
            return SimpleNamespace(download_as_bytearray=self._download)

        return SimpleNamespace(mime_type=mime, file_size=size, get_file=get_file)

    async def _download(self):
        return bytearray(self._data)


def photo_update(user_id, message):
    message.photo = [message.attachment()]
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(type="private"), effective_message=message
    )


@pytest.fixture
def svc(tmp_path, root):
    reader = FakeReader(referto())
    svc = Service(tmp_path / "data", "chiave-di-test", root, reader=reader)
    svc.settings.update({"documents_dir": "Documenti"})
    svc.mario = svc.users.add(111, "Mario Rossi")
    svc.reader = reader
    return svc


# --- Sicurezza -----------------------------------------------------------------


@pytest.mark.parametrize(
    "user,chat",
    [
        (SimpleNamespace(id=999), SimpleNamespace(type="private")),  # ID non approvato
        (SimpleNamespace(id=111), SimpleNamespace(type="group")),  # approvato ma in un gruppo
        (SimpleNamespace(id=111), SimpleNamespace(type="supergroup")),
        (SimpleNamespace(id=111), SimpleNamespace(type="channel")),
        (None, SimpleNamespace(type="private")),
        (SimpleNamespace(id=111), None),
    ],
)
async def test_gate_stops_everyone_but_approved_users_in_private(svc, user, chat):
    update = SimpleNamespace(effective_user=user, effective_chat=chat)
    with pytest.raises(ApplicationHandlerStop):
        await svc.bot._gate(update, None)


async def test_gate_lets_approved_user_through_and_follows_the_panel(svc):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=222), effective_chat=SimpleNamespace(type="private"))
    with pytest.raises(ApplicationHandlerStop):
        await svc.bot._gate(update, None)
    svc.users.add(222, "Anna")  # aggiunta dal pannello mentre il bot è acceso
    assert await svc.bot._gate(update, None) is None


def test_gate_is_registered_before_every_other_handler(svc):
    from telegram.ext import Application

    app = Application.builder().token("1:abc").build()
    svc.bot._register(app)
    assert min(app.handlers) == -1 and len(app.handlers[-1]) == 1


def test_token_never_appears_in_messages():
    assert _scrub("errore su https://api.telegram.org/bot123:ABC/getMe", "123:ABC") == "errore su https://api.telegram.org/bot***/getMe"
    assert _scrub("niente", "") == "niente"


# --- Flusso documento ----------------------------------------------------------


async def test_photo_gets_an_immediate_ack_then_the_result_with_undo_button(svc):
    message = FakeMessage()
    await svc.bot._document(photo_update(111, message), None)

    (ack,) = message.replies
    assert "referto di Mario Rossi" in ack.text  # la risposta immediata è stata sostituita dal risultato
    button = ack.markup.inline_keyboard[0][0]
    assert button.callback_data.startswith(UNDO_PREFIX) and "Annulla" in button.text
    assert svc.reader.calls == [(len(JPEG), "image/jpeg")]


async def test_undo_button_removes_the_document(svc):
    message = FakeMessage()
    await svc.bot._document(photo_update(111, message), None)
    document_id = int(message.replies[0].markup.inline_keyboard[0][0].callback_data.removeprefix(UNDO_PREFIX))

    edited = []

    async def answer():
        pass

    async def edit(text):
        edited.append(text)

    query = SimpleNamespace(data=f"{UNDO_PREFIX}{document_id}", answer=answer, edit_message_text=edit)
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=111))
    await svc.bot._undo(update, None)

    assert edited == ["Annullato: ho tolto il documento."]
    assert svc.records.get_document(document_id) is None


@pytest.mark.parametrize("mime", ["video/mp4", "text/plain", ""])
async def test_unsupported_file_type_is_refused_without_calling_gemini(svc, mime):
    message = FakeMessage(document=None)
    message.document = message.attachment(mime)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=111), effective_message=message)
    await svc.bot._document(update, None)
    assert "non lo so leggere" in message.replies[0].text and svc.reader.calls == []


async def test_oversized_file_is_refused_before_downloading(svc):
    message = FakeMessage()
    update = photo_update(111, message)
    message.photo = [message.attachment(size=50 * 1024 * 1024)]
    await svc.bot._document(update, None)
    assert "troppo grande" in message.replies[0].text and svc.reader.calls == []


@pytest.mark.parametrize(
    "error,fragment",
    [
        (GeminiError("Gemini ha ricevuto troppe richieste."), "troppe richieste"),
        (RuntimeError("bug"), "Qualcosa è andato storto"),
    ],
)
async def test_errors_always_end_with_a_friendly_reply_and_no_button(svc, error, fragment):
    svc.reader.error = error
    message = FakeMessage()
    await svc.bot._document(photo_update(111, message), None)
    ack = message.replies[0]
    assert fragment in ack.text and ack.markup is None
    assert svc.db.execute("SELECT * FROM documents") == []
    assert svc.recent_activity(1)[0]["kind"] == "errore"


async def test_telegram_network_error_while_downloading(svc):
    message = FakeMessage(download_error=NetworkError("httpx.ConnectError"))
    await svc.bot._document(photo_update(111, message), None)
    assert "problema di connessione" in message.replies[0].text and svc.reader.calls == []


# --- Avvio e riavvio -----------------------------------------------------------


async def wait_for(condition, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def test_bot_waits_without_a_token_then_starts_when_it_is_saved(svc, monkeypatch):
    started = []

    async def fake_serve(token):
        started.append(token)
        svc.bot.status = "in ascolto come @prova"
        await svc.bot._changed.wait()

    monkeypatch.setattr(svc.bot, "_serve", fake_serve)
    task = asyncio.create_task(svc.bot.run())
    await wait_for(lambda: svc.bot.status == "token del bot mancante")
    assert started == []

    svc.settings.update({"bot_token": "1:aaa"})
    await wait_for(lambda: started == ["1:aaa"])

    svc.settings.update({"bot_token": "2:bbb"})  # cambio dal pannello: riavvio con il nuovo token
    await wait_for(lambda: started == ["1:aaa", "2:bbb"])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_invalid_token_is_reported_without_leaking_it_and_retried_after_change(svc, monkeypatch):
    attempts = []

    async def fake_serve(token):
        attempts.append(token)
        if token == "1:cattivo":
            raise InvalidToken("The token `1:cattivo` was rejected by the server.")
        await svc.bot._changed.wait()

    monkeypatch.setattr(svc.bot, "_serve", fake_serve)
    svc.settings.update({"bot_token": "1:cattivo"})
    task = asyncio.create_task(svc.bot.run())
    await wait_for(lambda: svc.bot.status == "errore: token del bot non valido")
    assert "cattivo" not in svc.bot.status and "cattivo" not in svc.recent_activity(1)[0]["detail"]

    svc.settings.update({"bot_token": "1:buono"})
    await wait_for(lambda: attempts == ["1:cattivo", "1:buono"])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_network_down_at_startup_is_retried_and_token_is_scrubbed(svc, monkeypatch):
    monkeypatch.setattr("famiglia.bot.RETRY_SECONDS", 0.05)
    attempts = []

    async def fake_serve(token):
        attempts.append(token)
        if len(attempts) < 3:
            raise NetworkError(f"non raggiungibile https://api.telegram.org/bot{token}/getMe")
        await svc.bot._changed.wait()

    monkeypatch.setattr(svc.bot, "_serve", fake_serve)
    svc.settings.update({"bot_token": "1:segreto"})
    task = asyncio.create_task(svc.bot.run())
    await wait_for(lambda: len(attempts) == 3)
    assert all("segreto" not in row["detail"] for row in svc.recent_activity(10))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
