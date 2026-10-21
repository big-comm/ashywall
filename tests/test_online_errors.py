"""Distinguish provider outages from local connectivity and request errors."""

import gettext
from pathlib import Path

import pytest

from ashywall.services.http import HttpError, NetworkUnavailable, RateLimited
from ashywall.ui import errors, window_states


@pytest.fixture(autouse=True)
def english_messages(monkeypatch):
    monkeypatch.setattr(errors, "_", lambda message: message)
    monkeypatch.setattr(window_states, "_", lambda message: message)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 521, 522, 524, 599])
def test_provider_outage_retains_status_and_manual_retry(status):
    state = window_states.remote_error_state("Wallhaven", HttpError("upstream", status))
    assert state.title == "Wallhaven Is Currently Unavailable"
    assert state.description == (
        f"The service is currently unavailable (HTTP {status}). Try again later."
    )
    assert state.action_name == "win.retry-explore"
    assert state.action_label == "Try Again"


@pytest.mark.parametrize("status", [None, 403, 404])
def test_other_http_errors_keep_their_original_description(status):
    state = window_states.remote_error_state("Wallhaven", HttpError("request", status))
    assert state.title == "Wallhaven Could Not Be Loaded"
    assert state.description == f"The server answered with an error ({status or '?'})."


def test_connectivity_and_throttling_keep_their_specific_advice():
    unreachable = window_states.remote_error_state("Wallhaven", NetworkUnavailable("DNS"))
    assert unreachable.title == "Wallhaven Is Unreachable"
    assert "Check your connection" in unreachable.description
    limited = window_states.remote_error_state("Wallhaven", RateLimited(60))
    assert limited.title == "Too Many Requests"
    assert "Wait a minute" in limited.description


def test_outage_is_translated_in_the_packaged_portuguese_catalog(monkeypatch):
    locale_dir = Path(__file__).resolve().parents[1] / "usr/share/locale"
    translate = gettext.translation("ashywall", locale_dir, languages=["pt_BR"]).gettext
    monkeypatch.setattr(errors, "_", translate)
    monkeypatch.setattr(window_states, "_", translate)
    state = window_states.remote_error_state("Wallhaven", HttpError("upstream", 521))
    assert state.title == "Wallhaven está indisponível no momento"
    assert state.description == (
        "O serviço está indisponível no momento (HTTP 521). Tente novamente mais tarde."
    )
