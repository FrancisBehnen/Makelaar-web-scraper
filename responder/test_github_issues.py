"""Unit tests for github_issues.create_add_site_issue.

These exercise the pure request-building logic (labels, body, target service,
dedup) with a stubbed `requests` module, so no network or token is needed.
"""

import github_issues


class _FakeResp:
    def __init__(self, status_code=201, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _FakeRequests:
    """Records POST calls and returns a canned issue; GET returns open issues."""

    def __init__(self, open_issues=None, created_url="https://gh/issue/1"):
        self.open_issues = open_issues or []
        self.created_url = created_url
        self.post_calls = []
        self.get_calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.get_calls.append({"url": url, "params": params})
        return _FakeResp(200, self.open_issues)

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls.append({"url": url, "json": json})
        return _FakeResp(201, {"html_url": self.created_url})


def _patch(monkeypatch, fake):
    monkeypatch.setattr(github_issues, "requests", fake)
    monkeypatch.setattr(github_issues, "GH_TOKEN", "test-token")


def test_rental_issue_labels_and_body(monkeypatch):
    fake = _FakeRequests()
    _patch(monkeypatch, fake)

    url = github_issues.create_add_site_issue("https://www.example.nl/huis/1")

    assert url == "https://gh/issue/1"
    body = fake.post_calls[0]["json"]
    assert body["labels"] == ["add-site"]
    assert body["title"] == "Add site: example.nl"
    assert "python-sidecar/ADDING-SITES.md" in body["body"]
    assert "sales-sidecar" not in body["body"]


def test_sales_issue_labels_body_and_target(monkeypatch):
    fake = _FakeRequests()
    _patch(monkeypatch, fake)

    url = github_issues.create_add_site_issue(
        "https://www.example.nl/koop/1", sales=True
    )

    assert url == "https://gh/issue/1"
    body = fake.post_calls[0]["json"]
    assert body["labels"] == ["add-site", "sales"]
    assert body["title"] == "Add site: example.nl"
    # Targets the sales-sidecar and states its koop filters + room semantics.
    assert "sales-sidecar" in body["body"]
    assert "Delft" in body["body"]
    assert "270.000" in body["body"]
    assert "kamers" in body["body"]
    assert "python-sidecar/ADDING-SITES.md" not in body["body"]


def test_dedup_returns_existing_open_issue(monkeypatch):
    fake = _FakeRequests(
        open_issues=[
            {"title": "Add site: example.nl", "html_url": "https://gh/issue/existing"}
        ]
    )
    _patch(monkeypatch, fake)

    url = github_issues.create_add_site_issue("https://www.example.nl/koop/1", sales=True)

    assert url == "https://gh/issue/existing"
    assert fake.post_calls == []  # no duplicate created


def test_missing_token_raises(monkeypatch):
    fake = _FakeRequests()
    monkeypatch.setattr(github_issues, "requests", fake)
    monkeypatch.setattr(github_issues, "GH_TOKEN", "")

    try:
        github_issues.create_add_site_issue("https://www.example.nl/huis/1")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError when GH_TOKEN is unset")
