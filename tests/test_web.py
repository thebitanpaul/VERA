"""HTTP surface: contract, validation, and the escalation flow."""

from __future__ import annotations

import json


def test_index_renders():
    from web import create_app
    with create_app({"TESTING": True}).test_client() as c:
        body = c.get("/").get_data(as_text=True)
    assert "VERA" in body
    assert 'id="question"' in body


def test_index_has_the_copyright_bar_and_key_hint(client):
    body = client.get("/").get_data(as_text=True)
    assert 'id="copyright-bar"' in body
    assert "phiUture" in body
    assert 'id="model-hint-bubble"' in body
    assert 'id="model-hint-dismiss"' in body


def test_model_hint_hidden_attribute_is_not_overridden_by_display_flex(client):
    """Regression guard: `.model-hint { display: flex }` and the browser's own
    `[hidden] { display: none }` have equal specificity, and author CSS wins
    ties over the user-agent stylesheet. Without an explicit
    `.model-hint[hidden] { display: none }` override, setting
    `bubble.hidden = true` in app.js has no visible effect at all — confirmed
    live in Chrome: the element stayed on screen (`display: flex`) after
    dismissal. This only checks the override rule exists; it cannot evaluate
    the CSS cascade itself."""
    css = client.get("/static/styles.css").get_data(as_text=True)
    assert ".model-hint[hidden]" in css


def test_index_favicons_present(client):
    body = client.get("/").get_data(as_text=True)
    for size in ("favicon-16.png", "favicon-32.png", "favicon-64.png",
                "apple-touch-icon.png"):
        assert size in body


def test_favicon_files_are_served(client):
    for name in ("favicon-16.png", "favicon-32.png", "favicon-64.png",
                "apple-touch-icon.png", "logo.png"):
        r = client.get(f"/static/{name}")
        assert r.status_code == 200
        assert r.data[:8] == b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------------------------- crawlers
