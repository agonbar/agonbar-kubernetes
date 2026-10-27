#!/usr/bin/env bash
# Re-sign a Morphe/ReVanced patched APK with a clean fresh key (v1+v2, no v3)
# and install it via adb. Works around Morphe's broken v3 signature that
# PackageInstaller rejects ("APK Signature Scheme v3 ... digest did not verify").
# Only for apps with password login: a new signature breaks Google sign-in, so
# Google-login apps (Duolingo) must stay on Morphe mount install instead.
# Usage: morphe-resign-install.sh <patched.apk> <package.name>
set -euo pipefail
APK="$1"; PKG="$2"
WORK=$(mktemp -d /tmp/resign.XXXX)
KS="$WORK/key.jks"
cp "$APK" "$WORK/app.apk"
adb() { nix shell nixpkgs#android-tools -c adb "$@" </dev/null; }

echo ">> ephemeral keystore"
nix shell nixpkgs#jdk -c keytool -genkeypair -keystore "$KS" -storepass morphe123 -keypass morphe123 \
  -alias k -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=resign" >/dev/null 2>&1

# zipalign is optional: Morphe output is already aligned
if nix shell nixpkgs#zipalign -c true >/dev/null 2>&1; then
  nix shell nixpkgs#zipalign -c zipalign -p -f 4 "$WORK/app.apk" "$WORK/aligned.apk" </dev/null && mv "$WORK/aligned.apk" "$WORK/app.apk"
fi

echo ">> sign (v1+v2, no v3/v4)"
nix shell nixpkgs#apksigner -c apksigner sign --ks "$KS" --ks-pass pass:morphe123 --key-pass pass:morphe123 \
  --v3-signing-enabled false --v4-signing-enabled false "$WORK/app.apk" </dev/null
nix shell nixpkgs#apksigner -c apksigner verify "$WORK/app.apk" </dev/null 2>&1 | grep -vE "^WARNING" | head -3 || true

echo ">> uninstall previous $PKG (if any)"
adb uninstall "$PKG" 2>/dev/null || true
echo ">> install"
adb install "$WORK/app.apk"
echo ">> status"
adb shell "pm path $PKG" | head -1
rm -rf "$WORK"
