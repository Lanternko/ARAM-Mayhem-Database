import json

from aram_nn.desktop import webview_host


def test_page_ships_with_the_render_hook():
    page = (webview_host.PAGE_DIR / "index.html").read_text(encoding="utf-8")
    assert "window.aramPick" in page
    assert "chrome.webview.postMessage" in page


def test_payload_script_embeds_json_and_waits_for_hook():
    script = webview_host.payload_script({"title": "安妮", "bench": []})
    assert "window.aramPick.render(" in script
    assert json.dumps({"title": "安妮", "bench": []}, ensure_ascii=False) in script


def test_asset_url_only_serves_files_under_the_asset_dir(tmp_path):
    icon = tmp_path / "icons" / "1.png"
    icon.parent.mkdir()
    icon.write_bytes(b"")
    assert webview_host.asset_url(tmp_path, icon) == "https://assets.aram/icons/1.png"
    assert webview_host.asset_url(tmp_path / "icons", tmp_path / "other.png") == ""
    assert webview_host.asset_url(tmp_path, None) == ""
