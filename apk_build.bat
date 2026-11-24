@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  Legado one-click APK builder
rem
rem  Default build type: noR8 (same package as release, signed,
rem  keeps existing app data, no R8 minification).
rem
rem  Usage:
rem    apk_build.bat                     interactive menu
rem    apk_build.bat core                core noR8 APK (default)
rem    apk_build.bat comic               comic noR8 APK
rem    apk_build.bat core debug          core debug APK (.debug app)
rem    apk_build.bat core release        core release APK (R8)
rem    apk_build.bat all                 core + comic noR8
rem    apk_build.bat core share          build core noR8, then LAN share
rem    apk_build.bat core noR8 share     same as above
rem
rem  Output: build\apk-output\
rem ============================================================

rem ---- machine-specific paths (override with env vars if needed) ----
if not defined JDK21_HOME set "JDK21_HOME=D:\devtools\jdk21\jdk-21.0.12.1+1"
if not defined GRADLE_USER_HOME if exist "D:\devtools\gradle-home" set "GRADLE_USER_HOME=D:\devtools\gradle-home"

rem ---- mode ----
set "MENU_MODE=0"
set "OUT_APK="
set "SHARE=0"
if "%~1"=="" set "MENU_MODE=1"

call :init_env
if errorlevel 1 goto :fail

if "%MENU_MODE%"=="1" goto :menu

set "PRODUCT=%~1"
if /i "%~3"=="share" set "SHARE=1"
set "BUILD=%~2"
if /i "%BUILD%"=="share" (
    set "BUILD=noR8"
    set "SHARE=1"
)
if "%BUILD%"=="" set "BUILD=noR8"
call :dispatch
if errorlevel 1 goto :fail
goto :done


rem ============================================================
rem  Menu
rem ============================================================
:menu
cls
echo ============================================================
echo   Legado APK builder
echo ============================================================
echo.
echo   [1] core  noR8     universal, signed, keeps your data (default)
echo   [2] comic noR8     universal, signed, keeps your data
echo   [3] core  Debug    separate .debug app, isolated data
echo   [4] comic Debug    separate .debug app, isolated data
echo   [5] core  Release  R8 minified + signed
echo   [6] comic Release  R8 minified + signed
echo   [7] core + comic noR8
echo   [8] Start LAN share (newest APK)
echo   [0] Exit
echo.
set "CHOICE="
set /p "CHOICE=Select [1-8, Enter=1]: "
if not defined CHOICE set "CHOICE=1"
set "CHOICE=%CHOICE: =%"
if "%CHOICE%"=="0" goto :done
if "%CHOICE%"=="1" goto :pick_core_nor8
if "%CHOICE%"=="2" goto :pick_comic_nor8
if "%CHOICE%"=="3" goto :pick_core_debug
if "%CHOICE%"=="4" goto :pick_comic_debug
if "%CHOICE%"=="5" goto :pick_core_release
if "%CHOICE%"=="6" goto :pick_comic_release
if "%CHOICE%"=="7" goto :pick_all_nor8
if "%CHOICE%"=="8" goto :pick_share
echo.
echo [ERROR] Invalid choice: %CHOICE%
pause
goto :menu

:pick_core_nor8
set "PRODUCT=core"
set "BUILD=noR8"
goto :menu_build

:pick_comic_nor8
set "PRODUCT=comic"
set "BUILD=noR8"
goto :menu_build

:pick_core_debug
set "PRODUCT=core"
set "BUILD=debug"
goto :menu_build

:pick_comic_debug
set "PRODUCT=comic"
set "BUILD=debug"
goto :menu_build

:pick_core_release
set "PRODUCT=core"
set "BUILD=release"
goto :menu_build

:pick_comic_release
set "PRODUCT=comic"
set "BUILD=release"
goto :menu_build

:pick_all_nor8
set "PRODUCT=all"
set "BUILD=noR8"
goto :menu_build

