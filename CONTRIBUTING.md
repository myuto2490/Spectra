# コントリビュートガイド

Spectra への関心をありがとうございます。不具合の報告・改善の提案・プルリクエストを歓迎します。

## 不具合の報告

[Issue](https://github.com/myuto2490/Spectra/issues/new/choose) のテンプレートに沿って報告してください。
レイアウトの崩れを報告する場合は、**崩れた箇所のスクリーンショット**と、可能であれば**公開されている論文の URL**（arXiv など）を添えてください。
著作権のある PDF ファイルそのものは添付しないでください。

## 開発環境

```sh
git clone https://github.com/myuto2490/Spectra.git
cd Spectra
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

- `SPECTRA_DEV=1` で、API を使わない「擬似翻訳」エンジンが選べるようになります。
- `SPECTRA_HOME=<ディレクトリ>` で、設定・キャッシュ・ログの保存先をまとめて切り替えられます（普段の設定を汚さずに試せます）。

## ブランチ運用

| ブランチ | 役割 |
| --- | --- |
| `main` | リリース済みの状態。タグ `vX.Y.Z` はここに付ける |
| `develop` | 次のリリースに向けた統合ブランチ |
| `feature/*` `fix/*` `docs/*` `ci/*` `chore/*` | 作業ブランチ。`develop` から作成し、`develop` へプルリクエスト |

## コミットメッセージ

[Conventional Commits](https://www.conventionalcommits.org/ja/v1.0.0/) の形式で書いてください。

```text
feat(core): 段落の自動縮小に下限を設ける
fix(ui): ダークモードでバナーの文字色が薄い問題を修正
docs: API キーの取得手順を更新
```

主な種類: `feat` `fix` `docs` `test` `refactor` `perf` `ci` `chore`

## プルリクエストの前に

- [ ] `pytest` と `ruff check src tests` が通ること
- [ ] 振る舞いを変えた場合はテストを追加・更新したこと
- [ ] 利用者に見える変更は `CHANGELOG.md` の `Unreleased` に追記したこと
- [ ] テストに実在の論文 PDF を含めていないこと（`tests/pdf_factory.py` で合成した PDF を使ってください）
- [ ] 翻訳 API に実際に接続するテストを書いていないこと（`httpx.MockTransport` を使ってください）

### レイアウト保持に関わる変更

`src/spectra/core/` を変更する場合は、図表・数式・翻訳対象外の文字が変化しないことを
`spectra.core.qa.check` で必ず確認してください。`tests/test_pipeline.py::test_layout_is_preserved` がその例です。
判定に迷うケースでは「訳さない」側に倒すのが方針です。

## バージョン管理とリリース

バージョンは [セマンティック バージョニング](https://semver.org/lang/ja/) に従い、
`src/spectra/__init__.py` の `__version__` だけで管理します（他の場所はすべてここから取得します）。

### リリース前の文書の確認

リリースのたびに、次の文書をそのリリースの内容に合わせて更新します（過去の版の説明のまま残さないため）。

- [ ] `README.md`：特長・使い方・キーボードショートカット・よくある質問・English の要約に、新しい機能と変わった操作が入っていること。既知の制限（訳さないもの）が今の動きと合っていること
- [ ] `SECURITY.md`：外部への通信（送り先・送る内容・オフにする方法）が今のコードと合っていること
- [ ] `docs/DESIGN.md`：ディレクトリ構成・判定の規則・画面構成・ショートカットが今のコードと合っていること
- [ ] スクリーンショット（`docs/images/`）：画面が変わったときは撮り直すこと
- [ ] `README.md`・`SECURITY.md`・`CONTRIBUTING.md` にバージョン番号を直接書かないこと（ダウンロードのファイル名は `x.y.z`、最新版はバッジとリンク `releases/latest` で示す。履歴の説明は CHANGELOG に書く）。
  `pytest`（`tests/test_versioning.py`）と、タグの push で動く `scripts/check_version.py` が、数字 3 つを点でつないだ番号を見つけるとエラーにします（URL の中は除きます）

リリース手順（メンテナー向け）:

```sh
git checkout develop
# 先に上の「リリース前の文書の確認」を済ませ、develop にマージしておく
python scripts/bump_version.py minor        # major / minor / patch / X.Y.Z
python scripts/check_version.py vX.Y.Z      # タグ・__version__・CHANGELOG の一致と、文書に固定の番号が無いことを確認
git commit -am "chore(release): vX.Y.Z"
git checkout main && git merge --no-ff develop
git tag -a vX.Y.Z -m "Spectra vX.Y.Z"
git push origin main develop vX.Y.Z
```

タグを push すると GitHub Actions がタグ・`__version__`・CHANGELOG の一致と、文書に固定のバージョン番号が無いことを検査し、
Windows / macOS / Linux 向けのアプリをビルドして Release を作成します。
コード署名の設定は [docs/CODE_SIGNING.md](docs/CODE_SIGNING.md) を参照してください。

## ライセンス

コントリビュートしていただいたコードは、本プロジェクトと同じ [AGPL-3.0-or-later](LICENSE) で配布されます。
