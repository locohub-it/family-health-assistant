import pytest

from famiglia.ai import AiError, suggest_model, usable_models, vision_candidates, whisper_model

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


GROQ_TODAY_IDS = [
    "llama-3.1-8b-instant", "llama-3.3-70b-versatile", "openai/gpt-oss-120b", "groq/compound", "groq/compound-mini",
    "qwen/qwen3.6-27b", "qwen/qwen3.8-27b", "whisper-large-v3", "openai/gpt-oss-safeguard-20b",
]


def test_vision_candidates_put_known_vision_names_first_newest_first():
    ordered = vision_candidates(GROQ_TODAY_IDS)
    assert ordered[:2] == ["qwen/qwen3.8-27b", "qwen/qwen3.6-27b"]
    assert "groq/compound" not in ordered and "whisper-large-v3" not in ordered and "openai/gpt-oss-safeguard-20b" not in ordered


def test_vision_candidates_are_capped_and_stable_models_come_before_previews():
    ids = [f"modello-{n}" for n in range(20)] + ["qualcosa-vision-preview", "altro-vision-2"]
    ordered = vision_candidates(ids, limit=4)
    assert len(ordered) == 4 and ordered[:2] == ["altro-vision-2", "qualcosa-vision-preview"]
    assert vision_candidates([]) == []


def test_old_llama4_names_are_still_recognised_for_providers_that_keep_them():
    assert vision_candidates(["llama-3.3-70b-versatile", "meta-llama/llama-4-scout-17b-16e-instruct"])[0].endswith("scout-17b-16e-instruct")
