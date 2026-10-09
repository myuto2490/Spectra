import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _version import parse, read_version  # noqa: E402
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
