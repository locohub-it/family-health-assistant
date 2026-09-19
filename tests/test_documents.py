import pytest

from famiglia.documents import MAX_BYTES, match_patient
from famiglia.service import Service
from famiglia.users import User

from helpers import FakeReader, altro, appuntamento, referto

JPEG = b"\xff\xd8\xff fake jpeg"


@pytest.fixture
def make(tmp_path, root):
    """Crea un servizio con Gemini finto, due utenti e la cartella dei documenti già scelta."""

    def _make(extraction=None, error=None):
        reader = FakeReader(extraction, error)
        svc = Service(tmp_path / "data", "chiave-di-test", root, reader=reader)
        svc.settings.update({"documents_dir": "Documenti"})
        svc.mario = svc.users.add(111, "Mario Rossi", "papà")
        svc.anna = svc.users.add(222, "Anna Bianchi", "mamma")
        svc.reader = reader
        return svc

    return _make


async def test_lab_report_is_saved_in_db_and_folder(make, root):
    svc = make(referto())
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")

    assert outcome.document_id
    assert "referto di Mario Rossi del 25/10/2025" in outcome.text and "2 valori" in outcome.text
    assert "Colesterolo totale: 240 mg/dL (alto)" in outcome.text
    assert "Glicemia" not in outcome.text.split("Fuori dai valori")[1]  # i valori normali non allarmano
    rows = svc.db.execute("SELECT name, value, flag FROM lab_results ORDER BY id")
    assert [(r["name"], r["value"], r["flag"]) for r in rows] == [("Glicemia", "95", ""), ("Colesterolo totale", "240", "alto")]
    doc = svc.records.get_document(outcome.document_id)
    assert doc["file_path"] == "Documenti/Mario Rossi/Referti/2025-10-25_referto.jpg"
    assert (root / doc["file_path"]).read_bytes() == JPEG


async def test_appointment_is_saved(make):
    svc = make(appuntamento(notes="Portare la tessera sanitaria"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")

    assert "«Visita cardiologica»" in outcome.text and "03/11/2026 alle 09:30" in outcome.text
    assert "Ospedale Nord" in outcome.text and "tessera sanitaria" in outcome.text
    row = svc.db.execute("SELECT * FROM appointments")[0]
    assert (row["user_id"], row["starts_at"], row["place"]) == (svc.mario.id, "2026-11-03T09:30", "Ospedale Nord")


async def test_appointment_without_time_keeps_the_date_only(make):
    svc = make(appuntamento(time=""))
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert svc.db.execute("SELECT starts_at FROM appointments")[0]["starts_at"] == "2026-11-03"


async def test_appointment_without_a_valid_date_is_not_scheduled(make):
    svc = make(appuntamento(date="prossimo martedì"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "non trovo la data" in outcome.text
    assert svc.db.execute("SELECT * FROM appointments") == []
    assert svc.db.execute("SELECT * FROM documents")  # il documento resta salvato


async def test_unreadable_document_is_not_saved(make, root):
    svc = make(altro("illeggibile"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert outcome.document_id is None and "rifare la foto" in outcome.text
    assert svc.db.execute("SELECT * FROM documents") == []
    assert list((root / "Documenti").rglob("*.jpg")) == []


async def test_prescription_and_other_documents(make):
    svc = make(altro("ricetta"))
    outcome = await svc.documents.process(svc.mario, JPEG, "application/pdf")
    assert "la ricetta di Mario Rossi" in outcome.text
    path = svc.records.get_document(outcome.document_id)["file_path"]
    assert path.startswith("Documenti/Mario Rossi/Ricette/") and path.endswith("_ricetta.pdf")


async def test_document_of_another_family_member_goes_to_them(make):
    svc = make(referto(patient="BIANCHI ANNA"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    doc = svc.records.get_document(outcome.document_id)
    assert doc["user_id"] == svc.anna.id and doc["sender_telegram_id"] == svc.mario.telegram_id
    assert "Anna Bianchi" in outcome.text and "Anna Bianchi/Referti" in doc["file_path"]


async def test_unknown_patient_falls_back_to_sender_and_says_so(make):
    svc = make(referto(patient="Giuseppe Verdi"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert svc.records.get_document(outcome.document_id)["user_id"] == svc.mario.id
    assert "Giuseppe Verdi" in outcome.text and "non è tra i familiari" in outcome.text


async def test_gemini_is_not_called_for_unsupported_or_huge_files_or_without_folder(make):
    svc = make(referto())
    assert "non lo so leggere" in (await svc.documents.process(svc.mario, b"x", "video/mp4")).text
    assert "troppo grande" in (await svc.documents.process(svc.mario, b"x" * (MAX_BYTES + 1), "image/jpeg")).text
    svc.settings.update({"documents_dir": ""})
    assert "scegliere la cartella" in (await svc.documents.process(svc.mario, JPEG, "image/jpeg")).text
    assert svc.reader.calls == []


async def test_gemini_error_is_propagated_and_nothing_is_saved(make):
    from famiglia.gemini import GeminiError

    svc = make(error=GeminiError("Gemini non risponde"))
    with pytest.raises(GeminiError):
        await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert svc.db.execute("SELECT * FROM documents") == []


async def test_undo_removes_rows_and_file(make, root):
    svc = make(referto())
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    path = root / svc.records.get_document(outcome.document_id)["file_path"]
    assert path.exists()

    assert "Annullato" in await svc.documents.undo(svc.mario, outcome.document_id)
    assert not path.exists()
    assert svc.db.execute("SELECT * FROM documents") == [] and svc.db.execute("SELECT * FROM lab_results") == []
    assert "già stato annullato" in await svc.documents.undo(svc.mario, outcome.document_id)


async def test_only_the_sender_can_undo(make):
    svc = make(referto())
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "solo quello che hai inviato tu" in await svc.documents.undo(svc.anna, outcome.document_id)
    assert svc.records.get_document(outcome.document_id) is not None


def _user(uid, name):
    return User(uid, uid, name, "", "primary")


@pytest.mark.parametrize(
    "written,expected",
    [
        ("ROSSI MARIO", ["Mario Rossi"]),
        ("Sig. Mario Rossi", ["Mario Rossi"]),
        ("Rossi M.", ["Mario Rossi"]),
        ("Bianchi Anna Maria", ["Anna Bianchi"]),
        ("Verdi Luigi", []),
        ("", []),
    ],
)
def test_match_patient(written, expected):
    users = [_user(1, "Mario Rossi"), _user(2, "Anna Bianchi")]
    assert [u.name for u in match_patient(written, users)] == expected


def test_match_patient_reports_ambiguity():
    users = [_user(1, "Mario"), _user(2, "Mario Rossi")]
    assert sorted(u.name for u in match_patient("Mario", users)) == ["Mario", "Mario Rossi"]