def test_robots_txt_allows_crawling_and_lists_this_hosts_sitemap(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert r.mimetype == "text/plain"
    body = r.get_data(as_text=True)
    assert "User-agent: *" in body
    assert "Allow: /" in body
    # Nothing may disallow the one page Google is asked to index.
    assert "Disallow: /\n" not in body
    assert "Sitemap: https://vera.phiuture.com/sitemap.xml" in body


def test_sitemap_lists_the_page_with_an_https_absolute_url(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.mimetype == "application/xml"
    body = r.get_data(as_text=True)
    assert "<loc>https://vera.phiuture.com/</loc>" in body
    assert "<lastmod>" in body


def test_crawler_files_survive_a_sleeping_origin(client):
    """Google reads an unreachable robots.txt as a full disallow, so both files
    must be cacheable long enough for the CDN to answer while the instance is
    idled out — hence stale-if-error, not just a max-age."""
    for path in ("/robots.txt", "/sitemap.xml"):
        r = client.get(path)
        cache = r.headers["Cache-Control"]
        assert "max-age=86400" in cache
        assert "stale-if-error" in cache
        # A shorter Expires alongside that Cache-Control reads as a conflict.
        assert "Expires" not in r.headers


def test_index_declares_an_https_canonical(client):
    body = client.get("/").get_data(as_text=True)
    assert '<link rel="canonical" href="https://vera.phiuture.com/">' in body


# ----------------------------------------------------------- security headers
def test_security_headers_present(client):
    r = client.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "camera=()" in r.headers["Permissions-Policy"]

    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    # No blanket 'unsafe-inline' for scripts — only a per-request nonce.
    assert "script-src 'self' 'nonce-" in csp
    assert "script-src 'unsafe-inline'" not in csp


def test_csp_nonce_matches_the_inline_script_tag(client):
    r = client.get("/")
    csp = r.headers["Content-Security-Policy"]
    nonce = csp.split("'nonce-", 1)[1].split("'", 1)[0]
    body = r.get_data(as_text=True)
    assert f'nonce="{nonce}"' in body


def test_csp_nonce_is_fresh_per_request(client):
    n1 = client.get("/").headers["Content-Security-Policy"]
    n2 = client.get("/").headers["Content-Security-Policy"]
    assert n1 != n2


def test_api_responses_also_carry_security_headers(client):
    r = client.get("/healthz")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in r.headers


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "ok"
    assert data["trials"] == ["412", "778"]
    assert data["backend"] == "mock"


def test_trials_include_the_full_version_timeline(client):
    data = client.get("/api/trials?as_of=2026-01-01").get_json()
    trial = next(t for t in data["trials"] if t["id"] == "412")
    assert trial["id"] == "412"
    assert trial["governing"] == "3.0"
    assert [v["version"] for v in trial["versions"]] == ["1.0", "2.0", "3.0"]
    assert sum(v["governing"] for v in trial["versions"]) == 1
    assert trial["first_effective_date"] == "2023-06-01"
    assert trial["historical"] is False


def test_trials_respects_as_of(client):
    """A past date marks the version in force then, flagged historical."""
    data = client.get("/api/trials?as_of=2023-07-01").get_json()
    trial = next(t for t in data["trials"] if t["id"] == "412")
    assert trial["governing"] == "1.0"
    assert trial["historical"] is True
    # Trial 778 did not exist yet.
    other = next(t for t in data["trials"] if t["id"] == "778")
    assert other["governing"] is None


def test_trials_rejects_a_bad_date(client):
    assert client.get("/api/trials?as_of=yesterday").status_code == 400


def test_sections_endpoint(client):
    data = client.get("/api/trials/412/sections").get_json()
    assert data["version"] == "3.0"
    assert "5.2" in [s["id"] for s in data["sections"]]


def test_ask_answered(client):
    r = client.post("/api/ask", json={"question": "Is concomitant metformin permitted?",
                                      "trial": "412"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["answered"] is True
    assert data["governing"]["version"] == "3.0"
    assert data["passages"]
    assert data["usage"]["calls"] == 0


def test_ask_abstains(client):
    data = client.post("/api/ask", json={"question": "Is canagliflozin allowed?",
                                         "trial": "412"}).get_json()
    assert data["answered"] is False
    assert "not present in any version" in data["reason"]


def test_ask_honours_as_of(client):
    """Asked as of 2023, the answer comes from v1.0, which excluded metformin."""
    data = client.post("/api/ask", json={"question": "Is concomitant metformin permitted?",
                                         "trial": "412", "as_of": "2023-07-01"}).get_json()
    assert data["answered"] is True
    assert data["governing"]["version"] == "1.0"
    assert data["governing"]["status"] == "superseded"
    assert data["historical"] is True
    assert "excluded" in data["answer"]


def test_ask_before_the_first_version_is_an_input_problem(client):
    data = client.post("/api/ask", json={"question": "Is concomitant metformin permitted?",
                                         "trial": "412", "as_of": "2020-01-01"}).get_json()
    assert data["answered"] is False
    assert data["reason_kind"] == "date_before_first_version"
    assert data["first_effective_date"] == "2023-06-01"
    assert data["backend"] == ""      # no backend ran; UI must not show "unknown"
    assert "before" in data["reason"]


def test_trial_isolation_same_question_different_answers(client):
    """The core guarantee: each trial is answered only from its own protocol."""
    q = "Is concomitant metformin permitted?"
    a = client.post("/api/ask", json={"question": q, "trial": "412"}).get_json()
    b = client.post("/api/ask", json={"question": q, "trial": "778"}).get_json()

    assert a["answered"] and b["answered"]
    assert "2000 mg/day" in a["answer"]             # 412 caps the dose
    assert "without a dose ceiling" in b["answer"]  # 778 does not
    assert all(p["trial"] == "412" for p in a["passages"])
    assert all(p["trial"] == "778" for p in b["passages"])


def test_ask_validation(client):
    assert client.post("/api/ask", json={}).status_code == 400
    assert client.post("/api/ask", json={"question": "q"}).status_code == 400
    assert client.post("/api/ask", json={"trial": "412"}).status_code == 400
    assert client.post("/api/ask", json={"question": "q", "trial": "412",
                                         "backend": "llama"}).status_code == 400
    assert client.post("/api/ask", json={"question": "q", "trial": "412",
                                         "as_of": "soon"}).status_code == 400
    assert client.post("/api/ask", json={"question": "x" * 3000,
                                         "trial": "412"}).status_code == 400


def test_ask_rejects_non_json_body(client):
    assert client.post("/api/ask", data="not json",
                       content_type="text/plain").status_code == 400


def test_escalate_writes_a_work_item(client, isolated_settings):
    reason = "topic not present in any version of the protocol"
    data = client.post("/api/escalate", json={"question": "Is canagliflozin allowed?",
                                              "trial": "412",
                                              "reason": reason}).get_json()
    assert data["mode"] == "file"
    written = json.loads(open(data["path"], encoding="utf-8").read())
    assert written["trial"] == "412"
    assert written["reason"] == reason
    assert data["ado_payload"]["fields"]["System.Title"].startswith("VERA:")
    assert list(isolated_settings.escalation_dir.iterdir())


def test_escalate_validation(client):
    assert client.post("/api/escalate", json={"trial": "412"}).status_code == 400


def test_static_assets_are_served(client):
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
