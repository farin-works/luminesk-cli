#!/bin/sh
# Official onedir installer. No Python interpreter is needed on the host.
set -eu
umask 077
RESET=''; PRIMARY=''; GREEN=''; DIM=''
if [ -t 1 ] && [ "${TERM:-}" != dumb ] && [ -z "${NO_COLOR:-}" ]; then
    ESC=$(printf '\033')
    RESET="${ESC}[0m"; PRIMARY="${ESC}[1;35m"; GREEN="${ESC}[32m"; DIM="${ESC}[2m"
fi
heading() { printf '\n%s%s%s\n' "$PRIMARY" "$1" "$RESET"; }
step() { printf '%s  >%s %s\n' "$PRIMARY" "$RESET" "$1"; }
success() { printf '%s  OK%s %s\n' "$GREEN" "$RESET" "$1"; }
detail() { printf '  %s%-15s%s: %s\n' "$DIM" "$1" "$RESET" "$2"; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
YES=false; DELETE=false
for arg in "$@"; do
    case "$arg" in -y|--yes) YES=true ;; -d|--delete) DELETE=true ;; *) fail "Unknown option: $arg" ;; esac
done
confirm() {
    [ "$YES" = true ] && return 0
    [ -t 0 ] || fail 'Use --yes when piping this installer.'
    printf '%s [y/N]: ' "$1"
    read -r answer
    case "$answer" in y|Y|yes|YES) return 0 ;; *) echo 'Cancelled.'; exit 0 ;; esac
}
meta() { if [ -f "$META" ]; then sed -n "s/^$1=//p" "$META"; fi; }
# Refuse links in every existing ancestor, including the installation root.
no_links() {
    cursor=$1
    while [ "$cursor" != / ] && [ "$cursor" != . ]; do
        [ ! -L "$cursor" ] || fail "Symbolic link in installation path: $cursor"
        cursor=$(dirname "$cursor")
    done
}
heading 'Luminesk installer'
step 'Checking system and existing installation...'
case "$(uname -s)" in Linux) OS=linux ;; Darwin) OS=macos ;; *) fail 'Unsupported OS' ;; esac
case "$(uname -m)" in x86_64|amd64) ARCH=amd64 ;; arm64|aarch64) ARCH=arm64 ;; *) fail 'Unsupported architecture' ;; esac
BIN="$HOME/.local/bin"
STATE="$HOME/.local/share/luminesk"
if [ "$(id -u)" = 0 ]; then BIN=/usr/local/bin; STATE=/usr/local/share/luminesk; fi
TARGET="$BIN/nesk"; META="$STATE/.metadata_one_line"; ROOT="$STATE/app"
no_links "$BIN"; no_links "$STATE"; no_links "$ROOT"
mkdir -p "$BIN" "$STATE" "$ROOT"
if [ "$(id -u)" = 0 ]; then chmod 755 "$BIN" "$STATE" "$ROOT"; fi
LOCK="$ROOT/install.lock"
mkdir "$LOCK" 2>/dev/null || fail "Another installation is active, or was interrupted. After checking no installer is running, remove $LOCK and retry."
WORK=''; NEXT=''; ROLLBACK=false
cleanup() {
    if [ "$ROLLBACK" = true ]; then
        printf '%s\n' 'Restoring the previous installation...' >&2
        if [ -e "$WORK/previous-entry" ] || [ -L "$WORK/previous-entry" ]; then
            mv -f "$WORK/previous-entry" "$TARGET" || { echo "Rollback failed; recovery files retained at $WORK" >&2; return; }
        else
            rm -f "$TARGET"
        fi
        if [ -f "$WORK/previous-meta" ]; then mv -f "$WORK/previous-meta" "$META"; else rm -f "$META"; fi
    fi
    [ -z "$NEXT" ] || rm -f "$NEXT"
    [ -z "$WORK" ] || rm -rf "$WORK"
    rmdir "$LOCK" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
WORK=$(mktemp -d "$ROOT/.stage.XXXXXXXX")
OWNED=false
if [ -e "$META" ]; then
    no_links "$META"
    [ "$(meta installed_via)" = one-line-script ] && [ "$(meta binary)" = "$TARGET" ] || fail 'Foreign installation metadata; resolve uv/pipx/manual installation first.'
    OWNED=true
fi
EXISTING=$(command -v nesk || true)
[ -z "$EXISTING" ] || { [ "$OWNED" = true ] && [ "$EXISTING" = "$TARGET" ]; } || fail "Foreign installation: $EXISTING (check uv tool list / pipx list)."
if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    [ "$OWNED" = true ] || fail "Refusing to overwrite unowned $TARGET"
    if [ -L "$TARGET" ]; then
        active=$(readlink "$TARGET")
        case "$active" in "$ROOT"/versions/*/nesk/nesk) ;; *) fail 'Foreign launcher target' ;; esac
        version_id=${active#"$ROOT/versions/"}; version_id=${version_id%/nesk/nesk}
        case "$version_id" in ''|*[!A-Za-z0-9.-]*|.*) fail 'Invalid active version' ;; esac
        no_links "${active%/nesk}"
        [ -f "${active%/nesk/nesk}/.owned" ] || fail 'Unowned active version'
    else
        [ "$(meta layout)" != onedir ] || fail 'Managed launcher was replaced by a foreign file.'
    fi
fi
if [ "$DELETE" = true ]; then
    [ "$OWNED" = true ] || { echo 'Luminesk is not installed.'; exit 0; }
    confirm 'Remove the installer-owned application (keep instances and cache)?'
    step 'Checking application use and ownership...'
    command -v lsof >/dev/null || fail 'lsof is required to verify that no running process uses the application before removal.'
    # Include loaded libraries: deleting only a retained executable inode can
    # still break a running process when it lazily imports another module.
    status=0
    holders=$(lsof -t +D "$ROOT" "$TARGET" 2>&1) || status=$?
    [ "$status" -eq 1 ] && [ -z "$holders" ] || fail 'Application files may be in use. Close running nesk processes and retry removal.'
    # Validate every deletion candidate before changing the active entry point.
    if [ -d "$ROOT/versions" ]; then
        no_links "$ROOT/versions"
        for version in "$ROOT"/versions/*; do
            [ -d "$version" ] || continue
            [ ! -L "$version" ] && [ -f "$version/.owned" ] && [ ! -L "$version/.owned" ] || fail "Unowned version: $version"
            [ -f "$version/.installed-files" ] && [ ! -L "$version/.installed-files" ] || fail "Missing ownership inventory: $version"
            (cd "$version" && find . -print) | LC_ALL=C sort > "$WORK/remove-actual"
            LC_ALL=C sort "$version/.installed-files" > "$WORK/remove-expected"
            [ -z "$(comm -23 "$WORK/remove-actual" "$WORK/remove-expected")" ] || fail "Unexpected user files in $version; move them outside the application before uninstalling."
        done
    fi
    step 'Removing installer-owned application files...'
    if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then mv "$TARGET" "$WORK/removed-entry"; fi
    if [ -d "$ROOT/versions" ]; then
        no_links "$ROOT/versions"
        for version in "$ROOT"/versions/*; do
            [ -d "$version" ] || continue
            [ ! -L "$version" ] && [ -f "$version/.owned" ] && [ ! -L "$version/.owned" ] || fail "Unowned version: $version"
            # rm does not follow symlinks; scope is a marked direct child only.
            rm -rf "$version"
        done
    fi
    rm -f "$META"
    success 'Luminesk uninstalled; instances and cache were preserved.'
    exit 0
fi
for tool in curl unzip awk head sort cmp; do command -v "$tool" >/dev/null || fail "$tool is required"; done
sha() {
    if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null; then shasum -a 256 "$1" | awk '{print $1}'
    else fail 'sha256sum or shasum is required'; fi
}
# Follow redirects ourselves so every destination is checked before connecting.
fetch() {
    url=$1; destination=$2; limit=$3; redirects=0
    progress_option=--silent
    if [ "${4:-}" = progress ] && [ -t 2 ] && [ "${TERM:-}" != dumb ]; then
        progress_option=--progress-bar
    fi
    while :; do
        case "$url" in
            https://api.github.com/repos/farin-works/luminesk-cli/releases/latest|https://github.com/farin-works/luminesk-cli/releases/download/*|https://release-assets.githubusercontent.com/*) ;;
            *) fail 'Release asset has an invalid download URL' ;;
        esac
        code=$(curl --proto '=https' "$progress_option" --show-error --connect-timeout 20 --max-time 300 --max-filesize "$limit" --max-redirs 0 -D "$WORK/headers" -o "$destination" -w '%{http_code}' "$url") || fail 'Download failed (network unavailable or size limit exceeded).'
        case "$code" in
            200) [ "$(wc -c < "$destination")" -le "$limit" ] || fail 'Download too large'; return ;;
            301|302|303|307|308) url=$(sed -n 's/^[Ll]ocation: *//p' "$WORK/headers" | tr -d '\r'); redirects=$((redirects + 1)); [ "$redirects" -le 5 ] || fail 'Too many redirects' ;;
            *) fail "Download HTTP status $code" ;;
        esac
    done
}
step 'Checking the latest release...'
fetch https://api.github.com/repos/farin-works/luminesk-cli/releases/latest "$WORK/release" 4194304
TAG=$(grep -oE '"tag_name"[[:space:]]*:[[:space:]]*"v?[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[1-9][0-9]*)?"' "$WORK/release" | sed -E 's/.*"(v?[0-9][^"]*)"$/\1/')
printf '%s\n' "$TAG" | grep -Eq '^v?[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[1-9][0-9]*)?$' || fail 'Invalid release version'
[ "$(printf '%s\n' "$TAG" | wc -l)" -eq 1 ] || fail 'Duplicate release version'
VERSION=${TAG#v}; ASSET="luminesk-$OS-$ARCH.zip"
BASE="https://github.com/farin-works/luminesk-cli/releases/download/$TAG"
fetch "$BASE/SHA256SUMS" "$WORK/sums" 1048576
EXPECTED=$(awk -v name="$ASSET" '$2==name || $2=="*"name {print $1}' "$WORK/sums")
printf '%s\n' "$EXPECTED" | grep -Eq '^[0-9a-f]{64}$' || fail 'Release asset has no valid SHA-256 digest'
[ "$(printf '%s\n' "$EXPECTED" | wc -l)" -eq 1 ] || fail 'Duplicate checksum'
INSTALLED='Not installed'; INSTALL_STATUS='New installation'
if [ -e "$TARGET" ]; then
    INSTALLED=$("$TARGET" --version) || fail 'Existing command failed; inspect installation before updating.'
    LOCAL_VERSION=${INSTALLED#Luminesk }
    if [ "$LOCAL_VERSION" = "$VERSION" ]; then
        INSTALL_STATUS='Up to date (reinstall)'
    else
        INSTALL_STATUS="Version change ($LOCAL_VERSION -> $VERSION)"
    fi
fi
heading 'Installation summary'
detail 'System' "$OS-$ARCH"
detail 'Installed' "$INSTALLED"
detail 'Latest release' "$VERSION"
detail 'Status' "$INSTALL_STATUS"
detail 'Command' "$TARGET"
printf '\n'
confirm "Install Luminesk $VERSION ($OS-$ARCH)?"
step "Downloading Luminesk $VERSION ($ASSET)..."
fetch "$BASE/$ASSET" "$WORK/bundle.zip" 536870912 progress
step 'Verifying archive checksum...'
[ "$(sha "$WORK/bundle.zip")" = "$EXPECTED" ] || fail 'Checksum verification failed'
success 'Archive checksum verified.'
step 'Extracting and verifying bundle files...'
# Never ask unzip to choose output paths or create links.
unzip -p "$WORK/bundle.zip" nesk/.bundle-manifest | head -c 4194305 > "$WORK/manifest"
[ -s "$WORK/manifest" ] && [ "$(wc -c < "$WORK/manifest")" -le 4194304 ] || fail 'Invalid bundle inventory'
awk '
function safe(p, n,a,i) { if(p !~ /^[A-Za-z0-9_,+.\/-]+$/ || p ~ /^\//) return 0; n=split(p,a,"/"); for(i=1;i<=n;i++) if(a[i]=="" || a[i]=="." || a[i]=="..") return 0; return 1 }
NF!=5 || !safe($5) || seen[$5]++ {exit 1}
$1=="F" {if(($2!="755" && $2!="644") || $3 !~ /^[0-9]+$/ || length($3)>10 || length($4)!=64 || $4 !~ /^[0-9a-f]+$/) exit 1; total+=$3; if(total>2147483648) exit 1}
$1=="D" {if($2!="755" || $3!="0" || $4!="-") exit 1}
$1=="L" {if($2!="777" || $3!="0" || !safe($4)) exit 1; links[$5]=$4}
$1!="F" && $1!="D" && $1!="L" {exit 1}
{kind[$5]=$1; if(NR>20000) exit 1}
END {if(NR==0) exit 1; for(p in kind) {parent=p; while(sub(/\/[^\/]+$/,"",parent)) if(kind[parent]!="D") exit 1} for(p in links) if(kind[links[p]]!="F" && kind[links[p]]!="D") exit 1; if(kind["nesk"]!="F" || kind["_internal"]!="D") exit 1}
' "$WORK/manifest" || fail 'Unsafe bundle inventory'
{ echo nesk/.bundle-manifest; awk '{print "nesk/"$5 ($1=="D"?"/":"")}' "$WORK/manifest"; } | LC_ALL=C sort > "$WORK/expected"
unzip -Z1 "$WORK/bundle.zip" | head -c 4194305 > "$WORK/members"
[ "$(wc -c < "$WORK/members")" -le 4194304 ] || fail 'Too many ZIP members'
LC_ALL=C sort "$WORK/members" > "$WORK/actual"
cmp -s "$WORK/expected" "$WORK/actual" || fail 'ZIP structure does not match inventory'
mkdir "$WORK/nesk"
cp "$WORK/manifest" "$WORK/nesk/.bundle-manifest"
chmod 755 "$WORK/nesk"
while read -r kind mode size digest path; do
    [ "$kind" != D ] || { mkdir -p "$WORK/nesk/$path"; chmod 755 "$WORK/nesk/$path"; }
done < "$WORK/manifest"
while read -r kind mode size digest path; do
    [ "$kind" = F ] || continue
    unzip -p "$WORK/bundle.zip" "nesk/$path" | head -c "$((size + 1))" > "$WORK/nesk/$path"
    [ "$(wc -c < "$WORK/nesk/$path")" -eq "$size" ] && [ "$(sha "$WORK/nesk/$path")" = "$digest" ] || fail "Damaged bundle file: $path"
    chmod "$mode" "$WORK/nesk/$path"
done < "$WORK/manifest"
while read -r kind mode size target path; do
    [ "$kind" = L ] || continue
    prefix=''; parent=$(dirname "$path")
    while [ "$parent" != . ]; do prefix="../$prefix"; parent=$(dirname "$parent"); done
    ln -s "$prefix$target" "$WORK/nesk/$path"
done < "$WORK/manifest"
step 'Checking application imports, version and help...'
"$WORK/nesk/nesk" --_bundle-check
"$WORK/nesk/nesk" --version | grep -F "$VERSION" >/dev/null || fail 'Bundle version mismatch'
"$WORK/nesk/nesk" --help >/dev/null
step 'Activating the verified application...'
no_links "$ROOT/versions"; mkdir -p "$ROOT/versions"; chmod 755 "$ROOT/versions"
DEST="$ROOT/versions/$VERSION-$(basename "$WORK" | cut -d. -f3)"
[ ! -e "$DEST" ] || fail 'Version directory already exists'
mkdir "$DEST"; chmod 755 "$DEST"; printf '%s\n' one-line-script > "$DEST/.owned"
mv "$WORK/nesk" "$DEST/nesk"
"$DEST/nesk/nesk" --_bundle-check
# Metadata can precede activation: it grants ownership only at the exact target.
# Preserve a legacy executable without changing its inode or the active command.
if [ -f "$TARGET" ] && [ ! -L "$TARGET" ]; then
    ln "$TARGET" "$DEST/legacy-nesk" || fail 'Cannot retain the legacy executable inode; migration requires bin and app on the same filesystem.'
fi
{
    printf '.\n./.owned\n./.installed-files\n./nesk\n./nesk/.bundle-manifest\n'
    awk '{print "./nesk/"$5}' "$WORK/manifest"
    if [ -f "$DEST/legacy-nesk" ]; then echo './legacy-nesk'; fi
} > "$DEST/.installed-files"
if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then cp -Pp "$TARGET" "$WORK/previous-entry"; fi
if [ -f "$META" ]; then cp -p "$META" "$WORK/previous-meta"; fi
ROLLBACK=true
printf 'installed_via=one-line-script\nbinary=%s\nlayout=onedir\nversion=%s\n' "$TARGET" "$VERSION" > "$WORK/meta"
# First metadata remains compatible with a legacy regular entry until switch.
if [ -f "$TARGET" ] && [ ! -L "$TARGET" ]; then sed '/^layout=/d' "$WORK/meta" > "$WORK/legacy-meta"; mv "$WORK/legacy-meta" "$META"; else mv "$WORK/meta" "$META"; fi
sync
NEXT="$BIN/.nesk-$(basename "$WORK")"
ln -s "$DEST/nesk/nesk" "$NEXT"
# Same-filesystem rename is the activation commit; never truncate the command.
mv -f "$NEXT" "$TARGET"; NEXT=''
if [ -f "$WORK/meta" ]; then mv "$WORK/meta" "$META"; fi
"$TARGET" --version | grep -F "$VERSION" >/dev/null || fail 'Activated launcher smoke test failed'
# Flush prepared files before reporting the activation as committed.
sync
ROLLBACK=false
step 'Configuring PATH...'
case ":$PATH:" in *:"$BIN":*) ;; *)
    # The parent shell cannot be modified by a piped child. Persist for new shells.
    for profile in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
        no_links "$profile"
        line='export PATH="$HOME/.local/bin:$PATH" # Luminesk installer'
        if [ "$BIN" = /usr/local/bin ]; then line='export PATH="/usr/local/bin:$PATH" # Luminesk installer'; fi
        grep -F "$line" "$profile" >/dev/null 2>&1 || printf '\n%s\n' "$line" >> "$profile"
    done
    echo 'PATH configured. Open a new terminal to run nesk.' ;;
esac
success "Luminesk $VERSION installed."
detail 'Command' "$TARGET"
printf '  Previous versions retained for running processes.\n'
heading 'Next steps'
printf '  Check the installed version: nesk --version\n'
if [ "$OS" = linux ]; then
    printf '  Install or start Docker Engine / Docker Desktop.\n'
    printf '  Docker must be accessible to the same user that runs nesk.\n'
else
    printf '  Install and start Docker Desktop before running servers.\n'
fi
printf '  Check Docker and Luminesk:\n    docker version\n    nesk doctor\n'
printf '  Explore recipes and commands:\n    nesk catalog update\n    nesk --help\n'
if [ "$OS" = linux ]; then
    heading 'Docker file permissions'
    printf '  For recipes exposing runtime_uid/runtime_gid, use your host identity:\n'
    printf '    nesk install NAME --set runtime_uid=$(id -u) --set runtime_gid=$(id -g)\n'
fi
printf '\n  Installation and Docker guide: https://nesk.farin.cc/docs/installation\n'
