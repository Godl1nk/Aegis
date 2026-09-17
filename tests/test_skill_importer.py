"""Skill URL importer — GitHub path parsing."""
import httpx
import pytest

from services.memory.skill_importer import (
    ResolvedSource,
    SkillImportError,
    _assert_github_url,
    _fetch_bytes,
    _list_github_dir,
    parse_skill_source,
)


@pytest.fixture
def importer_requests(monkeypatch):
    calls = []
    responses = []

    def get(client, url, *, headers=None, follow_redirects=None):
        assert follow_redirects is False
        calls.append(url)
        status, response_headers, text = responses.pop(0)
        return httpx.Response(
            status, headers=response_headers, text=text,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.Client, "get", get)
    monkeypatch.setattr("src.url_safety._default_resolver", lambda host: ["93.184.216.34"])
    return calls, responses


def test_skills_page_extracts_github_link(importer_requests):
    calls, responses = importer_requests
    responses.append((200, {}, '<a href="https://github.com/o/r">Skill</a>'))
    src = parse_skill_source("https://skills.sh/o/r")
    assert (src.owner, src.repo) == ("o", "r")
    assert calls == ["https://skills.sh/o/r"]


def test_skills_relative_redirect_then_github(importer_requests):
    calls, responses = importer_requests
    responses.extend([
        (302, {"location": "/new"}, ""),
        (302, {"location": "https://github.com/o/r"}, ""),
        (200, {}, ""),
    ])
    src = parse_skill_source("https://skills.sh/old")
    assert (src.owner, src.repo) == ("o", "r")
    assert calls == ["https://skills.sh/old", "https://skills.sh/new", "https://github.com/o/r"]


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/", "http://127.0.0.1/", "https://example.com/",
])
def test_skills_redirect_rejected_before_request(importer_requests, target):
    calls, responses = importer_requests
    responses.append((302, {"location": target}, ""))
    with pytest.raises(SkillImportError, match="must stay on GitHub"):
        parse_skill_source("https://skills.sh/o/r")
    assert calls == ["https://skills.sh/o/r"]


@pytest.mark.parametrize("url", [
    "https://example.com/skills.sh", "https://skills.sh.example.com/o/r",
])
def test_skills_substring_does_not_trigger_fetch(importer_requests, url):
    calls, _ = importer_requests
    with pytest.raises(SkillImportError):
        parse_skill_source(url)
    assert calls == []


@pytest.mark.parametrize("host", ["skills.sh", "raw.githubusercontent.com"])
def test_importer_rejects_private_dns_before_request(monkeypatch, importer_requests, host):
    calls, _ = importer_requests
    monkeypatch.setattr("src.url_safety._default_resolver", lambda host: ["127.0.0.1"])
    with pytest.raises(SkillImportError, match="private/loopback"):
        if host == "skills.sh":
            parse_skill_source("https://skills.sh/o/r")
        else:
            _fetch_bytes("https://raw.githubusercontent.com/o/r/main/SKILL.md")
    assert calls == []


def test_skills_redirect_limit(importer_requests):
    calls, responses = importer_requests
    responses.extend([(302, {"location": "/loop"}, "")] * 5)
    with pytest.raises(SkillImportError, match="too many redirects"):
        parse_skill_source("https://skills.sh/loop")
    assert len(calls) == 5


def test_parse_github_blob_skill_md():
    src = parse_skill_source(
        "https://github.com/anthropics/skills/blob/main/skills/pdf/SKILL.md"
    )
    assert src.owner == "anthropics"
    assert src.repo == "skills"
    assert src.ref == "main"
    assert src.path.endswith("skills/pdf/SKILL.md")


def test_parse_github_tree_directory():
    src = parse_skill_source(
        "https://github.com/example/my-skills/tree/develop/caveman-skill"
    )
    assert src.owner == "example"
    assert src.repo == "my-skills"
    assert src.ref == "develop"
    assert src.path == "caveman-skill"


