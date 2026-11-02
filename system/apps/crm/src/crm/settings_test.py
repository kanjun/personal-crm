def test_cleanup_hidden_and_unreachable_by_default(client):
    assert b'href="/cleanup"' not in client.get("/").data
    assert client.get("/cleanup").status_code == 404


def test_cleanup_visible_only_when_enabled(client, monkeypatch):
    monkeypatch.setenv("CRM_CLEANUP_ENABLED", "true")
    assert b'href="/cleanup"' in client.get("/").data
    assert client.get("/cleanup").status_code == 200
