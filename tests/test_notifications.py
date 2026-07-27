"""Tests for reasoning/notifications.py (Telegram alerts, module 11).

Runs the real selection/formatting logic against the REAL production
database (same convention as tests/test_dashboard_pages.py -- this project
tests against real data, not synthetic fixtures), but NEVER performs a real
network call: every test here mocks requests.post, so no test run can ever
send a real Telegram message or depend on network/credentials being present.

Run:
    pytest tests/test_notifications.py -v
"""

import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

from reasoning import notifications  # noqa: E402


def _real_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# --- send_telegram_message -----------------------------------------------------

def test_send_telegram_message_success_is_mocked_never_real():
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, content=b'{"ok": true}')
        mock_post.return_value.json.return_value = {"ok": True}
        ok = notifications.send_telegram_message(
            "hello", token="fake-token", chat_id="123456")
    assert ok is True
    mock_post.assert_called_once()
    # The real endpoint is used (so the call shape is verified), but requests
    # itself is mocked -- no packet ever leaves the machine.
    called_url = mock_post.call_args.args[0]
    assert called_url == "https://api.telegram.org/botfake-token/sendMessage"


def test_send_telegram_message_missing_credentials_returns_false():
    with patch("requests.post") as mock_post:
        ok = notifications.send_telegram_message("hello", token=None, chat_id=None)
    assert ok is False
    mock_post.assert_not_called()


def test_send_telegram_message_network_error_returns_false_never_raises():
    import requests
    with patch("requests.post", side_effect=requests.RequestException("boom")):
        ok = notifications.send_telegram_message(
            "hello", token="fake-token", chat_id="123456")
    assert ok is False


def test_send_telegram_message_rejected_by_telegram_returns_false():
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=400, content=b'{"ok": false, "description": "chat not found"}')
        mock_post.return_value.json.return_value = {
            "ok": False, "description": "chat not found"}
        ok = notifications.send_telegram_message(
            "hello", token="fake-token", chat_id="wrong-id")
    assert ok is False


def test_send_telegram_message_markdown_parse_error_retries_plain_text():
    responses = [
        MagicMock(status_code=400,
                  content=b'{"ok": false, "description": "Bad Request: can\'t parse entities"}'),
        MagicMock(status_code=200, content=b'{"ok": true}'),
    ]
    responses[0].json.return_value = {
        "ok": False, "description": "Bad Request: can't parse entities"}
    responses[1].json.return_value = {"ok": True}
    with patch("requests.post", side_effect=responses) as mock_post:
        ok = notifications.send_telegram_message(
            "hello *unbalanced", token="fake-token", chat_id="123456")
    assert ok is True
    assert mock_post.call_count == 2
    # Second attempt must drop parse_mode (plain-text retry).
    second_call_kwargs = mock_post.call_args_list[1].kwargs
    assert "parse_mode" not in second_call_kwargs.get("data", {})


# --- find_notable_opportunities / format_message (real DB, no network) --------

def test_find_notable_opportunities_returns_list_never_raises():
    conn = _real_conn()
    try:
        row = conn.execute("SELECT MAX(date_calcul) FROM opportunites").fetchone()
        data_date = row[0]
        rows = notifications.find_notable_opportunities(conn, data_date, min_score_ajuste=0.0)
        assert isinstance(rows, list)
        # threshold 0 must include every scored row for that date
        all_rows = notifications.load_opportunites_for_date(conn, data_date) if data_date else []
        assert len(rows) == len([r for r in all_rows if r["confiance"] is not None])
    finally:
        conn.close()


def test_find_notable_opportunities_impossible_threshold_returns_empty():
    conn = _real_conn()
    try:
        row = conn.execute("SELECT MAX(date_calcul) FROM opportunites").fetchone()
        data_date = row[0]
        rows = notifications.find_notable_opportunities(conn, data_date, min_score_ajuste=1000.0)
        assert rows == []
    finally:
        conn.close()


def test_find_notable_opportunities_no_data_date_returns_empty():
    conn = _real_conn()
    try:
        rows = notifications.find_notable_opportunities(conn, None)
        assert rows == []
    finally:
        conn.close()


# --- run_notifications end-to-end (real DB, mocked Telegram) ------------------

def test_run_notifications_no_send_when_nothing_qualifies():
    conn = _real_conn()
    try:
        with patch("requests.post") as mock_post:
            sent, message, n = notifications.run_notifications(
                conn, min_score_ajuste=1000.0)
        assert sent is False
        assert message is None
        assert n == 0
        mock_post.assert_not_called()
    finally:
        conn.close()


def test_run_notifications_sends_via_mocked_telegram_when_threshold_is_low(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    conn = _real_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM opportunites WHERE score_global IS NOT NULL"
        ).fetchone()
        if row[0] == 0:
            return  # nothing scored yet in this environment -- nothing to assert
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, content=b'{"ok": true}')
            mock_post.return_value.json.return_value = {"ok": True}
            sent, message, n = notifications.run_notifications(
                conn, min_score_ajuste=0.0)
        assert sent is True
        assert n > 0
        assert message is not None
        mock_post.assert_called_once()
    finally:
        conn.close()


def test_run_notifications_dry_run_never_calls_telegram():
    conn = _real_conn()
    try:
        with patch("requests.post") as mock_post:
            sent, message, n = notifications.run_notifications(
                conn, min_score_ajuste=0.0, dry_run=True)
        assert sent is False
        mock_post.assert_not_called()
    finally:
        conn.close()