:pick_share
call :start_share
goto :done

:menu_build
call :dispatch
if errorlevel 1 goto :fail
set "ANS="
set /p "ANS=Start LAN share now? [Y/n]: "
if not defined ANS set "ANS=Y"
if /i "%ANS%"=="n" goto :done
set "SHARE=1"
goto :done


rem ============================================================
rem  Dispatch / build
rem ============================================================
:dispatch
if /i "%PRODUCT%"=="all" goto :dispatch_all
call :build "%PRODUCT%" "%BUILD%"
exit /b %errorlevel%

:dispatch_all
call :build core "%BUILD%"
if errorlevel 1 exit /b 1
call :build comic "%BUILD%"
exit /b %errorlevel%


:build
set "P=%~1"
set "B=%~2"

if /i "%P%"=="core" goto :prod_core
if /i "%P%"=="comic" goto :prod_comic
echo [ERROR] Unknown product: %P%  (core^|comic^|all)
exit /b 2

:prod_core
set "P_CAP=Core"
set "P_TAG=core"
goto :prod_ready

:prod_comic
set "P_CAP=Comic"
set "P_TAG=comic"
goto :prod_ready

:prod_ready
if /i "%B%"=="debug" goto :bt_debug
if /i "%B%"=="release" goto :bt_release
if /i "%B%"=="nor8" goto :bt_nor8
echo [ERROR] Unknown build type: %B%  (debug^|release^|noR8)
exit /b 2

:bt_debug
set "B_CAP=Debug"
set "B_DIR=debug"
set "B_TAG=debug"
goto :bt_ready

:bt_release
set "B_CAP=Release"
set "B_DIR=release"
set "B_TAG=release"
goto :bt_ready

:bt_nor8
set "B_CAP=NoR8"
set "B_DIR=noR8"
set "B_TAG=noR8"
goto :bt_ready

:bt_ready
if /i "%B%"=="release" call :check_signing
if /i "%B%"=="noR8" call :check_signing

set "TASK=assembleApp%P_CAP%%B_CAP%"
echo.
echo ============================================================
echo   Building :app:%TASK%   [%P_TAG% / %B_TAG%]
echo ============================================================
echo.

call gradlew.bat :app:%TASK% -PenableAbiSplits=false --no-configuration-cache --console=plain
if errorlevel 1 (
    echo.
    echo [ERROR] Gradle build failed. See the output above.
    exit /b 1
)

rem ---- locate the freshest APK and copy it to build\apk-output ----
set "APK_DIR=app\build\outputs\apk\app%P_CAP%\%B_DIR%"
set "NEWEST="
for /f "delims=" %%F in ('dir /b /a-d /o-d "%APK_DIR%\*.apk" 2^>nul') do if not defined NEWEST set "NEWEST=%APK_DIR%\%%F"

if not defined NEWEST (
    echo [ERROR] No APK found under %APK_DIR%
    exit /b 3
)

set "V_MAJOR="
set "V_MINOR="
set "V_PATCH="
for /f "tokens=2 delims==" %%A in ('findstr /b "VERSION_MAJOR=" app\version.properties 2^>nul') do set "V_MAJOR=%%A"
for /f "tokens=2 delims==" %%A in ('findstr /b "VERSION_MINOR=" app\version.properties 2^>nul') do set "V_MINOR=%%A"
for /f "tokens=2 delims==" %%A in ('findstr /b "VERSION_PATCH=" app\version.properties 2^>nul') do set "V_PATCH=%%A"
set "VER=%V_MAJOR%.%V_MINOR%.%V_PATCH%"
if not defined V_MAJOR set "VER=dev"

set "OUT_DIR=%~dp0build\apk-output"
if not exist "%OUT_DIR%" mkdir "%OUT_DIR%" >nul 2>&1
set "OUT_APK=%OUT_DIR%\legado-%VER%-%P_TAG%-%B_TAG%.apk"

