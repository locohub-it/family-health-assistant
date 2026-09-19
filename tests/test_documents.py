import shutil

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


async def test_gemini_is_not_called_for_unsupported_or_huge_files(make):
    svc = make(referto())
    assert "non lo so leggere" in (await svc.documents.process(svc.mario, b"x", "video/mp4")).text
    assert "troppo grande" in (await svc.documents.process(svc.mario, b"x" * (MAX_BYTES + 1), "image/jpeg")).text
    assert svc.reader.calls == []


# --- Cartella predefinita e riserva --------------------------------------------


async def test_without_a_chosen_folder_documents_go_to_the_default_one(make, root):
    svc = make(referto())
    svc.settings.update({"documents_dir": ""})
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    path = svc.records.get_document(outcome.document_id)["file_path"]
    assert path == "Documenti/Mario Rossi/Referti/2025-10-25_referto.jpg" and (root / path).exists()
    assert "cartella" not in outcome.text.lower()


async def test_a_wrong_path_never_reaches_the_chat_and_uses_the_default_folder(make, root):
    svc = make(referto())
    (root / "bloccata").write_text("sono un file, non una cartella")  # il percorso scelto non è una cartella
    svc.settings.update({"documents_dir": "bloccata"})
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert outcome.document_id and outcome.text.startswith("Ho salvato il referto")
    assert svc.records.get_document(outcome.document_id)["file_path"].startswith("Documenti/Mario Rossi/Referti/")
    errors = [r for r in svc.recent_activity(5) if r["kind"] == "errore"]
    assert len(errors) == 1 and "«bloccata»" in errors[0]["detail"] and "Salvato in Documenti/" in errors[0]["detail"]


async def test_a_deleted_chosen_folder_is_simply_recreated(make, root):
    svc = make(referto())
    svc.settings.update({"documents_dir": "Vecchia/Cartella"})
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert svc.records.get_document(outcome.document_id)["file_path"].startswith("Vecchia/Cartella/Mario Rossi/")
    assert svc.recent_activity(5) == [] or all(r["kind"] != "errore" for r in svc.recent_activity(5))


async def test_when_the_whole_root_is_unusable_the_data_volume_is_the_last_resort(make, root, tmp_path):
    svc = make(referto())
    shutil.rmtree(root / "Documenti")
    (root / "Documenti").write_text("un file al posto della cartella predefinita")
    (root / "Altra").write_text("e anche la scelta è un file")
    svc.settings.update({"documents_dir": "Altra"})
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    path = svc.records.get_document(outcome.document_id)["file_path"]
    assert path.startswith("@volume/Mario Rossi/Referti/") and outcome.text.startswith("Ho salvato il referto")
    assert (tmp_path / "data" / "documenti" / path.removeprefix("@volume/")).read_bytes() == JPEG
    # e «Annulla» toglie il file dal posto giusto
    assert "Annullato" in await svc.documents.undo(svc.mario, outcome.document_id)
    assert not (tmp_path / "data" / "documenti" / path.removeprefix("@volume/")).exists()


async def test_the_error_reaches_the_chat_only_if_nothing_at_all_can_be_written(make, root):
    from famiglia.storage import StorageError

    svc = make(referto())
    shutil.rmtree(root / "Documenti")
    (root / "Documenti").write_text("x")
    svc.documents._fallback = None  # nessuna riserva
    with pytest.raises(StorageError):
        await svc.documents.process(svc.mario, JPEG, "image/jpeg")


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
