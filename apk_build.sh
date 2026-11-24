#!/usr/bin/env bash
# ============================================================
#  Legado one-click APK builder (Git Bash)
#
#  Default build type: noR8 (same package as release, signed,
#  keeps existing app data, no R8 minification).
#
#  Usage:
#    ./apk_build.sh                      interactive menu
#    ./apk_build.sh core                 core noR8 APK (default)
#    ./apk_build.sh comic                comic noR8 APK
#    ./apk_build.sh core debug           core debug APK (.debug app)
#    ./apk_build.sh core release         core release APK (R8)
#    ./apk_build.sh all                  core + comic noR8
#    ./apk_build.sh core share           build core noR8, then LAN share
#    ./apk_build.sh core noR8 share      same as above
#
#  Output: build/apk-output/
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

DEFAULT_JDK21="/d/devtools/jdk21/jdk-21.0.12.1+1"
DEFAULT_GRADLE_HOME="/d/devtools/gradle-home"
DEFAULT_ANDROID_HOME="D:\\devtools\\android-sdk"

JAVA_BIN=""
GRADLE_USER_HOME_POSIX=""

usage() {
    sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

win_path() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$1"
    else
        printf '%s' "$1"
    fi
}

unix_path() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -u "$1"
    else
        printf '%s' "$1"
    fi
}