copy /y "%NEWEST%" "%OUT_APK%" >nul
if errorlevel 1 (
    echo [ERROR] Failed to copy APK to %OUT_APK%
    exit /b 3
)

for %%A in ("%OUT_APK%") do set "OUT_SIZE=%%~zA"
set /a OUT_MB=%OUT_SIZE% / 1048576
echo.
echo [OK] %OUT_APK%
echo      size: %OUT_MB% MB
echo      raw:  %NEWEST%
exit /b 0


rem ============================================================
rem  Helpers
rem ============================================================
:start_share
if not exist "%~dp0tools\apk_share\apk_share.py" (
    echo [WARN] LAN share tool missing: tools\apk_share\apk_share.py
    exit /b 0
)
echo.
echo Starting LAN share window ...
start "Legado APK Share" /min "%~dp0apk_share.bat"
if errorlevel 1 echo [WARN] Failed to start the LAN share window.
timeout /t 2 /nobreak >nul 2>&1
echo   QR page should open in your browser.
echo   Stop with: apk_share.bat --stop
exit /b 0


:check_signing
findstr /r /c:"^RELEASE_STORE_FILE=." "%GRADLE_USER_HOME%\gradle.properties" >nul 2>&1
if errorlevel 1 (
    echo [WARN] RELEASE_STORE_FILE is not set in %GRADLE_USER_HOME%\gradle.properties
    echo [WARN] This APK will be UNSIGNED and cannot be installed.
)
exit /b 0


:init_env
if exist "gradlew.bat" goto :ie_gradle_ok
echo [ERROR] gradlew.bat not found.
echo         Put apk_build.bat in the repository root and run it again.
exit /b 1

:ie_gradle_ok
if exist "%JDK21_HOME%\bin\java.exe" set "JAVA_HOME=%JDK21_HOME%"
if not defined JAVA_HOME goto :ie_no_java
if not exist "%JAVA_HOME%\bin\java.exe" goto :ie_no_java

"%JAVA_HOME%\bin\java.exe" -XshowSettings:properties -version 2>&1 | findstr /C:"java.specification.version = 21" >nul
if errorlevel 1 goto :ie_bad_java

if defined ANDROID_HOME goto :ie_sdk_ok
if defined ANDROID_SDK_ROOT set "ANDROID_HOME=%ANDROID_SDK_ROOT%"
if defined ANDROID_HOME goto :ie_sdk_ok
if exist "D:\devtools\android-sdk" set "ANDROID_HOME=D:\devtools\android-sdk"

:ie_sdk_ok
if not defined ANDROID_HOME if not exist "local.properties" (
    echo [WARN] ANDROID_HOME is not set and local.properties is missing.
    echo [WARN] Gradle may fail to locate the Android SDK.
)

if not defined GRADLE_USER_HOME set "GRADLE_USER_HOME=%USERPROFILE%\.gradle"

echo.
echo   JAVA_HOME        = %JAVA_HOME%
echo   GRADLE_USER_HOME = %GRADLE_USER_HOME%
if defined ANDROID_HOME echo   ANDROID_HOME     = %ANDROID_HOME%
echo.
exit /b 0

:ie_no_java
echo [ERROR] JDK 21 not found.
echo         Set JAVA_HOME to a JDK 21, or edit JDK21_HOME at the top of this script.
exit /b 1

:ie_bad_java
echo [ERROR] This project requires JDK 21, but JAVA_HOME points to:
echo         %JAVA_HOME%
echo         Edit JDK21_HOME at the top of this script.
exit /b 1


rem ============================================================
rem  End
rem ============================================================
:fail
echo.
echo Build aborted.
if "%MENU_MODE%"=="1" pause
exit /b 1

:done
if "%SHARE%"=="1" call :start_share
echo.
if "%MENU_MODE%"=="1" (
    if "%SHARE%"=="0" if defined OUT_APK explorer /select,"!OUT_APK!"
    pause
)
exit /b 0