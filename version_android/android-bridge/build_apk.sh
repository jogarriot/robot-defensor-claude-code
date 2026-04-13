#!/bin/bash
set -e

SDK="/opt/homebrew/share/android-commandlinetools"
PLATFORM="$SDK/platforms/android-35/android.jar"
AAPT2="$SDK/build-tools/35.0.0/aapt2"
D8="$SDK/build-tools/35.0.0/d8"
ZIPALIGN="$SDK/build-tools/35.0.0/zipalign"
APKSIGNER="$SDK/build-tools/35.0.0/apksigner"

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

rm -rf out && mkdir -p out/compiled out/dex

echo "=== Compiling resources ==="
# Compile manifest (link step handles it)

echo "=== Linking APK ==="
$AAPT2 link \
    -o out/base.apk \
    --manifest app/src/main/AndroidManifest.xml \
    -I "$PLATFORM" \
    --min-sdk-version 26 \
    --target-sdk-version 35 \
    -v 2>&1 | tail -3

echo "=== Compiling Java ==="
mkdir -p out/classes
javac --release 17 \
    -cp "$PLATFORM" \
    -d out/classes \
    app/src/main/java/com/qunyu/method/BLEUtil.java \
    app/src/main/java/com/robobridge/MainActivity.java 2>&1

echo "=== Creating DEX ==="
$D8 --output out/dex $(find out/classes -name "*.class") 2>&1

echo "=== Adding DEX to APK ==="
cd out
cp base.apk unsigned.apk
# Add classes.dex to APK
zip -j unsigned.apk dex/classes.dex
cd "$DIR"

echo "=== Signing APK ==="
# Create debug keystore if needed
KEYSTORE="$DIR/out/debug.keystore"
if [ ! -f "$KEYSTORE" ]; then
    keytool -genkeypair -v \
        -keystore "$KEYSTORE" \
        -storepass android \
        -keypass android \
        -alias debug \
        -keyalg RSA \
        -keysize 2048 \
        -validity 10000 \
        -dname "CN=Debug" 2>&1 | tail -2
fi

# Zipalign
$ZIPALIGN -f 4 out/unsigned.apk out/aligned.apk

# Sign
$APKSIGNER sign \
    --ks "$KEYSTORE" \
    --ks-pass pass:android \
    --key-pass pass:android \
    --ks-key-alias debug \
    --out out/RoboBridge.apk \
    out/aligned.apk 2>&1

echo "=== Done ==="
ls -la out/RoboBridge.apk
