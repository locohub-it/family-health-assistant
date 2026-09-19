import pytest

from famiglia.ai import AiError, suggest_model, usable_models, whisper_model

GROQ = [
    "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "meta-llama/llama-4-scout-17b-16e-instruct",
    "whisper-large-v3", "whisper-large-v3-turbo", "openai/gpt-oss-120b", "meta-llama/llama-guard-4-12b", "playai-tts",
]
GEMINI = ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.1-pro-preview", "text-embedding-004", "imagen-4"]


@pytest.mark.parametrize(
    "role,ids,expected",
    [
        ("chat", GROQ, "llama-3.3-70b-versatile"),
        ("docs", GROQ, "meta-llama/llama-4-scout-17b-16e-instruct"),
        ("chat", ["deepseek-chat", "deepseek-reasoner"], "deepseek-chat"),
        ("chat", ["deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"], "deepseek-flash"),  # non lo sperimentale
        ("chat", GEMINI, "gemini-3.8-flash"),  # la versione più alta, non la lite
        ("docs", GEMINI, "gemini-3.8-flash"),
        ("chat", ["qualcosa-di-nuovo", "altro-modello"], "qualcosa-di-nuovo"),  # nessun criterio: il primo utilizzabile
        ("chat", ["whisper-large-v3", "playai-tts", "text-embedding-3"], ""),  # solo modelli che non chattano
        ("chat", [], ""),
        ("chat", ["gemini-3.5-flash-lite"], "gemini-3.5-flash-lite"),  # se c'è solo uno sperimentale si usa
    ],
)
def test_suggest_model(role, ids, expected):
    assert suggest_model(role, ids) == expected


def test_models_that_cannot_chat_are_filtered_out():
    assert usable_models(GROQ) == [
        "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "meta-llama/llama-4-scout-17b-16e-instruct", "openai/gpt-oss-120b",
    ]


def test_whisper_model_prefers_turbo_and_may_not_exist():
    assert whisper_model(GROQ) == "whisper-large-v3-turbo"
    assert whisper_model(["deepseek-chat"]) == ""


def test_ai_error_keeps_the_user_message_and_the_technical_detail():
    error = AiError("Messaggio gentile", "429 dettaglio tecnico", quota=True)
    assert error.user_message == "Messaggio gentile" and str(error) == "429 dettaglio tecnico" and error.quota
    assert str(AiError("solo messaggio")) == "solo messaggio"
