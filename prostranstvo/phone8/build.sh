#!/bin/sh
# Сборка APK стенда шага 8 без Gradle: javac → d8 → aapt2 → zipalign → apksigner.
#
#   sh build.sh ИСПОЛНИТЕЛЬ_ARM64
#
# Исполнитель — программа второй реализации, собранная под
# aarch64-linux-android; в APK она кладётся как lib/arm64-v8a/libexecutor7.so
# (почему так — AndroidDevice.java).
#
# Ключ подписи — вне этой папки (KS, KSPASS): ключи в архив не кладутся.
# Новый ключ — новая подпись: поверх старой установки такой APK не встанет,
# старый стенд сначала удаляют.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
SDK=${SDK:-/home/claude/android/sdk}
BT=$SDK/build-tools/34.0.0
JAR=$SDK/platforms/android-34/android.jar
NDKBIN=${NDKBIN:-$SDK/ndk/27.3.13750724/toolchains/llvm/prebuilt/linux-x86_64/bin}
KS=${KS:-/home/claude/android/stend8.jks}
KSPASS=${KSPASS:-$(cat /home/claude/android/stend8.pass)}
EXE=$1
[ -x "$EXE" ] || { echo "нет исполнителя: $EXE"; exit 1; }

B=$HERE/build/apk
rm -rf "$B" && mkdir -p "$B/classes" "$B/stage/lib/arm64-v8a" "$B/dex"

javac --release 8 -encoding UTF-8 -cp "$JAR" -d "$B/classes" \
  $(find "$HERE/src/stend/core" "$HERE/src/stend/android" -name '*.java')
"$BT/d8" --min-api 29 --lib "$JAR" --output "$B/dex" $(find "$B/classes" -name '*.class')

"$BT/aapt2" link -o "$B/base.apk" --manifest "$HERE/AndroidManifest.xml" -I "$JAR" \
  --min-sdk-version 29 --target-sdk-version 34 -A "$HERE/assets"

cp "$B/dex/classes.dex" "$B/stage/"
"$NDKBIN/llvm-strip" -o "$B/stage/lib/arm64-v8a/libexecutor7.so" "$EXE"
(cd "$B/stage" && zip -q -r "$B/base.apk" classes.dex lib)

"$BT/zipalign" -p -f 4 "$B/base.apk" "$B/aligned.apk"
"$BT/apksigner" sign --ks "$KS" --ks-pass "pass:$KSPASS" --out "$HERE/build/stend8.apk" "$B/aligned.apk"
"$BT/apksigner" verify --print-certs "$HERE/build/stend8.apk" | head -3
echo "готово: $HERE/build/stend8.apk"