setup_env() {
    if [[ ! -f "$REPO_ROOT/gradlew" ]]; then
        echo "[ERROR] gradlew not found. Put apk_build.sh in the repository root." >&2
        exit 1
    fi

    # ---- JDK 21 ----
    if [[ -n "${JDK21_HOME:-}" ]]; then
        if [[ "$JDK21_HOME" == *\\* ]]; then
            JDK21_HOME="$(unix_path "$JDK21_HOME")"
        fi
    else
        JDK21_HOME="$DEFAULT_JDK21"
    fi

    if [[ -x "$JDK21_HOME/bin/java.exe" ]]; then
        JAVA_BIN="$JDK21_HOME/bin/java.exe"
    elif [[ -x "$JDK21_HOME/bin/java" ]]; then
        JAVA_BIN="$JDK21_HOME/bin/java"
    elif [[ -n "${JAVA_HOME:-}" ]]; then
        if [[ "$JAVA_HOME" == *\\* ]]; then
            JAVA_HOME="$(unix_path "$JAVA_HOME")"
        fi
        if [[ -x "$JAVA_HOME/bin/java.exe" ]]; then
            JAVA_BIN="$JAVA_HOME/bin/java.exe"
        elif [[ -x "$JAVA_HOME/bin/java" ]]; then
            JAVA_BIN="$JAVA_HOME/bin/java"
        fi
    fi

    if [[ -z "$JAVA_BIN" ]]; then
        echo "[ERROR] JDK 21 not found." >&2
        echo "        Set JDK21_HOME, or install JDK 21 at $DEFAULT_JDK21" >&2
        exit 1
    fi

    local java_settings
    java_settings="$("$JAVA_BIN" -XshowSettings:properties -version 2>&1 || true)"
    if [[ "$java_settings" != *"java.specification.version = 21"* ]]; then
        echo "[ERROR] This project requires JDK 21, but JAVA_HOME points to:" >&2
        echo "        $JAVA_BIN" >&2
        echo "        Set JDK21_HOME to a JDK 21 directory." >&2
        exit 1
    fi
    export JAVA_HOME="$(dirname "$(dirname "$JAVA_BIN")")"

    # ---- Gradle user home (holds RELEASE_* signing properties) ----
    if [[ -z "${GRADLE_USER_HOME:-}" ]]; then
        if [[ -d "$DEFAULT_GRADLE_HOME" ]]; then
            GRADLE_USER_HOME_POSIX="$DEFAULT_GRADLE_HOME"
        else
            GRADLE_USER_HOME_POSIX="$HOME/.gradle"
        fi
    elif [[ "$GRADLE_USER_HOME" == /* ]]; then
        GRADLE_USER_HOME_POSIX="$GRADLE_USER_HOME"
    else
        GRADLE_USER_HOME_POSIX="$(unix_path "$GRADLE_USER_HOME")"
    fi
    export GRADLE_USER_HOME="$(win_path "$GRADLE_USER_HOME_POSIX")"

    # ---- Android SDK ----
    if [[ -z "${ANDROID_HOME:-}" && -n "${ANDROID_SDK_ROOT:-}" ]]; then
        export ANDROID_HOME="$ANDROID_SDK_ROOT"
    fi
    if [[ -z "${ANDROID_HOME:-}" && -d /d/devtools/android-sdk ]]; then
        export ANDROID_HOME="$DEFAULT_ANDROID_HOME"
    fi
    if [[ -n "${ANDROID_HOME:-}" && "$ANDROID_HOME" == /* ]]; then
        export ANDROID_HOME="$(win_path "$ANDROID_HOME")"
    fi
    if [[ -z "${ANDROID_HOME:-}" && ! -f "$REPO_ROOT/local.properties" ]]; then
        echo "[WARN] ANDROID_HOME is not set and local.properties is missing."
        echo "[WARN] Gradle may fail to locate the Android SDK."
    fi

    echo
    echo "  JAVA_HOME        = $JAVA_HOME"
    echo "  GRADLE_USER_HOME = $GRADLE_USER_HOME"
    if [[ -n "${ANDROID_HOME:-}" ]]; then
        echo "  ANDROID_HOME     = $ANDROID_HOME"
    fi
    echo
}

check_signing() {
    local props="$GRADLE_USER_HOME_POSIX/gradle.properties"
    if ! grep -Eq '^RELEASE_STORE_FILE=.' "$props" 2>/dev/null; then
        echo "[WARN] RELEASE_STORE_FILE is not set in $props"
        echo "[WARN] This APK will be UNSIGNED and cannot be installed."
    fi
}

version_of() {
    local key="$1"
    sed -n "s/^${key}=//p" "$REPO_ROOT/app/version.properties" 2>/dev/null \
        | tr -d '\r' | head -n1
}

build_one() {
    local product="$1"
    local build="$2"
    local p_cap p_tag b_cap b_dir b_tag task apk_dir newest
    local v_major v_minor v_patch ver out_dir out_apk size_bytes size_mb

    case "$product" in
        core)  p_cap=Core;  p_tag=core ;;
        comic) p_cap=Comic; p_tag=comic ;;
        *)
            echo "[ERROR] Unknown product: $product  (core|comic|all)" >&2
            return 2
            ;;
    esac

    case "$build" in
        debug)   b_cap=Debug;   b_dir=debug;   b_tag=debug ;;
        release) b_cap=Release; b_dir=release; b_tag=release ;;
        noR8)    b_cap=NoR8;    b_dir=noR8;    b_tag=noR8 ;;
        *)
            echo "[ERROR] Unknown build type: $build  (debug|release|noR8)" >&2
            return 2
            ;;
    esac

    if [[ "$build" != "debug" ]]; then
        check_signing
    fi

    task="assembleApp${p_cap}${b_cap}"
    echo
    echo "============================================================"
    echo "  Building :app:${task}   [${p_tag} / ${b_tag}]"
    echo "============================================================"
    echo

    if ! ./gradlew ":app:${task}" -PenableAbiSplits=false \
        --no-configuration-cache --console=plain; then
        echo
        echo "[ERROR] Gradle build failed. See the output above." >&2
        return 1
    fi

    apk_dir="$REPO_ROOT/app/build/outputs/apk/app${p_cap}/${b_dir}"
    newest=""
    if compgen -G "$apk_dir/*.apk" >/dev/null 2>&1; then
        newest="$(ls -t "$apk_dir"/*.apk | head -n1)"
    fi
    if [[ -z "$newest" ]]; then
        echo "[ERROR] No APK found under $apk_dir" >&2
        return 3
    fi

    v_major="$(version_of VERSION_MAJOR)"
    v_minor="$(version_of VERSION_MINOR)"
    v_patch="$(version_of VERSION_PATCH)"
    if [[ -n "$v_major" ]]; then
        ver="${v_major}.${v_minor:-0}.${v_patch:-0}"
    else
        ver="dev"
    fi

    out_dir="$REPO_ROOT/build/apk-output"
    mkdir -p "$out_dir"
    out_apk="$out_dir/legado-${ver}-${p_tag}-${b_tag}.apk"
    if ! cp -f "$newest" "$out_apk"; then
        echo "[ERROR] Failed to copy APK to $out_apk" >&2
        return 3
    fi

    size_bytes="$(wc -c < "$out_apk")"
    size_mb=$((size_bytes / 1048576))
    echo
    echo "[OK] $out_apk"
    echo "     size: ${size_mb} MB"
    echo "     raw:  $newest"
    return 0
}

dispatch() {
    local product="$1"
    local build="$2"
    if [[ "$product" == "all" ]]; then
        build_one core "$build" || return 1
        build_one comic "$build" || return 1
    else
        build_one "$product" "$build" || return 1
    fi
}

start_share() {
    if [[ ! -f "$REPO_ROOT/tools/apk_share/apk_share.py" ]]; then
        echo "[WARN] LAN share tool missing: tools/apk_share/apk_share.py" >&2
        return 0
    fi
    mkdir -p "$REPO_ROOT/build"
    local log="$REPO_ROOT/build/apk-share.log"
    echo
    echo "Starting LAN share (log: $log) ..."
    nohup bash "$REPO_ROOT/apk_share.sh" >"$log" 2>&1 &
    disown >/dev/null 2>&1 || true
    sleep 1
    echo "  QR page should open in your browser."
    echo "  Stop with: ./apk_share.sh --stop"
}

menu() {
    local choice product build ans
    while true; do
        cat <<'EOF'
============================================================
  Legado APK builder
============================================================

  [1] core  noR8     universal, signed, keeps your data (default)
  [2] comic noR8     universal, signed, keeps your data
  [3] core  Debug    separate .debug app, isolated data
  [4] comic Debug    separate .debug app, isolated data
  [5] core  Release  R8 minified + signed
  [6] comic Release  R8 minified + signed
  [7] core + comic noR8
  [8] Start LAN share (newest APK)
  [0] Exit

EOF
        read -r -p "Select [1-8, Enter=1]: " choice || choice=""
        choice="${choice:-1}"
        case "$choice" in
            0) return 0 ;;
            1) product=core;  build=noR8 ;;
            2) product=comic; build=noR8 ;;
            3) product=core;  build=debug ;;
            4) product=comic; build=debug ;;
            5) product=core;  build=release ;;
            6) product=comic; build=release ;;
            7) product=all;   build=noR8 ;;
            8) start_share; return 0 ;;
            *)
                echo "[ERROR] Invalid choice: $choice" >&2
                continue
                ;;
        esac

        if ! dispatch "$product" "$build"; then
            echo
            echo "Build aborted." >&2
            return 1
        fi

        ans=""
        read -r -p "Start LAN share now? [Y/n]: " ans || ans=""
        ans="${ans:-Y}"
        if [[ "${ans,,}" != "n" ]]; then
            start_share
        fi
        return 0
    done
}

main() {
    case "${1:-}" in
        -h|--help|help)
            usage
            return 0
            ;;
    esac

    setup_env

    if [[ -z "${1:-}" ]]; then
        menu
        return $?
    fi

    local product="${1,,}"
    local build_type="${2:-}"
    local share_arg="${3:-}"
    local share_flag=0

    if [[ "${build_type,,}" == "share" ]]; then
        build_type=noR8
        share_flag=1
    fi
    if [[ "${share_arg,,}" == "share" ]]; then
        share_flag=1
    fi

    build_type="${build_type,,}"
    case "$build_type" in
        ""|nor8) build_type=noR8 ;;
        debug|release) ;;
        *)
            echo "[ERROR] Unknown build type: $build_type  (debug|release|noR8)" >&2
            return 2
            ;;
    esac

    case "$product" in
        core|comic|all) ;;
        *)
            echo "[ERROR] Unknown product: $product  (core|comic|all)" >&2
            return 2
            ;;
    esac

    if ! dispatch "$product" "$build_type"; then
        return 1
    fi
    if [[ "$share_flag" == 1 ]]; then
        start_share
    fi
    return 0
}

main "$@"