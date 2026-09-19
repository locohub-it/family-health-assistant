from famiglia.gemini import AppointmentInfo, Extraction, LabResult


def lab(name="Glicemia", value="95", unit="mg/dL", reference="70-100", flag=""):
    return LabResult(name=name, value=value, unit=unit, reference=reference, flag=flag)


def referto(patient="", date="2025-10-25", results=None, summary="Esami del sangue di routine."):
    results = results if results is not None else [lab(), lab("Colesterolo totale", "240", "mg/dL", "<200", "alto")]
    return Extraction(
        kind="referto", patient_name=patient, document_date=date, summary=summary, appointment=None, lab_results=results
    )


def appuntamento(patient="", date="2026-11-03", time="09:30", title="Visita cardiologica", place="Ospedale Nord", notes=""):
    info = AppointmentInfo(title=title, date=date, time=time, place=place, notes=notes)
    return Extraction(
        kind="appuntamento", patient_name=patient, document_date="", summary="Prenotazione visita.", appointment=info,
        lab_results=[],
    )


def altro(kind="altro", patient=""):
    return Extraction(
        kind=kind, patient_name=patient, document_date="", summary="Un documento.", appointment=None, lab_results=[]
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
