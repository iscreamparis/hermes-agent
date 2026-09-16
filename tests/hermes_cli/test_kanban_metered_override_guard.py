"""A model_override must never route a subscription-covered model through a reseller.

Real incident: tracer card t_70c49156 was pinned to
``anthropic/claude-opus-4.8`` with ``provider=openrouter``. The override beats the
profile, so the worker ran Opus through OpenRouter's metered billing — 17 calls,
1.68M tokens, real cash — while claude-opus-5 sat unused on the flat-rate Anthropic
subscription. Nothing in the pipeline objected.

``_validate_model_override`` now refuses that combination. Escape hatch:
``kanban.allow_metered_override: true``.
"""
from __future__ import annotations

import pytest

from hermes_cli.kanban_db import _validate_model_override


@pytest.fixture(autouse=True)
def _no_escape_hatch(monkeypatch):
    """Default config: the guard is armed."""
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {}, raising=False)


@pytest.mark.parametrize("model", [
    "anthropic/claude-opus-4.8",
    "anthropic/claude-opus-5",
    "anthropic/claude-fable-5.1",
    "anthropic/claude-sonnet-5",
    "anthropic/CLAUDE-Opus-4.8",
])
def test_claude_via_openrouter_is_refused(model):
    with pytest.raises(ValueError, match="covered by your anthropic subscription"):
        _validate_model_override(model, "openrouter")


@pytest.mark.parametrize("provider", ["openrouter", "nous", "OpenRouter", "deepinfra"])
def test_every_metered_reseller_is_covered(provider):
    with pytest.raises(ValueError, match="bills per token"):
        _validate_model_override("anthropic/claude-opus-4.8", provider)


def test_the_error_names_the_fix():
    with pytest.raises(ValueError) as exc:
        _validate_model_override("anthropic/claude-opus-4.8", "openrouter")
    msg = str(exc.value)
    assert "provider='anthropic'" in msg
    assert "allow_metered_override" in msg


@pytest.mark.parametrize("model,provider", [
    # The subscription routes themselves: always fine.
    ("claude-opus-5", "anthropic"),
    ("claude-fable-5.1", "anthropic"),
    ("gpt-5.6-luna", "openai-codex"),
    ("gpt-6-astra", "openai-codex"),
    # Models no subscription covers: OpenRouter is the correct route.
    ("google/gemini-3.8-flash", "openrouter"),
    ("z-ai/glm-5.3", "openrouter"),
    ("deepseek/deepseek-v4-flash-0731", "openrouter"),
    ("moonshotai/kimi-k3", "openrouter"),
    # OpenAI models on OpenRouter stay allowed: the Codex plan is ChatGPT-scoped,
    # and openai/* on OpenRouter is the documented overflow twin (o-luna-*).
    ("openai/gpt-5.6-luna", "openrouter"),
])
def test_legitimate_overrides_still_pass(model, provider):
    assert _validate_model_override(model, provider) == (model, provider)


def test_model_without_provider_is_untouched():
    """No provider pin means the profile decides the route; nothing to check."""
    assert _validate_model_override("anthropic/claude-opus-4.8", None) == (
        "anthropic/claude-opus-4.8", None)


def test_clearing_the_override_still_works():
    assert _validate_model_override("", "") == (None, None)
    assert _validate_model_override(None, None) == (None, None)


def test_provider_without_model_still_rejected():
    with pytest.raises(ValueError, match="requires a model_override"):
        _validate_model_override(None, "openrouter")


def test_escape_hatch_allows_it(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"kanban": {"allow_metered_override": True}}, raising=False)
    assert _validate_model_override("anthropic/claude-opus-4.8", "openrouter") == (
        "anthropic/claude-opus-4.8", "openrouter")


def test_unreadable_config_keeps_the_guard_armed(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("config is gone")
    monkeypatch.setattr("hermes_cli.config.load_config", _boom, raising=False)
    with pytest.raises(ValueError, match="covered by your anthropic subscription"):
        _validate_model_override("anthropic/claude-opus-4.8", "openrouter")
