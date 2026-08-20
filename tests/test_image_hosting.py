"""image_hosting 模块测试：R2 配置检测 + 上传 fail-soft。"""

import json
import urllib.error

import pytest

from skill_engine.execution import image_hosting


def _clear_env(monkeypatch):
    for k in image_hosting._ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _set_env(monkeypatch):
    vals = {k: f"v-{k}" for k in image_hosting._ENV_KEYS}
    for k, v in vals.items():
        monkeypatch.setenv(k, v)
    return vals


def test_r2_config_none_when_missing(monkeypatch):
    _clear_env(monkeypatch)
    assert image_hosting.r2_config() is None


def test_r2_config_partial_none(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CF_R2_TOKEN", "t")
    assert image_hosting.r2_config() is None


def test_r2_config_ok(monkeypatch):
    vals = _set_env(monkeypatch)
    assert image_hosting.r2_config() == vals


def test_upload_returns_none_when_unconfigured(monkeypatch):
    _clear_env(monkeypatch)
    assert image_hosting.upload_image_to_r2(b"x", "image/png") is None


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_upload_success(monkeypatch):
    _set_env(monkeypatch)
    captured = {}

    def fake_urlopen(req, timeout=60):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["auth"] = req.headers.get("Authorization")
        captured["ctype"] = req.headers.get("Content-type", req.headers.get("Content-Type"))
        return _FakeResp(json.dumps({"success": True}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    url = image_hosting.upload_image_to_r2(b"PNG-DATA", "image/png")
    assert url.startswith("v-CF_R2_PUBLIC_BASE/vision/")
    assert url.endswith(".png")
    assert "v-CF_R2_ACCOUNT_ID" in captured["url"]
    assert captured["url"].endswith("/objects/vision/" + url.split("vision/", 1)[1])
    assert captured["auth"] == "Bearer v-CF_R2_TOKEN"
    assert captured["ctype"] == "image/png"
    assert captured["data"] == b"PNG-DATA"


def test_upload_success_false_returns_none(monkeypatch):
    _set_env(monkeypatch)

    def fake_urlopen(req, timeout=60):
        return _FakeResp(json.dumps({"success": False}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert image_hosting.upload_image_to_r2(b"x", "image/png") is None


def test_upload_network_error_returns_none(monkeypatch):
    _set_env(monkeypatch)

    def fake_urlopen(req, timeout=60):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert image_hosting.upload_image_to_r2(b"x", "image/png") is None


def test_upload_bad_json_returns_none(monkeypatch):
    _set_env(monkeypatch)

    def fake_urlopen(req, timeout=60):
        return _FakeResp(b"not-json")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert image_hosting.upload_image_to_r2(b"x", "image/png") is None