from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import register_spa


def _make_app(tmp_path):
    (tmp_path / "index.html").write_text("<html>SPA-SHELL</html>")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    (tmp_path / "manual.html").write_text("<html>USER MANUAL</html>")
    app2 = FastAPI()

    @app2.get("/api-route")
    def api_route():
        return {"ok": True}

    register_spa(app2, str(tmp_path))
    return TestClient(app2)


def test_api_routes_win_over_spa(tmp_path):
    client = _make_app(tmp_path)
    assert client.get("/api-route").json() == {"ok": True}


def test_root_serves_index(tmp_path):
    client = _make_app(tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "SPA-SHELL" in resp.text
    assert resp.headers["cache-control"] == "no-store"


def test_client_route_falls_back_to_index(tmp_path):
    client = _make_app(tmp_path)
    resp = client.get("/some/client/route")
    assert resp.status_code == 200
    assert "SPA-SHELL" in resp.text


def test_real_static_file_served(tmp_path):
    client = _make_app(tmp_path)
    assert client.get("/favicon.svg").text == "<svg/>"


def test_manual_html_served_anonymously(tmp_path):
    """Invariant 3 (spec §不変条件): /manual.html is a real static file in
    dist, reachable via the SPA catch-all with no auth required."""
    client = _make_app(tmp_path)
    resp = client.get("/manual.html")
    assert resp.status_code == 200
    assert "USER MANUAL" in resp.text


def _make_app_with_outside_sentinel(tmp_path):
    """Build a dist dir as a *sibling* of a sentinel file that lives outside
    it, so a path-traversal attempt has something real to try to reach."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>SPA-SHELL</html>")

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    sentinel = outside_dir / "secret.txt"
    sentinel.write_text("TOP-SECRET-OUTSIDE-DIST")

    app2 = FastAPI()
    register_spa(app2, str(dist_dir))
    return TestClient(app2), sentinel


def test_dotdot_path_traversal_falls_back_to_index(tmp_path):
    client, sentinel = _make_app_with_outside_sentinel(tmp_path)
    resp = client.get("/../outside/secret.txt")
    assert resp.status_code == 200
    assert "SPA-SHELL" in resp.text
    assert sentinel.read_text() not in resp.text


def test_encoded_dotdot_path_traversal_falls_back_to_index(tmp_path):
    client, sentinel = _make_app_with_outside_sentinel(tmp_path)
    # %2e%2e is the URL-encoded form of "..". Starlette URL-decodes the path
    # before routing, so this reaches the handler as "../outside/secret.txt"
    # unless the handler itself re-resolves and bounds-checks it.
    resp = client.get("/%2e%2e/outside/secret.txt")
    assert resp.status_code == 200
    assert "SPA-SHELL" in resp.text
    assert sentinel.read_text() not in resp.text


def test_deep_encoded_traversal_never_serves_outside_file(tmp_path):
    client, sentinel = _make_app_with_outside_sentinel(tmp_path)
    resp = client.get("/a/b/%2e%2e/%2e%2e/%2e%2e/outside/secret.txt")
    assert resp.status_code == 200
    assert sentinel.read_text() not in resp.text
