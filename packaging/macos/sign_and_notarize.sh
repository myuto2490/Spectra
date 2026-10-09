#!/usr/bin/env bash
# macOS アプリの署名・公証・ステープル。
#
# 使い方: packaging/macos/sign_and_notarize.sh <Spectra.app> <出力.dmg> <ボリューム名>
#
# 環境変数:
#   MACOS_SIGN_IDENTITY  署名に使う ID（例: "Developer ID Application: Your Name (ABCDE12345)"）
#                        "-" ならアドホック署名（公証はしない。ローカル検証用）
#   APPLE_ID / APPLE_TEAM_ID / APPLE_APP_PASSWORD
#                        公証（notarization）に使う Apple アカウント。未設定なら公証を省略する
set -euo pipefail

APP="$1"
DMG="$2"
VOLNAME="$3"
IDENTITY="${MACOS_SIGN_IDENTITY:?MACOS_SIGN_IDENTITY が未設定です}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENTITLEMENTS="$HERE/entitlements.plist"

sign() {
  codesign --force --timestamp --options runtime --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$@"
}
if [[ "$IDENTITY" == "-" ]]; then
  # アドホック署名ではタイムスタンプを付けられない
  sign() { codesign --force --options runtime --entitlements "$ENTITLEMENTS" --sign - "$@"; }
fi

echo "==> 内側のバイナリから署名"
# 深い階層から順に署名する（外側を先に署名すると内側の変更で署名が壊れるため）
deepest_first() { awk -F/ '{ print NF "\t" $0 }' | sort -rn | cut -f2-; }

while IFS= read -r f; do
  if file -b "$f" | grep -q "Mach-O"; then
    sign "$f"
  fi
done < <(find "$APP/Contents" -type f ! -path "*/_CodeSignature/*" | deepest_first)

while IFS= read -r fw; do
  sign "$fw"
done < <(find "$APP/Contents" -type d -name "*.framework" | deepest_first)

echo "==> アプリ本体を署名"
sign "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "==> DMG を作成"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "$VOLNAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

if [[ "$IDENTITY" == "-" ]]; then
  echo "==> アドホック署名のため公証は省略"
  exit 0
fi
codesign --force --timestamp --sign "$IDENTITY" "$DMG"

if [[ -z "${APPLE_ID:-}" || -z "${APPLE_TEAM_ID:-}" || -z "${APPLE_APP_PASSWORD:-}" ]]; then
  echo "::warning::APPLE_ID / APPLE_TEAM_ID / APPLE_APP_PASSWORD が未設定のため公証を省略しました"
  exit 0
fi

echo "==> 公証（数分かかります）"
xcrun notarytool submit "$DMG" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD" --wait
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"
spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
echo "==> 署名・公証が完了しました"
