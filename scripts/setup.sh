#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS_DIR="${ROOT_DIR}/.deps"
INTELLIKIT_DIR="${DEPS_DIR}/intellikit"
INTELLIKIT_REPO="https://github.com/AMDResearch/intellikit.git"
INTELLIKIT_COMMIT="0cb3a54f1327e1fc22e875c9ba52efe43e790f64"
INTELLIKIT_PATCH="${ROOT_DIR}/patch/kerncap-workspace-json.diff"
PYTHON="python3"
export PIP_DISABLE_PIP_VERSION_CHECK=1

die() {
    echo "error: $*" >&2
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

if [[ "$#" -ne 0 ]]; then
    die "setup.sh does not take arguments"
fi

require_cmd git
require_cmd "${PYTHON}"
"${PYTHON}" -m pip --version >/dev/null

[[ -f "${INTELLIKIT_PATCH}" ]] || die "missing patch: ${INTELLIKIT_PATCH}"

echo "Setting up IntelliKit kerncap at pinned commit ${INTELLIKIT_COMMIT:0:7}..."

mkdir -p "${DEPS_DIR}"

if [[ -e "${INTELLIKIT_DIR}" && ! -d "${INTELLIKIT_DIR}/.git" ]]; then
    die "${INTELLIKIT_DIR} exists but is not a git checkout; remove it and rerun setup"
fi

if [[ ! -d "${INTELLIKIT_DIR}/.git" ]]; then
    git clone --quiet "${INTELLIKIT_REPO}" "${INTELLIKIT_DIR}"
fi

origin_url="$(git -C "${INTELLIKIT_DIR}" config --get remote.origin.url || true)"
case "${origin_url}" in
    *AMDResearch/intellikit*) ;;
    *) die "${INTELLIKIT_DIR} is not an AMDResearch/intellikit checkout" ;;
esac

git -C "${INTELLIKIT_DIR}" fetch --quiet origin
git -C "${INTELLIKIT_DIR}" checkout --quiet --detach "${INTELLIKIT_COMMIT}"
git -C "${INTELLIKIT_DIR}" reset --hard --quiet "${INTELLIKIT_COMMIT}"
git -C "${INTELLIKIT_DIR}" clean -fdq

echo "Applying patch/kerncap-workspace-json.diff..."
git -C "${INTELLIKIT_DIR}" apply --quiet --unidiff-zero "${INTELLIKIT_PATCH}"

echo "Reinstalling kerncap and kerncap-plus..."
for package in kerncap kerncap-plus; do
    if "${PYTHON}" -m pip show "${package}" >/dev/null 2>&1; then
        "${PYTHON}" -m pip uninstall -y -q "${package}"
    fi
done

"${PYTHON}" -m pip install -q -e "${INTELLIKIT_DIR}/kerncap"
"${PYTHON}" -m pip install -q -e "${ROOT_DIR}"

echo "Verifying installation..."
"${PYTHON}" -c "import kerncap, kerncap_plus"
kerncap --help >/dev/null
kerncap-plus --help >/dev/null

echo "setup complete"
