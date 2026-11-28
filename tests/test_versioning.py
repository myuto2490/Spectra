import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _version import fixed_versions, parse, read_version  # noqa: E402
from bump_version import next_version, update_changelog  # noqa: E402

import spectra  # noqa: E402


def test_single_source_of_truth():
    assert read_version() == spectra.__version__
    parse(spectra.__version__)


@pytest.mark.parametrize(
    ("current", "part", "expected"),
    [
        ("0.1.0", "patch", "0.1.1"),
        ("0.1.3", "minor", "0.2.0"),
        ("0.9.9", "major", "1.0.0"),
        ("1.2.0rc1", "patch", "1.2.0"),
        ("1.2.0", "1.3.0rc1", "1.3.0rc1"),
    ],
)
def test_next_version(current, part, expected):
    assert next_version(current, part) == expected


def test_invalid_version_rejected():
    with pytest.raises(SystemExit):
        parse("1.2")


def test_changelog_release_section():
    text = "# 変更履歴\n\n## [Unreleased]\n\n### 追加\n- 機能 A\n\n[Unreleased]: https://x/commits/develop\n"
    out = update_changelog(text, "0.1.0", "0.2.0", "2026-01-01")
    assert "## [Unreleased]\n\n## [0.2.0] - 2026-01-01\n\n### 追加\n- 機能 A" in out
    assert "[0.2.0]: https://github.com/myuto2490/Spectra/releases/tag/v0.2.0" in out


def test_changelog_requires_entries():
    with pytest.raises(SystemExit):
        update_changelog("## [Unreleased]\n\n## [0.1.0] - 2026-01-01\n- x\n", "0.1.0", "0.2.0", "2026-01-02")


def test_release_notes_extracts_section(tmp_path, monkeypatch):
    import release_notes

    log = tmp_path / "CHANGELOG.md"
    log.write_text(
        "## [Unreleased]\n\n## [0.2.0] - 2026-01-01\n\n### 追加\n- B\n\n## [0.1.0] - 2025-12-01\n- A\n\n[0.2.0]: x\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release_notes, "CHANGELOG", log)
    assert release_notes.notes("v0.2.0") == "### 追加\n- B\n"
    assert release_notes.notes("0.1.0") == "- A\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("最新は v0.4.7 です", ["v0.4.7"]),
        ("Spectra 1.2.3rc1 から", ["1.2.3rc1"]),
        ("Spectra 0.4.7.", ["0.4.7"]),
        ("`Spectra-x.y.z-macOS-arm64.dmg`", []),
        ("git tag -a vX.Y.Z", []),
        ("[Conventional Commits](https://www.conventionalcommits.org/ja/v1.0.0/)", []),
        ("https://github.com/myuto2490/Spectra/releases/tag/v0.4.7", []),
        ("127.0.0.1 と 1.2.3.4", []),
        ("Python 3.10 以上、AGPL-3.0、OFL 1.1", []),
    ],
)
def test_fixed_versions(text, expected):
    assert [v for _, v in fixed_versions(text)] == expected


def test_fixed_versions_reports_line_numbers():
    assert fixed_versions("a\nb 1.0.0\n") == [(2, "1.0.0")]


def test_docs_have_no_fixed_versions():
    """README.md・SECURITY.md・CONTRIBUTING.md に固定のバージョン番号を書かない（リリースのたびに古くならないように）。"""
    from check_version import versionless_doc_errors

    assert versionless_doc_errors() == []


def test_check_version_reports_fixed_versions(tmp_path):
    from check_version import versionless_doc_errors

    (tmp_path / "README.md").write_text("最新版は 0.4.7 です\n", encoding="utf-8")
    errors = versionless_doc_errors(tmp_path)
    assert len(errors) == 1 and "README.md:1" in errors[0] and "0.4.7" in errors[0]
