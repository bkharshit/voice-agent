import pytest
from pydantic import ValidationError
from voice_agent.settings import Settings
from voice_agent.speech import PhraseBuffer


def test_postgres_is_required_and_sqlite_is_never_implicit():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mode="normal", database_url=None)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mode="normal", database_url="sqlite+aiosqlite:///demo.db")
    assert Settings(_env_file=None, mode="demo").database_url.startswith("sqlite")


def test_remote_binding_requires_access_key():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mode="demo", host="0.0.0.0", api_key="")


def test_explicit_demo_flag_uses_offline_profile_even_with_normal_env(monkeypatch):
    from voice_agent.cli import resolve

    monkeypatch.setattr("dotenv.load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("VOICE_DEFAULT_PROFILE", "ollama-local")
    settings = resolve("demo", None)
    assert settings.default_profile == "demo"
    assert settings.database_url.startswith("sqlite")
    assert resolve("demo", None, "ollama-local").default_profile == "ollama-local"


def test_phrase_buffer_does_not_split_decimal_or_every_comma():
    buffer = PhraseBuffer()
    assert buffer.add("The result is 3.14, and ") == []
    assert buffer.add("that is the answer. ") == ["The result is 3.14, and that is the answer."]
    assert buffer.add("Next phrase", flush=True) == [" Next phrase"]
