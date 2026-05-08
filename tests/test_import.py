from pathlib import Path


def test_import_app(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WORKSPACE_DIR", str(root / "workspace"))
    monkeypatch.setenv("DATA_DIR", str(root / "data"))

    import app.main  # noqa: F401
