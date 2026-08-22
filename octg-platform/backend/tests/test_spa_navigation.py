"""Browser navigation vs the SPA's own fetch(), on paths that are both.

/coverage, /wells/{id}, /scenarios/{id} and friends are simultaneously a React
Router route and a JSON endpoint. FastAPI matches the API first, so a planner
reloading one of those screens used to be handed raw JSON. The discriminator is
the Accept header, and `app.main` intercepts HTML-first GETs before routing.

WHY THIS IS AN EXEMPTION LIST RATHER THAN A ROUTE LIST
======================================================
It was a regex enumerating every client route with a "KEEP IN SYNC with the
<Routes> table" warning. That sync failed once already -- bare /wells was
missing and a trimmed URL landed on a JSON dead end (QA 2026-08-14) -- and
failing it is silent. Inverted after reviewing a sibling implementation, which
had generalised the same middleware: a navigation now gets the shell unless the
path is exempt.

The sibling's version exempts only the API docs, which would swallow its own
file downloads -- every one of them is an <a href> navigation and therefore
arrives HTML-first. That hole is what `test_a_download_link_still_downloads`
exists to keep shut here.
"""

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app, _STATIC_DIR
from app.models import AllocationPolicy, BusinessUnit, Customer

pytestmark = pytest.mark.skipif(
    not _STATIC_DIR.is_dir(),
    reason="the SPA middleware is only registered when a build is present",
)

HTML = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
FETCH = {"accept": "*/*"}


@pytest.fixture()
def client(db_session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/coverage",
        "/executive",
        "/surplus",
        "/approvals",
        "/wells",
        "/wells/some-id",
        "/scenarios/some-id",
        "/mrp/order-requirements",
        # Not a client route at all. The shell's own not-found screen -- with the
        # navigation still around it -- beats a bare JSON body either way.
        "/customers",
    ],
)
def test_a_browser_navigation_gets_the_app_shell(client, path):
    res = client.get(path, headers=HTML)
    assert res.status_code == 200, path
    assert res.headers["content-type"].startswith("text/html"), path
    assert "<!doctype" in res.text[:120].lower(), path


def test_adding_a_screen_needs_no_change_here(client):
    """The point of the inversion: an unknown client route already works.

    This path is deliberately one no router knows about. Under the old route
    list it would have been a JSON dead end until someone remembered to add it.
    """
    res = client.get("/a-screen-nobody-has-written-yet", headers=HTML)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")


def test_the_spa_fetch_of_the_same_path_still_reaches_the_api(client):
    """The other half of the contract: the client's own call must NOT be swallowed."""
    res = client.get("/coverage", headers=FETCH)
    assert res.headers["content-type"].startswith("application/json")


def test_the_navigation_response_is_uncacheable_and_varies_on_accept(client):
    """Without this the browser answers the fetch() from the cached NAVIGATION.

    Same URL, two representations. A cache that ignores Vary would hand the
    coverage screen an HTML body to parse as JSON, which is how this was first
    found.
    """
    res = client.get("/coverage", headers=HTML)
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["vary"] == "Accept"


def test_a_download_link_still_downloads(client, db_session):
    """The hazard the exemption list exists for.

    A download is an <a href> navigation, so it arrives with an HTML-first
    Accept header and is indistinguishable from a screen reload by header
    alone. If the middleware swallowed it, the user would receive the app shell
    named like a spreadsheet.
    """
    db_session.add(BusinessUnit(id="bu-dl", name="Tubular Downloads"))
    db_session.add(
        Customer(
            id="cust-dl",
            name="Acme Drilling",
            business_unit_id="bu-dl",
            allocation_policy=AllocationPolicy.SOFT,
        )
    )
    db_session.commit()

    res = client.get(
        "/demand-imports/template", params={"customer_id": "cust-dl"}, headers=HTML
    )
    assert res.status_code == 200, res.text
    assert "spreadsheet" in res.headers["content-type"]
    assert res.content[:2] == b"PK", "an xlsx is a zip container"


def test_the_health_probe_and_docs_answer_for_themselves(client):
    assert client.get("/health", headers=HTML).json() == {"status": "ok"}
    docs = client.get("/docs", headers=HTML)
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()


def test_a_real_file_in_the_build_is_served_as_itself(client):
    """A direct link to an asset must not be answered with the shell."""
    files = [p for p in _STATIC_DIR.iterdir() if p.is_file() and p.name != "index.html"]
    if not files:
        pytest.skip("this build has no root-level static file to check")
    res = client.get(f"/{files[0].name}", headers=HTML)
    assert res.status_code == 200
    assert "<!doctype html>" not in res.text[:120].lower() or files[0].suffix == ".html"
