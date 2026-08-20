from app.models import Setting


def test_get_coverage_scope_defaults(client, db):
    resp = client.get("/admin/coverage-scope")
    assert resp.status_code == 200
    body = resp.json()
    assert body["statuses"] == ["Confirmed"]
    assert body["profiles"] == ["Primary", "Contingency"]


def test_put_coverage_scope_roundtrip(client, db):
    resp = client.put(
        "/admin/coverage-scope",
        json={"statuses": ["Planned", "Confirmed"], "profiles": ["Primary"]},
    )
    assert resp.status_code == 200

    resp = client.get("/admin/coverage-scope")
    assert resp.json()["statuses"] == ["Planned", "Confirmed"]
    assert resp.json()["profiles"] == ["Primary"]


def test_put_coverage_scope_rejects_unknown_value(client, db):
    resp = client.put(
        "/admin/coverage-scope",
        json={"statuses": ["Bogus"], "profiles": ["Primary"]},
    )
    assert resp.status_code == 422


def test_put_coverage_scope_rejects_empty_array(client, db):
    resp = client.put(
        "/admin/coverage-scope",
        json={"statuses": [], "profiles": ["Primary"]},
    )
    assert resp.status_code == 422
