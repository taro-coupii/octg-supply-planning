def test_app_serves_openapi(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["title"] == "OCTG Supply Readiness Platform"
