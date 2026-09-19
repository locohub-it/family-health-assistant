import pytest

from famiglia.main import run


@pytest.mark.parametrize(
    "env,fragment",
    [
        ({"SECRET_KEY": "chiave", "ADMIN_PASSWORD": "corta"}, "almeno 10 caratteri"),
        ({"SECRET_KEY": "chiave", "ADMIN_PASSWORD": ""}, "Imposta ADMIN_PASSWORD"),
        ({"SECRET_KEY": "", "ADMIN_PASSWORD": "una-password-lunga"}, "SECRET_KEY mancante"),
    ],
)
async def test_bad_configuration_exits_with_one_clear_line(tmp_path, monkeypatch, env, fragment):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit) as exc:
        await run()
    message = str(exc.value)
    assert message.startswith("ERRORE DI CONFIGURAZIONE:") and fragment in message and "\n" not in message
