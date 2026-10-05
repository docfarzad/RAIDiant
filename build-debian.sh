#!/usr/bin/env bash
# Run from the copied project on Debian: bash build-debian.sh
# Installs build dependencies, builds the app and .deb, but does not install RAIDiant.
set -Eeuo pipefail
umask 022

if [[ $# -ne 0 ]]; then
    printf 'Usage: bash build-debian.sh\n' >&2
    exit 2
fi
if [[ $(uname -s) != Linux || ! -f /etc/debian_version ]]; then
    printf 'Run this script on Debian 12 or newer, on the target CPU architecture.\n' >&2
    exit 1
fi

raid_project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd -- "$raid_project"
for raid_required in main.py RAIDiant.spec requirements-build.txt pyproject.toml; do
    if [[ ! -f $raid_required ]]; then
        printf 'Missing project file: %s. Copy the whole RAIDiant project first.\n' "$raid_required" >&2
        exit 1
    fi
done

raid_sudo=()
if (( EUID != 0 )); then
    if ! command -v sudo >/dev/null; then
        printf 'sudo is required to install system build dependencies.\n' >&2
        exit 1
    fi
    raid_sudo=(sudo)
fi

printf '\nInstalling Debian build dependencies...\n'
"${raid_sudo[@]}" apt-get update
"${raid_sudo[@]}" apt-get install -y --no-install-recommends \
    python3 python3-venv python3-tk binutils ca-certificates \
    dpkg desktop-file-utils fontconfig fonts-dejavu-core libxcb1 hicolor-icon-theme

# These pinned wheels and PyInstaller support Python 3.11 through 3.13.
# Check before creating/reusing a build environment on a newer Debian release.
/usr/bin/python3 - <<'PY'
import sys
if not (3, 11) <= sys.version_info[:2] <= (3, 13):
    raise SystemExit('This pinned build requires Debian Python 3.11–3.13 (Debian 12 or 13).')
PY
raid_arch=$(dpkg --print-architecture)
case "$raid_arch" in
    amd64|arm64) ;;
    *) printf 'This pinned build supports amd64 and arm64, not %s.\n' "$raid_arch" >&2; exit 1 ;;
esac

# A dedicated environment avoids Debian's externally managed system Python.
# Keep it after building so later builds can reuse downloaded dependencies.
raid_venv="$raid_project/.venv-debian"
/usr/bin/python3 -m venv "$raid_venv"
raid_python="$raid_venv/bin/python"
"$raid_python" -c 'import tkinter; import venv'
"$raid_python" -m pip install -r "$raid_project/requirements-build.txt"

raid_version=$("$raid_python" - <<'PY'
from pathlib import Path
import tomllib
print(tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])
PY
)
raid_package_version="${raid_version}-1"
dpkg --validate-version "$raid_package_version"
raid_glibc=$(getconf GNU_LIBC_VERSION)
raid_glibc=${raid_glibc##* }
raid_xcb=$(dpkg-query -W -f='${Version}' "libxcb1:$raid_arch")

mkdir -p -- "$raid_project/build" "$raid_project/dist"
raid_work=$(mktemp -d "$raid_project/build/deb-package.XXXXXXXX")
cleanup() {
    # Only this invocation's randomly named staging directory is removed.
    case "$raid_work" in
        "$raid_project"/build/deb-package.*) rm -rf -- "$raid_work" ;;
    esac
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf '\nBuilding RAIDiant %s for %s...\n' "$raid_version" "$raid_arch"
"$raid_python" -m PyInstaller --clean --noconfirm \
    --distpath "$raid_work/output" --workpath "$raid_work/pyinstaller" \
    "$raid_project/RAIDiant.spec"

raid_stage="$raid_work/package"
install -Dm755 "$raid_work/output/RAIDiant" "$raid_stage/opt/raidiant/RAIDiant"
mkdir -p -- "$raid_stage/usr/bin" "$raid_stage/DEBIAN"
ln -s /opt/raidiant/RAIDiant "$raid_stage/usr/bin/RAIDiant"
install -Dm644 "$raid_project/raidiant/assets/RAIDiant.desktop" \
    "$raid_stage/usr/share/applications/raidiant.desktop"
desktop-file-validate "$raid_stage/usr/share/applications/raidiant.desktop"
for raid_size in 16 24 32 48 64 128 256 512 1024; do
    install -Dm644 "$raid_project/raidiant/assets/app-icon-$raid_size.png" \
        "$raid_stage/usr/share/icons/hicolor/${raid_size}x${raid_size}/apps/raidiant.png"
done

mkdir -p -- "$raid_stage/usr/share/doc/raidiant"
cat > "$raid_stage/usr/share/doc/raidiant/README.Debian" <<EOF
RAIDiant $raid_version

Launch RAIDiant from your Applications menu or run RAIDiant in a terminal.
The executable is installed at /opt/raidiant/RAIDiant.
Python is bundled; no Python installation is needed to run this package.
An X11 or XWayland desktop session is required for the Tk interface.

Built natively for $raid_arch with glibc $raid_glibc.
Build on Debian 12 when targeting Debian 12 and newer systems.
Removing the package does not remove your member files or user settings.
EOF

raid_installed_size=$(du -sk "$raid_stage" | cut -f1)
cat > "$raid_stage/DEBIAN/control" <<EOF
Package: raidiant
Version: $raid_package_version
Section: utils
Priority: optional
Architecture: $raid_arch
Maintainer: RAIDiant local package <noreply@localhost>
Installed-Size: $raid_installed_size
Depends: libc6 (>= $raid_glibc), libxcb1 (>= $raid_xcb), fontconfig, fonts-dejavu-core, hicolor-icon-theme
Description: File-backed redundant storage manager
 Manage portable multi-parity arrays stored in member files, with a desktop
 file manager, recovery tools, and optional FTP access.
 Python and application dependencies are bundled with the executable.
EOF

# Include payload checksums for package verification; symlinks are package metadata.
(
    cd -- "$raid_stage"
    find opt usr -type f -print0 | LC_ALL=C sort -z | xargs -0 md5sum > DEBIAN/md5sums
)
raid_name="raidiant_${raid_package_version}_${raid_arch}.deb"
printf '\nPreparing %s...\n' "$raid_name"
dpkg-deb --root-owner-group -Zxz --build "$raid_stage" "$raid_work/$raid_name"
dpkg-deb --info "$raid_work/$raid_name"

# Publish only after packaging succeeds; preserve previous outputs on build failure.
install -m755 "$raid_work/output/RAIDiant" "$raid_project/dist/RAIDiant"
install -m644 "$raid_work/output/RAIDiant.png" "$raid_project/dist/RAIDiant.png"
install -m644 "$raid_work/output/RAIDiant.desktop" "$raid_project/dist/RAIDiant.desktop"
mv -f -- "$raid_work/$raid_name" "$raid_project/dist/$raid_name"
printf '\nReady: %s\n' "$raid_project/dist/$raid_name"
printf 'The app has not been installed. To install it and add it to Applications:\n'
printf '  sudo apt install %q\n' "$raid_project/dist/$raid_name"
