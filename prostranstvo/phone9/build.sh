#!/bin/sh
# Сборка APK малого стенда шага 9б без Gradle: javac → d8 → aapt2 → zipalign →
# apksigner — так же, как стенд шага 8 (phone8/build.sh).
#
#   sh build.sh ИСПОЛНИТЕЛЬ_ARM64 SERVE9_ПЕРЕНОСИМАЯ SERVE9_БЫСТРАЯ
#   APP=k9 sh build.sh …   — стенд калибровки (AndroidManifestK9.xml, kalibr9.apk)
#
# Исполнитель — вторая реализация под aarch64-linux-android; программы модели
# — model9/native/build.py android. В APK они кладутся как
# lib/arm64-v8a/libexecutor7.so, libserve9a.so, libserve9b.so. Исполнитель
# запускается из папки библиотек, библиотеки модели загружает ModelService.
#
# Ключ подписи — вне этой папки (KS, KSPASS): ключи в архив не кладутся. Новый
# ключ — новая подпись: поверх установки с другим ключом APK не встанет.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
SDK=${SDK:-/home/claude/android/sdk}
BT=$SDK/build-tools/34.0.0
JAR=$SDK/platforms/android-34/android.jar
NDKBIN=${NDKBIN:-/home/claude/android/android-ndk-r27d/toolchains/llvm/prebuilt/linux-x86_64/bin}
APP=${APP:-9}
if [ "$APP" = k9 ]; then MANIFEST=$HERE/AndroidManifestK9.xml; NAME=kalibr9; else MANIFEST=$HERE/AndroidManifest.xml; NAME=stend9; fi
KS=${KS:-/home/claude/android/$NAME.jks}
KSPASS=${KSPASS:-$(cat /home/claude/android/$NAME.pass)}
EXE=$1; SA=$2; SB=$3
for f in "$EXE" "$SA" "$SB"; do [ -f "$f" ] || { echo "нет файла: $f"; exit 1; }; done
P8=$HERE/../phone8/src/stend

B=$HERE/build/apk
rm -rf "$B" && mkdir -p "$B/classes" "$B/stage/lib/arm64-v8a" "$B/dex"

javac --release 8 -encoding UTF-8 -cp "$JAR" -d "$B/classes" \
  $(find "$P8/core" -name '*.java') "$P8/android/AndroidDevice.java" \
  $(find "$HERE/src/stend9/core" "$HERE/src/stend/android" -name '*.java')
"$BT/d8" --min-api 29 --lib "$JAR" --output "$B/dex" $(find "$B/classes" -name '*.class')

"$BT/aapt2" link -o "$B/base.apk" --manifest "$MANIFEST" -I "$JAR" \
  --min-sdk-version 29 --target-sdk-version 34 -A "$HERE/assets"

cp "$B/dex/classes.dex" "$B/stage/"
"$NDKBIN/llvm-strip" -o "$B/stage/lib/arm64-v8a/libexecutor7.so" "$EXE"
cp "$SA" "$B/stage/lib/arm64-v8a/libserve9a.so"
cp "$SB" "$B/stage/lib/arm64-v8a/libserve9b.so"
(cd "$B/stage" && zip -q -r "$B/base.apk" classes.dex lib)

"$BT/zipalign" -p -f 4 "$B/base.apk" "$B/aligned.apk"
"$BT/apksigner" sign --ks "$KS" --ks-pass "pass:$KSPASS" --out "$HERE/build/$NAME.apk" "$B/aligned.apk"
"$BT/apksigner" verify --print-certs "$HERE/build/$NAME.apk" | head -3
echo "готово: $HERE/build/$NAME.apk"
