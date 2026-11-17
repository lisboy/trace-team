"""模型列表拉取的响应判定：只把真正的人机验证页当 WAF，普通 HTML（控制台/登录页）要如实报出来。"""
from __future__ import annotations

from game_agent.api.routers.config import _looks_like_waf, _models_headers


def test_html_200_without_challenge_is_not_waf():
    # 地址指向控制台/登录页时也是 200 + HTML，不能当成 CF 挑战去反复重试
    assert not _looks_like_waf(200, "text/html; charset=utf-8", None,
                               "<!doctype html><html><body>管理控制台</body></html>")


def test_cloudflare_challenge_with_200_is_waf():
    assert _looks_like_waf(200, "text/html", None,
                           "<html><title>Just a moment...</title></html>")
    assert _looks_like_waf(200, "text/html", "challenge", "<html></html>")


def test_html_403_is_waf():
    assert _looks_like_waf(403, "text/html", None, "<html>blocked</html>")


def test_json_error_is_not_waf():
    assert not _looks_like_waf(503, "application/json", None, '{"error": "busy"}')


def test_bearer_and_anthropic_headers():
    h = _models_headers("bearer", "sk-abc", "UA/1")
    assert h["Authorization"] == "Bearer sk-abc"
    assert h["Accept"] == "application/json"
    assert h["User-Agent"] == "UA/1"
    h2 = _models_headers("anthropic", "k", "")
    assert h2["x-api-key"] == "k" and h2["anthropic-version"] == "2023-06-01"
    assert "User-Agent" not in h2
