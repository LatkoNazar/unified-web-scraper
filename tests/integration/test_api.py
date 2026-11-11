from fastapi.testclient import TestClient

from unified_web_parser.interfaces.api import create_app


def test_api_inspection_and_unknown_job(tmp_path):
    config_dir = tmp_path / "jobs"
    config_dir.mkdir()
    client = TestClient(create_app(tmp_path / "data", config_dir))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/records").json() == []
    assert client.get("/runs").json() == []
    assert client.post("/jobs/unknown/run").status_code == 404
    assert client.get("/records?limit=-1").status_code == 422
