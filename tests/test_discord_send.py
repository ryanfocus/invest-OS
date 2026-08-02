"""Discord 送出的失敗路徑。

Discord 掛掉不該讓策略本身失敗——訊號已經算出來了，通知只是傳遞。
但也不能靜默：每條失敗路徑都要回傳 False，讓呼叫端知道沒送成功。
"""

import requests

from notifiers.discord import send

PAYLOAD = {"content": "測試"}
URL = "https://discord.example/webhook"


class _Resp:
    def __init__(self, ok, status_code=200):
        self.ok = ok
        self.status_code = status_code


def test_send_returns_true_when_discord_accepts(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(ok=True))
    assert send(PAYLOAD, URL) is True


def test_send_returns_false_on_http_error(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(ok=False, status_code=500))
    assert send(PAYLOAD, URL) is False


def test_send_returns_false_when_network_raises(monkeypatch):
    """網路例外要被吞掉並回報失敗，不可往上炸掉整個進場流程。"""

    def boom(*a, **k):
        raise requests.RequestException("connection reset")

    monkeypatch.setattr(requests, "post", boom)
    assert send(PAYLOAD, URL) is False


def test_send_makes_no_request_when_webhook_url_is_empty(monkeypatch):
    """webhook 沒設定時必須完全不發出請求，而不是打到空網址。"""
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(a) or _Resp(ok=True))
    assert send(PAYLOAD, "") is False
    assert calls == []


def test_send_posts_the_payload_as_json(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Resp(ok=True)

    monkeypatch.setattr(requests, "post", fake_post)
    send(PAYLOAD, URL)
    assert captured["url"] == URL
    assert captured["json"] == PAYLOAD
    assert captured["timeout"] is not None, "必須有 timeout，否則掛住會拖垮整個排程"