def test_parse_raw_github():
    src = parse_skill_source(
        "https://raw.githubusercontent.com/o/r/main/path/SKILL.md"
    )
    assert src.owner == "o"
    assert src.repo == "r"
    assert src.ref == "main"
    assert src.path == "path/SKILL.md"


def test_rejects_non_github():
    with pytest.raises(SkillImportError):
        parse_skill_source("https://example.com/skill.md")


def test_fetch_bytes_rejects_cross_host_redirect(monkeypatch):
    class _Resp:
        url = "https://evil.example/secret"
        status_code = 302
        headers = {"location": "https://evil.example/secret"}
        content = b"x"

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, follow_redirects=False):
            return _Resp()

    monkeypatch.setattr("services.memory.skill_importer.httpx.Client", _Client)
    monkeypatch.setattr(
        "services.memory.skill_importer.check_outbound_url",
        lambda url, *, block_private=False: (True, ""),
    )
    with pytest.raises(SkillImportError, match="must stay on GitHub"):
        _fetch_bytes("https://raw.githubusercontent.com/o/r/main/SKILL.md")


def test_fetch_bytes_rejects_metadata_redirect(monkeypatch):
    class _Resp:
        status_code = 302
        headers = {"location": "http://169.254.169.254/latest/meta-data"}
        content = b""

    _mock_httpx_client(monkeypatch, _Resp())
    with pytest.raises(SkillImportError, match="must stay on GitHub"):
        _fetch_bytes("https://raw.githubusercontent.com/o/r/main/SKILL.md")


def test_assert_github_url_allows_api_host():
    _assert_github_url(
        "https://api.github.com/repos/o/r/contents?ref=main",
        context="redirect target",
    )


def test_list_github_dir_accepts_api_github_response(monkeypatch):
    monkeypatch.setattr(
        "services.memory.skill_importer._fetch_text",
        lambda url: "# skill\n",
    )
    monkeypatch.setattr(
        "services.memory.skill_importer.check_outbound_url",
        lambda url, *, block_private=False: (True, ""),
    )

    class _Resp:
        url = "https://api.github.com/repos/o/r/contents?ref=main"
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [{
                "name": "SKILL.md",
                "type": "file",
                "download_url": "https://raw.githubusercontent.com/o/r/main/SKILL.md",
            }]

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, follow_redirects=False):
            return _Resp()

    monkeypatch.setattr("services.memory.skill_importer.httpx.Client", _Client)

    out = {}
    src = ResolvedSource(owner="o", repo="r", ref="main", path="")
    _list_github_dir(src, "", out)
    assert "SKILL.md" in out


def _mock_httpx_client(monkeypatch, response):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, follow_redirects=False):
            return response

    monkeypatch.setattr("services.memory.skill_importer.httpx.Client", _Client)
    monkeypatch.setattr(
        "services.memory.skill_importer.check_outbound_url",
        lambda url, *, block_private=False: (True, ""),
    )


def test_list_github_dir_surfaces_rate_limit(monkeypatch):
    class _Resp:
        url = "https://api.github.com/repos/o/r/contents?ref=main"
        status_code = 403

        def json(self):
            return {"message": "API rate limit exceeded for 203.0.113.1"}

    _mock_httpx_client(monkeypatch, _Resp())
    src = ResolvedSource(owner="o", repo="r", ref="main", path="")
    with pytest.raises(SkillImportError, match="rate limit"):
        _list_github_dir(src, "", {})


def test_fetch_bytes_surfaces_github_error_detail(monkeypatch):
    class _Resp:
        url = "https://raw.githubusercontent.com/o/r/main/SKILL.md"
        status_code = 403
        content = b""

        def json(self):
            return {"message": "Forbidden"}

    _mock_httpx_client(monkeypatch, _Resp())
    with pytest.raises(SkillImportError, match="GitHub request failed \\(403\\): Forbidden"):
        _fetch_bytes("https://raw.githubusercontent.com/o/r/main/SKILL.md")
