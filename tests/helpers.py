from famiglia.gemini import AppointmentInfo, Extraction, LabResult


def lab(name="Glicemia", value="95", unit="mg/dL", reference="70-100", flag=""):
    return LabResult(name=name, value=value, unit=unit, reference=reference, flag=flag)


def referto(patient="", date="2025-10-25", results=None, summary="Esami del sangue di routine.", details=""):
    results = results if results is not None else [lab(), lab("Colesterolo totale", "240", "mg/dL", "<200", "alto")]
    return Extraction(
        kind="referto", patient_name=patient, document_date=date, summary=summary, details=details, appointment=None,
        lab_results=results,
    )


def appuntamento(patient="", date="2026-11-03", time="09:30", title="Visita cardiologica", place="Ospedale Nord", notes=""):
    info = AppointmentInfo(title=title, date=date, time=time, place=place, notes=notes)
    return Extraction(
        kind="appuntamento", patient_name=patient, document_date="", summary="Prenotazione visita.", details="",
        appointment=info, lab_results=[],
    )


def altro(kind="altro", patient="", details="", date=""):
    return Extraction(
        kind=kind, patient_name=patient, document_date=date, summary="Un documento.", details=details, appointment=None,
        lab_results=[],
    )


class FakeReader:
    """Al posto di Gemini: restituisce quello che gli si è preparato e conta le chiamate."""

    def __init__(self, extraction: Extraction | None = None, error: Exception | None = None):
        self.extraction, self.error, self.calls = extraction, error, []

    async def analyze_document(self, data: bytes, mime: str) -> Extraction:
        self.calls.append((len(data), mime))
        if self.error:
            raise self.error
        return self.extraction


class FakeComposio:
    """Al posto di Composio: registra le chiamate e risponde come da documentazione."""

    def __init__(self, connected=(), calendars=None, execute_error=None, link="https://connect.composio.dev/link/ln_abc", expired=()):
        self.connected = set(connected)
        self.expired = set(expired)
        self.calendars = calendars if calendars is not None else [
            {"id": "mario@example.com", "summary": "Mario", "primary": True},
            {"id": "famiglia@group.calendar.google.com", "summary": "Famiglia", "primary": False},
        ]
        self.execute_error, self.link = execute_error, link
        self.executed: list[tuple[str, dict, str]] = []
        self.authorized: list[str] = []
        self.list_calls: list[dict] = []
        from types import SimpleNamespace

        self.tools = SimpleNamespace(execute=self._execute)
        self.toolkits = SimpleNamespace(authorize=self._authorize)
        self.connected_accounts = SimpleNamespace(list=self._list)

    def _execute(self, slug, arguments, *, user_id=None, dangerously_skip_version_check=None, **kwargs):
        self.executed.append((slug, arguments, user_id))
        assert dangerously_skip_version_check, "senza versione l'SDK vero solleva ToolVersionRequiredError"
        if self.execute_error:
            return {"successful": False, "data": {}, "error": self.execute_error}
        if slug == "GOOGLECALENDAR_LIST_CALENDARS":
            return {"successful": True, "data": {"items": self.calendars}, "error": None}
        if slug == "GOOGLECALENDAR_CREATE_EVENT":
            created = sum(1 for slug_, _, _ in self.executed if slug_ == "GOOGLECALENDAR_CREATE_EVENT")
            return {"successful": True, "data": {"response_data": {"id": f"evt{122 + created}", "summary": arguments["summary"]}}, "error": None}
        return {"successful": True, "data": {}, "error": None}

    def _authorize(self, *, user_id, toolkit):
        from types import SimpleNamespace

        self.authorized.append(f"{user_id}:{toolkit}")
        return SimpleNamespace(redirect_url=self.link, id="ca_1")

    def _list(self, **kwargs):
        from types import SimpleNamespace

        self.list_calls.append(kwargs)
        wanted = set(kwargs["user_ids"])
        items = [SimpleNamespace(user_id=u, status="ACTIVE") for u in self.connected if u in wanted]
        items += [SimpleNamespace(user_id=u, status="EXPIRED") for u in self.expired if u in wanted]
        return SimpleNamespace(items=items)


class FakeAnswerer:
    """Al posto di Gemini per le domande: registra cosa riceve e risponde con un testo fisso."""

    def __init__(self, reply="Risposta di prova.", error=None):
        self.reply, self.error, self.calls = reply, error, []

    async def answer(self, context, sender_name, question=None, audio=None, audio_mime=""):
        self.calls.append({"context": context, "sender": sender_name, "question": question, "audio": audio, "mime": audio_mime})
        if self.error:
            raise self.error
        return self.reply


def make_pdf(text: str) -> bytes:
    """Un PDF minimo ma valido, con del testo selezionabile (senza parentesi nel testo)."""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out
