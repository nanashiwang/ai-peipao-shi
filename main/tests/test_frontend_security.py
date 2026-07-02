from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_security_bootstrap_loads_before_auth_storage_probe():
    html = read(STATIC / "index.html")

    security_pos = html.index("/static/security.js")
    probe_pos = html.index('localStorage.getItem("controlUser")')

    assert security_pos < probe_pos
    assert "Content-Security-Policy" in html
    assert "object-src 'none'" in html
    assert "frame-ancestors 'none'" in html


def test_security_bootstrap_redirects_auth_tokens_to_session_storage():
    js = read(STATIC / "security.js")

    assert 'new Set(["controlUser", "chatUser"])' in js
    assert "window.sessionStorage" in js
    assert "Storage.prototype.setItem" in js
    assert "Storage.prototype.getItem" in js
    assert "storageRemove(window.localStorage" in js


def test_security_bootstrap_sanitizes_html_and_url_protocols():
    js = read(STATIC / "security.js")

    assert "sanitizeHTML" in js
    assert "insertAdjacentHTML" in js
    assert "Element.prototype.setAttribute" in js
    assert "javascript\\s*:" in js
    assert "data\\s*:" in js
    assert "SAFE_INLINE_HANDLERS" in js


def test_index_response_versions_security_js_and_sets_security_headers():
    main_py = read(ROOT / "app" / "main.py")

    assert 'for _n in ("security.js", "app.js", "style.css")' in main_py
    assert 'replace("/static/security.js", "/static/security.js?v=" + _v)' in main_py
    assert '"Content-Security-Policy"' in main_py
    assert '"X-Content-Type-Options": "nosniff"' in main_py
    assert '"Referrer-Policy": "same-origin"' in main_py


def test_send_artifacts_are_not_rendered_as_direct_href():
    app_js = read(STATIC / "app.js")

    assert 'href="${esc(log.screenshot_path)}"' not in app_js
    assert 'href="${esc(item.screenshot_path)}"' not in app_js
    assert 'data-artifact="${esc(log.screenshot_path)}"' in app_js
    assert 'data-artifact="${esc(item.screenshot_path)}"' in app_js
