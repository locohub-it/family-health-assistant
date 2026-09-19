"""Bot Telegram in long polling: nessuna porta aperta, risponde subito e solo agli utenti approvati."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import InvalidToken, NetworkError, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .calendar import Calendar, CalendarError
from .consult import Consultant, split_message
from .documents import MAX_BYTES, SUPPORTED_MIME, DocumentService
from .ai import AiError
from .settings import Settings
from .storage import StorageError
from .users import User, UserStore
from .visits import Reply, Visits, offer_new_visit, wants_list, wants_new_visit

log = logging.getLogger(__name__)

RETRY_SECONDS = 30
ACK_TEXT = "📄 Sto leggendo il documento, un attimo…"
ASK_ACK_TEXT = "🤔 Ci penso un attimo…"
MAX_QUESTION_CHARS = 2000
UNDO_PREFIX = "annulla:"
MENU = [
    ("visita", "Segna una nuova visita"),
    ("appuntamenti", "Vedi, cambia o togli le tue visite"),
    ("calendario", "Collega il tuo Google Calendar"),
]


def _markup(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=code) for label, code in row] for row in reply.buttons])


def _scrub(text: str, token: str) -> str:
    """Il token del bot non deve finire in pagina né nei log."""
    return text.replace(token, "***") if token else text


class BotRunner:
    """Tiene acceso il bot e lo riavvia quando dal pannello cambia il token."""

    def __init__(
        self,
        settings: Settings,
        users: UserStore,
        documents: DocumentService,
        consultant: Consultant,
        record: Callable[[str, str], None],
        calendar: Calendar | None = None,
        visits: Visits | None = None,
    ) -> None:
        self._settings = settings
        self._users = users
        self._documents = documents
        self._consultant = consultant
        self._record = record
        self._calendar = calendar
        self._visits = visits
        self._changed = asyncio.Event()
        self.status = "non avviato"

    def _note(self, kind: str, text: str) -> None:
        self._record(kind, _scrub(text, self._settings.get("bot_token")))

    def restart(self) -> None:
        self._changed.set()

    async def run(self) -> None:
        while True:
            self._changed.clear()
            token = self._settings.get("bot_token")
            if not token:
                self.status = "token del bot mancante"
                await self._changed.wait()
                continue
            try:
                await self._serve(token)
            except InvalidToken:
                self.status = "errore: token del bot non valido"
                self._record("errore", "Token del bot non valido")
                await self._changed.wait()
            except Exception as exc:  # rete assente all'avvio, Telegram irraggiungibile...
                reason = _scrub(str(exc), token)
                self.status = f"errore: {reason}"
                self._record("errore", f"Bot Telegram: {reason}")
                log.warning("Bot Telegram fermo, riprovo tra %s s: %s", RETRY_SECONDS, reason)
                try:
                    await asyncio.wait_for(self._changed.wait(), RETRY_SECONDS)
                except asyncio.TimeoutError:
                    pass

    async def _serve(self, token: str) -> None:
        app = Application.builder().token(token).concurrent_updates(True).build()
        self._register(app)
        await app.initialize()  # verifica il token con Telegram
        try:
            await app.start()
            await self._publish_menu(app)
            await app.updater.start_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
            self.status = f"in ascolto come @{app.bot.username}"
            log.info("Bot Telegram %s", self.status)
            await self._changed.wait()
        finally:
            for stop in (app.updater.stop, app.stop, app.shutdown):
                try:
                    await stop()
                except Exception:  # noqa: BLE001 - in chiusura conta solo arrivare in fondo
                    log.debug("Errore in chiusura del bot", exc_info=True)

    async def _publish_menu(self, app: Application) -> None:
        """L'elenco dei comandi sotto al pulsante «Menu» di Telegram: si toccano invece di scriverli."""
        try:
            await app.bot.set_my_commands(MENU)
        except TelegramError as exc:  # il menu è un comodo in più: il bot funziona anche senza
            self._note("errore", f"Menu dei comandi non impostato: {_scrub(str(exc), self._settings.get('bot_token'))}")

    def _register(self, app: Application) -> None:
        # Il cancello sta nel gruppo -1: se non passa, nessun altro handler vede l'aggiornamento.
        app.add_handler(TypeHandler(Update, self._gate), group=-1)
        app.add_handler(CommandHandler("start", self._start))
        app.add_handler(CommandHandler(["calendario", "calendar"], self._connect_calendar))
        app.add_handler(CommandHandler("visita", self._new_visit))
        app.add_handler(CommandHandler("appuntamenti", self._list_visits))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self._document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._text))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self._voice))
        app.add_handler(CallbackQueryHandler(self._undo, pattern=f"^{UNDO_PREFIX}"))
        app.add_handler(CallbackQueryHandler(self._visit_button, pattern=r"^[va]:"))
        # Per ultimo: un comando sconosciuto non deve cadere nel vuoto senza risposta.
        app.add_handler(MessageHandler(filters.COMMAND, self._unknown_command))
        app.add_error_handler(self._error)

    # --- Sicurezza ---------------------------------------------------------------

    async def _gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Solo chat private con utenti approvati; tutto il resto viene ignorato in silenzio."""
        user, chat = update.effective_user, update.effective_chat
        if user is None or chat is None or chat.type != ChatType.PRIVATE or self._users.by_telegram_id(user.id) is None:
            raise ApplicationHandlerStop

    def _approved(self, update: Update) -> User | None:
        return self._users.by_telegram_id(update.effective_user.id) if update.effective_user else None

    # --- Handler -----------------------------------------------------------------

    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._approved(update)
        if user:
            await update.effective_message.reply_text(
                f"Ciao {user.name}! Mandami la foto di un referto, di una ricetta o di una prenotazione "
                "e ci penso io: non devi scrivere niente.\n\n"
                "Puoi anche farmi una domanda sui tuoi referti, scritta o a voce.\n\n"
                "Per le visite:\n"
                "/visita – segna una nuova visita\n"
                "/appuntamenti – vedi, cambia o togli le tue visite"
            )

    async def _connect_calendar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, message = self._approved(update), update.effective_message
        if user is None or message is None:
            return
        if self._calendar is None or not self._calendar.enabled:
            await message.reply_text("Il calendario non è ancora configurato: chiedi a chi gestisce il bot.")
            return
        try:
            link = await self._calendar.connect_link(user)
        except CalendarError as exc:
            self._note("errore", f"Collegamento calendario di {user.name}: {exc}")
            await message.reply_text(exc.user_message)
            return
        await message.reply_text(
            "Per collegare il tuo Google Calendar apri questo link e accedi con il tuo account Google:\n" + link
        )

    async def _new_visit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, message = self._approved(update), update.effective_message
        if user is None or message is None or self._visits is None:
            return
        await self._send_replies(message, [self._visits.start_new(user, context.user_data)])

    async def _list_visits(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, message = self._approved(update), update.effective_message
        if user is None or message is None or self._visits is None:
            return
        context.user_data.clear()
        await self._send_replies(message, self._visits.list_cards(user))

    async def _visit_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query, user = update.callback_query, self._approved(update)
        if query is None or user is None or self._visits is None:
            return
        await query.answer()
        try:
            reply = await self._visits.on_button(user, context.user_data, query.data)
        except CalendarError as exc:
            self._note("errore", f"Calendario: {exc}")
            reply = Reply(exc.user_message)
        except Exception as exc:  # noqa: BLE001 - chi tocca un pulsante deve sempre ricevere una risposta
            log.exception("Errore in un pulsante")
            self._note("errore", f"Imprevisto: {exc!r}")
            reply = Reply("Qualcosa è andato storto. Riprova tra poco.")
        first, *rest = split_message(reply.text) or [reply.text]
        await query.edit_message_text(first, reply_markup=_markup(reply) if not rest else None)
        for part in rest:
            await query.message.reply_text(part, reply_markup=_markup(reply) if part is rest[-1] else None)

    async def _send_replies(self, message, replies: list[Reply]) -> None:
        for reply in replies:
            await message.reply_text(reply.text, reply_markup=_markup(reply))

    async def _unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._approved(update) and update.effective_message:
            await update.effective_message.reply_text(
                "Questo comando non lo conosco. Puoi usare /visita, /appuntamenti e /calendario; "
                "altrimenti mandami la foto di un documento o scrivimi una domanda."
            )

    async def _text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Un messaggio scritto è una domanda, tranne durante un passaggio guidato e per «modifica/elimina appuntamento»."""
        user, message = self._approved(update), update.effective_message
        if user is None or message is None:
            return
        question = (message.text or "").strip()
        if self._visits is not None:
            action = wants_list(question)
            if action:
                context.user_data.clear()
                await self._send_replies(message, self._visits.list_cards(user, action))
                return
            reply = await self._visits.on_text(user, context.user_data, question)
            if reply is not None:
                await self._send_replies(message, [reply])
                return
            if wants_new_visit(question):  # non si segna niente da una frase: si propone il passaggio guidato
                await self._send_replies(message, [offer_new_visit()])
                return
        if len(question) > MAX_QUESTION_CHARS:
            await message.reply_text("Il messaggio è troppo lungo. Puoi farmi una domanda più breve?")
            return

        async def job():
            return await self._consultant.ask(user, question=question), None

        await self._reply_with_ack(message, ASK_ACK_TEXT, job)

    async def _voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Un vocale è una domanda a voce: Gemini lo ascolta direttamente."""
        user, message = self._approved(update), update.effective_message
        if user is None or message is None:
            return
        attachment = message.voice or message.audio
        mime = attachment.mime_type or "audio/ogg"
        if not mime.startswith("audio/"):
            await message.reply_text("Questo audio non lo so ascoltare. Prova con un messaggio vocale.")
            return
        if attachment.file_size and attachment.file_size > MAX_BYTES:
            await message.reply_text("L'audio è troppo lungo. Prova con un messaggio più breve.")
            return

        async def job():
            file = await attachment.get_file()
            data = bytes(await file.download_as_bytearray())
            return await self._consultant.ask(user, audio=data, audio_mime=mime), None

        await self._reply_with_ack(message, ASK_ACK_TEXT, job)

    async def _document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, message = self._approved(update), update.effective_message
        if user is None or message is None:
            return
        if message.photo:
            attachment, mime = message.photo[-1], "image/jpeg"  # l'ultima è la versione più grande
        else:
            attachment, mime = message.document, (message.document.mime_type or "")
        if mime not in SUPPORTED_MIME:
            await message.reply_text("Questo tipo di file non lo so leggere. Mandami una foto o un PDF del documento.")
            return
        if attachment.file_size and attachment.file_size > MAX_BYTES:
            await message.reply_text("Il file è troppo grande. Prova con una foto più leggera.")
            return

        async def job():
            file = await attachment.get_file()
            data = bytes(await file.download_as_bytearray())
            outcome = await self._documents.process(user, data, mime)
            markup = None
            if outcome.document_id:
                markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ Annulla", callback_data=f"{UNDO_PREFIX}{outcome.document_id}")]]
                )
            return outcome.text, markup

        await self._reply_with_ack(message, ACK_TEXT, job)

    async def _reply_with_ack(self, message, ack_text: str, job) -> None:
        """Risposta immediata, poi la si sostituisce col risultato. L'utente riceve sempre qualcosa."""
        await message.chat.send_action(ChatAction.TYPING)
        ack = await message.reply_text(ack_text)
        markup = None
        try:
            text, markup = await job()
        except AiError as exc:
            text = exc.user_message
            self._note("errore", f"IA: {exc}")
        except StorageError as exc:
            text = "Non riesco a salvare il documento nella cartella scelta. Avvisa chi gestisce il bot."
            self._note("errore", f"Salvataggio: {exc}")
        except NetworkError as exc:
            text = "Ho un problema di connessione con Telegram. Riprova tra poco."
            self._note("errore", f"Rete: {exc}")
        except Exception as exc:  # noqa: BLE001 - l'utente deve sempre ricevere una risposta
            log.exception("Errore imprevisto")
            text = "Qualcosa è andato storto. Riprova tra poco."
            self._note("errore", f"Imprevisto: {exc!r}")
        first, *rest = split_message(text) or [text]
        await ack.edit_text(first, reply_markup=markup)
        for part in rest:  # una risposta lunga può superare il limite di Telegram
            await message.reply_text(part)

    async def _undo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query, user = update.callback_query, self._approved(update)
        if query is None or user is None:
            return
        await query.answer()
        try:
            document_id = int(query.data.removeprefix(UNDO_PREFIX))
        except ValueError:
            return
        await query.edit_message_text(await self._documents.undo(user, document_id))

    async def _error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if isinstance(context.error, TelegramError):
            log.warning("Errore Telegram: %s", context.error)
        else:
            log.error("Errore nel bot", exc_info=context.error)
