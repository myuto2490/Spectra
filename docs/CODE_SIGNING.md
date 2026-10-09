# コード署名の設定

配布するアプリに署名すると、利用者が初回起動時に見る警告（macOS の「開発元を確認できません」、
Windows の SmartScreen）を解消・軽減できます。

証明書は**開発者本人の名義で取得する必要がある**ため、リポジトリには含めていません。
取得した証明書を GitHub の Secrets に登録すると、`v*` タグでのリリース時に自動で署名されます。
登録していない場合も、これまでどおり未署名（macOS はアドホック署名）でビルドされます。

| OS | 方法 | 費用 | 結果 |
| --- | --- | --- | --- |
| macOS | Apple Developer Program の Developer ID 証明書で署名し、Apple の公証を受ける | Apple Developer Program の年会費 | 警告なしで起動できる |
| Windows | SignPath Foundation（オープンソース向けの無料署名サービス）で署名 | 無料（審査あり） | 発行元が表示され、SmartScreen の警告が徐々に出なくなる |

---

## macOS：署名と公証

### 1. Apple Developer Program に登録する

1. [Apple Developer Program](https://developer.apple.com/jp/programs/) に「個人」として登録します（Apple ID、本人確認、年会費の支払いが必要です）。
2. 登録が完了すると、[メンバーシップの詳細](https://developer.apple.com/account) に **チーム ID**（英数字 10 文字）が表示されます。

### 2. Developer ID Application 証明書を作る

1. Mac の「キーチェーンアクセス」→ メニュー「証明書アシスタント」→「認証局に証明書を要求」で、ディスクに保存（CSR ファイル）します。
2. [Certificates](https://developer.apple.com/account/resources/certificates/list) で「+」→ **Developer ID Application** を選び、CSR をアップロードして証明書をダウンロードします。
3. ダウンロードした `.cer` をダブルクリックしてキーチェーンに追加します。
4. キーチェーンアクセスの「自分の証明書」で `Developer ID Application: 氏名 (チームID)` を右クリック →「書き出す」→ **.p12** 形式で、パスワードを付けて保存します。

### 3. 公証用の App 用パスワードを作る

[Apple Account](https://account.apple.com) →「サインインとセキュリティ」→「App 用パスワード」で、`Spectra notarization` などの名前で作成します。

### 4. GitHub に登録する

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で次を登録します。

| Secret 名 | 値 |
| --- | --- |
| `MACOS_CERTIFICATE` | .p12 ファイルを Base64 にした文字列（`base64 -i 証明書.p12 \| pbcopy` でコピー） |
| `MACOS_CERTIFICATE_PASSWORD` | .p12 に付けたパスワード |
| `APPLE_ID` | Apple Developer Program に登録した Apple ID（メールアドレス） |
| `APPLE_TEAM_ID` | チーム ID |
| `APPLE_APP_PASSWORD` | 手順 3 の App 用パスワード |

### 手元で試す

証明書をキーチェーンに入れた Mac で、次のように署名・公証できます。

```sh
cd packaging && pyinstaller --noconfirm spectra.spec && cd ..
MACOS_SIGN_IDENTITY="Developer ID Application: 氏名 (チームID)" \
APPLE_ID=you@example.com APPLE_TEAM_ID=XXXXXXXXXX APPLE_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx \
packaging/macos/sign_and_notarize.sh packaging/dist/Spectra.app Spectra.dmg "Spectra"
```

`MACOS_SIGN_IDENTITY=-` とするとアドホック署名になり、証明書なしでスクリプトの動作だけを確認できます。

---

## Windows：SignPath Foundation で署名

2023 年以降、Windows のコード署名証明書は秘密鍵をハードウェア（HSM）で保管することが義務づけられ、
証明書ファイルを CI に置いて署名する方法は使えなくなりました。
オープンソースのプロジェクトは、[SignPath Foundation](https://signpath.org/) の無料プログラムを使うのが現実的です
（署名の発行元は「SignPath Foundation」と表示されます）。

### 1. 申請する

1. [SignPath Foundation の申請ページ](https://signpath.org/apply) から申請します。審査では、OSI 承認のライセンス（Spectra は AGPL-3.0）で公開されていること、
   ビルドが公開の CI（GitHub Actions）で行われていることなどが確認されます。
2. 承認されると SignPath のアカウントが用意されます。

### 2. SignPath 側を設定する

1. プロジェクトを作成し、GitHub のリポジトリと連携します（Trusted Build System として GitHub Actions を登録）。
2. **Artifact configuration** で、アップロードされるフォルダの中の `Spectra.exe` と同梱の `.dll` / `.pyd` に Authenticode 署名するよう設定します。
3. **Signing policy**（例: `release-signing`）を作成します。
4. API トークンを発行します。

### 3. GitHub に登録する

| 種類 | 名前 | 値 |
| --- | --- | --- |
| Secret | `SIGNPATH_API_TOKEN` | SignPath の API トークン |
| Variable | `SIGNPATH_ORGANIZATION_ID` | SignPath の組織 ID |
| Variable | `SIGNPATH_PROJECT_SLUG` | プロジェクトの slug |
| Variable | `SIGNPATH_SIGNING_POLICY_SLUG` | 署名ポリシーの slug |

Variable は **Settings → Secrets and variables → Actions → Variables** タブで登録します。

> [!NOTE]
> SmartScreen は署名済みのアプリでも、ダウンロード数が少ないうちは警告を出すことがあります（評価が蓄積されると表示されなくなります）。

---

## 確認方法

署名の設定を登録したあと、タグを push して Release のビルドログを確認します。

- macOS: `==> 署名・公証が完了しました` と表示され、配布した DMG を開いても警告が出ないこと
- Windows: `Spectra.exe` の「プロパティ」→「デジタル署名」に署名が表示されること

設定が無い場合は、ビルドログに「未署名です」「公証を省略しました」という警告が表示されます。
