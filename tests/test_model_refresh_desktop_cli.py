import pytest
from click.testing import CliRunner

from aram_nn.desktop import release
from aram_nn.heavy_jobs import ResourcePressure
from aram_nn.site import model_refresh_cli as cli


def test_existing_verified_data_publishes_before_training_admission(monkeypatch):
    calls = []

    def publish(**kwargs):
        calls.append("publish")
        assert kwargs["tag"] == "latest"
        assert kwargs["region"] == "TW"
        return True

    def waiting(**kwargs):
        assert calls == ["publish"], "Existing data must not wait for training memory"
        raise ResourcePressure("training needs more memory")

    monkeypatch.setattr(release, "publish_refreshed_models", publish)
    monkeypatch.setattr(cli, "refresh_models_once", waiting)
    result = CliRunner().invoke(cli.main, ["--desktop-release-tag", "latest"])
    assert result.exit_code == 1
    assert "[desktop-publish] published" in result.output
    assert "ResourcePressure" in result.output


def test_newly_trained_data_publishes_in_same_cycle(monkeypatch):
    calls = []

    def publish(**kwargs):
        calls.append("publish")
        return True

    def refreshed(**kwargs):
        calls.append("refresh")
        return {"refreshed": True, "current_patch": "16.20", "pool": ["16.20"],
                "elapsed_sec": 1, "local_total": 20000, "reason": "growth"}

    monkeypatch.setattr(release, "publish_refreshed_models", publish)
    monkeypatch.setattr(cli, "refresh_models_once", refreshed)
    result = CliRunner().invoke(cli.main, ["--desktop-release-tag", "latest"])
    assert result.exit_code == 0, result.output
    assert calls == ["publish", "refresh", "publish"]


@pytest.mark.parametrize("mode", ["--check-only", "--dry-run"])
def test_read_only_modes_never_publish(monkeypatch, mode):
    def unexpected(**kwargs):
        pytest.fail("Read-only modes must not upload")

    monkeypatch.setattr(release, "publish_refreshed_models", unexpected)
    monkeypatch.setattr(cli, "refresh_models_once", lambda **kwargs: {
        "would_refresh": False, "current_patch": "16.20", "local_total": 20000,
        "last_refreshed_total": 20000, "threshold": 5000, "reason": "growth gate"})
    result = CliRunner().invoke(cli.main, [mode, "--desktop-release-tag", "latest"])
    assert result.exit_code == 0, result.output


def test_publication_failure_does_not_stop_training(monkeypatch):
    calls = []

    def unavailable(**kwargs):
        calls.append("publish")
        raise OSError("GitHub unavailable")

    def refreshed(**kwargs):
        calls.append("refresh")
        return {"refreshed": True, "current_patch": "16.20", "elapsed_sec": 1,
                "local_total": 20000, "reason": "growth"}

    monkeypatch.setattr(release, "publish_refreshed_models", unavailable)
    monkeypatch.setattr(cli, "refresh_models_once", refreshed)
    result = CliRunner().invoke(cli.main, ["--desktop-release-tag", "latest"])
    assert result.exit_code == 0, result.output
    assert calls == ["publish", "refresh", "publish"]
    assert "will retry next cycle" in result.output


@pytest.mark.parametrize("enabled", [False, True])
def test_closed_growth_gate_publishes_once_only_when_enabled(monkeypatch, enabled):
    published = []
    monkeypatch.setattr(release, "publish_refreshed_models", lambda **kwargs: published.append(kwargs))
    monkeypatch.setattr(cli, "refresh_models_once", lambda **kwargs: {
        "current_patch": "16.20", "local_total": 20000, "last_refreshed_total": 20000,
        "threshold": 5000, "reason": "growth gate"})
    arguments = ["--desktop-release-tag", "latest"] if enabled else []
    result = CliRunner().invoke(cli.main, arguments)
    assert result.exit_code == 0, result.output
    assert len(published) == int(enabled)
