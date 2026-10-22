import pytest
from fastapi.testclient import TestClient

from project_os.api import create_app
from project_os.config import Settings
from tests.conftest import make_repo


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=tmp_path / "api.db", scan_interval_s=0, frontend_dist=tmp_path / "none",
                        exports_dir=tmp_path / "exports")
    with TestClient(create_app(settings)) as c:
        yield c


def test_root_scan_and_project_flow(client, workspace):
    make_repo(workspace / "alpha")

    assert client.post("/api/roots", json={"path": str(workspace)}).status_code == 201
    assert client.post("/api/roots", json={"path": str(workspace)}).status_code == 409

    run = client.post("/api/scans").json()
    assert run["status"] == "ok"

    overview = client.get("/api/overview").json()
    assert overview["total"] == 1
    assert overview["last_scan"]["id"] == run["id"]

    project = client.get("/api/projects/alpha").json()
    assert project["evidence"]["git"]["payload"]["branch"] == "main"

    r = client.patch("/api/projects/alpha", json={"lifecycle": "archived", "priority": 3})
    assert r.json()["lifecycle"] == "archived"
    assert client.get("/api/projects", params={"lifecycle": "archived"}).json()[0]["slug"] == "alpha"

    assert client.patch("/api/projects/alpha", json={"lifecycle": "bogus"}).status_code == 422
    assert client.get("/api/projects/nope").status_code == 404
    assert client.delete("/api/projects/alpha").status_code == 409


def test_manual_project_crud(client):
    r = client.post("/api/projects", json={"name": "Live set", "phase": "idea"})
    assert r.status_code == 201
    slug = r.json()["slug"]
    assert client.get("/api/events").json()[0]["event_type"] == "project_created"
    assert client.delete(f"/api/projects/{slug}").status_code == 204


def test_invalid_root(client, tmp_path):
    r = client.post("/api/roots", json={"path": str(tmp_path / "does-not-exist")})
    assert r.status_code == 409
