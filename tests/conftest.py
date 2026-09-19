import pytest

from famiglia.service import Service


@pytest.fixture
def root(tmp_path):
    """Radice simulata del NAS/PC, con qualche cartella dentro."""
    root = tmp_path / "storage"
    (root / "Documenti" / "Referti").mkdir(parents=True)
    (root / "Foto").mkdir()
    return root


@pytest.fixture
def service(tmp_path, root):
    return Service(tmp_path / "data", "chiave-di-test", root)
