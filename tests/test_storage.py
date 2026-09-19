import pytest

from famiglia.storage import Storage, StorageError


@pytest.fixture
def storage(root):
    return Storage(root)


def test_lists_only_visible_subfolders(storage, root):
    (root / ".nascosta").mkdir()
    (root / "file.txt").write_text("x")
    assert storage.subfolders(".") == ["Documenti", "Foto"]
    assert storage.subfolders("Documenti") == ["Referti"]


@pytest.mark.parametrize("path", ["..", "../altro", "Documenti/../..", "/etc", "/"])
def test_paths_outside_root_are_rejected(storage, path):
    with pytest.raises(StorageError):
        storage.subfolders(path)


def test_symlink_pointing_outside_root_is_rejected_and_hidden(storage, root, tmp_path):
    outside = tmp_path / "fuori"
    outside.mkdir()
    (root / "scorciatoia").symlink_to(outside)
    assert "scorciatoia" not in storage.subfolders(".")
    with pytest.raises(StorageError):
        storage.resolve("scorciatoia")


def test_make_folder(storage, root):
    assert storage.make_folder("Documenti", "Salute") == "Documenti/Salute"
    assert (root / "Documenti" / "Salute").is_dir()


@pytest.mark.parametrize("name", ["", "..", ".", "a/b", "..\\x", ".nascosta", "x" * 101])
def test_make_folder_rejects_bad_names(storage, name):
    with pytest.raises(StorageError):
        storage.make_folder(".", name)


def test_check_writable_leaves_no_probe_file(storage, root):
    storage.check_writable("Foto")
    assert list((root / "Foto").iterdir()) == []


def test_check_writable_fails_for_missing_folder(storage):
    with pytest.raises(StorageError):
        storage.check_writable("Non/esiste")


def test_save_creates_subfolders_and_never_overwrites(storage, root):
    first = storage.save("Documenti", ["Mario", "referti"], "2025-10-25_esami.jpg", b"uno")
    second = storage.save("Documenti", ["Mario", "referti"], "2025-10-25_esami.jpg", b"due")
    assert first == "Documenti/Mario/referti/2025-10-25_esami.jpg"
    assert second == "Documenti/Mario/referti/2025-10-25_esami-2.jpg"
    assert (root / first).read_bytes() == b"uno"


@pytest.mark.parametrize("parts,filename", [(["..", "x"], "a.jpg"), (["ok"], ".."), (["a/../../b"], "a.jpg")])
def test_save_cannot_escape_the_folder(storage, root, parts, filename):
    try:
        saved = storage.save("Documenti", parts, filename, b"x")
    except StorageError:
        return
    assert (root / saved).resolve().is_relative_to(root.resolve() / "Documenti")
