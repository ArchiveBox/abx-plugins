#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

TAG_PREFIX="v"
PYPI_PACKAGE="abx-plugins"
VERIFY_DIR_TO_CLEAN=""

cleanup_verify_dir() {
    if [[ -n "${VERIFY_DIR_TO_CLEAN}" ]]; then
        VERIFY_DIR_TO_CLEAN="${VERIFY_DIR_TO_CLEAN}" "${UV_BINARY}" run --no-project python - <<'PY'
import os
import shutil

shutil.rmtree(os.environ["VERIFY_DIR_TO_CLEAN"])
PY
    fi
}

trap cleanup_verify_dir EXIT

require_release_binaries() {
    local key value expected_bin
    expected_bin="${ABXPKG_LIB_DIR:?ABXPKG_LIB_DIR is required}/env/bin"
    for key in GH_BINARY GIT_BINARY CURL_BINARY JQ_BINARY UV_BINARY; do
        value="${!key:-}"
        [[ -n "${value}" && -x "${value}" && "${value%/*}" == "${expected_bin}" ]] || {
            echo "${key} must be an executable projected through ${expected_bin}" >&2
            return 1
        }
    done
}

source_optional_env() {
    if [[ -f "${REPO_DIR}/.env" ]]; then
        set -a
        # shellcheck disable=SC1091
        source "${REPO_DIR}/.env"
        set +a
    fi
}

repo_slug() {
    "${GH_BINARY}" repo view --json nameWithOwner --jq .nameWithOwner
}

current_version() {
    "${UV_BINARY}" run --no-project python - <<'PY'
from pathlib import Path
import re

match = re.search(r'^version = "([^"]+)"$', Path('pyproject.toml').read_text(), re.MULTILINE)
if not match:
    raise SystemExit('Failed to find version in pyproject.toml')
print(match.group(1))
PY
}

compare_versions() {
    "${UV_BINARY}" run --no-project python - "$1" "$2" <<'PY'
import re
import sys

def parse(version):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-?rc(\d+))?', version)
    if not match:
        raise SystemExit(f'Unsupported version format: {version}')
    major, minor, patch, rc = match.groups()
    return int(major), int(minor), int(patch), 0 if rc is not None else 1, int(rc or 0)

left, right = map(parse, sys.argv[1:3])
print('gt' if left > right else 'eq' if left == right else 'lt')
PY
}

latest_published_version() {
    local slug="$1"
    local pypi_versions github_tags
    pypi_versions="$("${CURL_BINARY}" -fsSL "https://pypi.org/pypi/${PYPI_PACKAGE}/json" | "${JQ_BINARY}" -r '.releases | keys[]')"
    github_tags="$("${GH_BINARY}" api "repos/${slug}/releases?per_page=100" --jq '.[].tag_name')"
    PYPI_VERSIONS="${pypi_versions}" GITHUB_TAGS="${github_tags}" TAG_PREFIX="${TAG_PREFIX}" "${UV_BINARY}" run --no-project python - <<'PY'
import os
import re

def parse(version):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-?rc(\d+))?', version)
    if not match:
        return -1, -1, -1, -1, -1
    major, minor, patch, rc = match.groups()
    return int(major), int(minor), int(patch), 0 if rc is not None else 1, int(rc or 0)

versions = set(os.environ['PYPI_VERSIONS'].splitlines())
versions.update(
    tag.removeprefix(os.environ['TAG_PREFIX'])
    for tag in os.environ['GITHUB_TAGS'].splitlines()
)
versions = [version for version in versions if parse(version)[0] >= 0]
print(max(versions, key=parse) if versions else '')
PY
}

pypi_has_version() {
    # shellcheck disable=SC2016
    "${CURL_BINARY}" -fsSL "https://pypi.org/pypi/${PYPI_PACKAGE}/json" \
        | "${JQ_BINARY}" -e --arg version "$1" '.releases[$version] | length > 0' >/dev/null
}

tag_target() {
    local tag="$1" output target
    output="$("${GIT_BINARY}" ls-remote origin "refs/tags/${tag}^{}")"
    target="${output%%[[:space:]]*}"
    if [[ -z "${target}" ]]; then
        output="$("${GIT_BINARY}" ls-remote origin "refs/tags/${tag}")"
        target="${output%%[[:space:]]*}"
    fi
    printf '%s\n' "${target}"
}

github_release_has_version() {
    "${GH_BINARY}" release view "${TAG_PREFIX}$1" --repo "$2" >/dev/null 2>&1
}

github_release_has_assets() {
    local version="$1" slug="$2" assets_json verify_dir
    assets_json="$("${GH_BINARY}" release view "${TAG_PREFIX}${version}" --repo "${slug}" --json assets)"
    verify_dir="$("${UV_BINARY}" run --no-project python -c 'import tempfile; print(tempfile.mkdtemp())')"
    VERIFY_DIR_TO_CLEAN="${verify_dir}"
    "${GH_BINARY}" release download "${TAG_PREFIX}${version}" --repo "${slug}" --pattern SHA256SUMS --dir "${verify_dir}"
    ASSETS_JSON="${assets_json}" VERIFY_DIR="${verify_dir}" EXPECTED_VERSION="${version}" "${UV_BINARY}" run --no-project python - <<'PY'
import json
import os
import re
from pathlib import Path

version = os.environ["EXPECTED_VERSION"]
expected_names = {
    f"abx_plugins-{version}-py3-none-any.whl",
    f"abx_plugins-{version}.tar.gz",
    "SHA256SUMS",
}
assets = json.loads(os.environ["ASSETS_JSON"])["assets"]
if {asset["name"] for asset in assets} != expected_names:
    raise SystemExit("Published release asset set is incomplete or contains extras")
published = {asset["name"]: asset.get("digest", "") for asset in assets}
lines = (Path(os.environ["VERIFY_DIR"]) / "SHA256SUMS").read_text().splitlines()
manifest = {}
for line in lines:
    digest, filename = line.split(maxsplit=1)
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or Path(filename).name != filename:
        raise SystemExit(f"Invalid checksum entry: {line}")
    if filename in manifest:
        raise SystemExit(f"Duplicate checksum entry: {filename}")
    manifest[filename] = digest
artifact_names = expected_names - {"SHA256SUMS"}
if set(manifest) != artifact_names:
    raise SystemExit("Published checksum manifest does not name exactly the wheel and sdist")
for filename, digest in manifest.items():
    if published[filename] != f"sha256:{digest}":
        raise SystemExit(f"Published digest mismatch for {filename}")
PY
    cleanup_verify_dir
    VERIFY_DIR_TO_CLEAN=""
}

verify_existing_tag() {
    local tag="${TAG_PREFIX}$1"
    local sha="$2"
    local target
    target="$(tag_target "${tag}")"
    if [[ -n "${target}" && "${target}" != "${sha}" ]]; then
        echo "Tag ${tag} points to ${target}, not release SHA ${sha}" >&2
        return 1
    fi
}

require_clean_exact_checkout() {
    local sha="$1" branch="${RELEASE_BRANCH:-main}"
    [[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || { echo "RELEASE_SHA must be a full commit SHA" >&2; return 1; }
    [[ "$("${GIT_BINARY}" rev-parse HEAD)" == "${sha}" ]] || { echo "HEAD does not match RELEASE_SHA ${sha}" >&2; return 1; }
    [[ -z "$("${GIT_BINARY}" status --short)" ]] || { echo "Refusing to release from a dirty worktree" >&2; return 1; }
    "${GIT_BINARY}" fetch --quiet --no-tags origin "+refs/heads/${branch}:refs/remotes/origin/${branch}"
    "${GIT_BINARY}" merge-base --is-ancestor "${sha}" "refs/remotes/origin/${branch}" || { echo "${sha} is not on ${branch}" >&2; return 1; }
}

publish_to_pypi() (
    local version="$1"
    local build_dir="$2"
    shopt -s nullglob
    local artifacts=("${build_dir}"/*)
    local wheels=("${build_dir}"/abx_plugins-"${version}"-*.whl)
    local sdists=("${build_dir}"/abx_plugins-"${version}".tar.gz)
    [[ "${#wheels[@]}" -eq 1 && "${#sdists[@]}" -eq 1 && -f "${sdists[0]}" ]] || {
        echo "Expected one tested wheel and sdist for ${version} in ${build_dir}" >&2
        return 1
    }
    artifacts=("${wheels[@]}" "${sdists[@]}")
    "${UV_BINARY}" publish --trusted-publishing always "${artifacts[@]}"
)

create_release() {
    local slug="$1" version="$2" sha="$3"
    local release_args=()
    if github_release_has_version "${version}" "${slug}"; then
        verify_existing_tag "${version}" "${sha}"
        return 0
    fi
    if [[ "${version}" =~ rc[0-9]+$ ]]; then
        release_args+=(--prerelease)
    fi
    "${GH_BINARY}" release create "${TAG_PREFIX}${version}" --repo "${slug}" --verify-tag \
        --title "${TAG_PREFIX}${version}" --generate-notes "${release_args[@]}"
}

create_release_tag() {
    local version="$1" sha="$2" tag="${TAG_PREFIX}$1"
    verify_existing_tag "${version}" "${sha}"
    if [[ -n "$(tag_target "${tag}")" ]]; then
        return 0
    fi
    "${GIT_BINARY}" tag "${tag}" "${sha}"
    "${GIT_BINARY}" push origin "refs/tags/${tag}:refs/tags/${tag}"
    [[ "$(tag_target "${tag}")" == "${sha}" ]] || {
        echo "Tag ${tag} was not published at release SHA ${sha}" >&2
        return 1
    }
}

main() {
    local slug version latest relation release_sha target artifact_dir pypi_exists=false github_exists=false github_complete=false
    source_optional_env
    require_release_binaries
    slug="$(repo_slug)"
    version="$(current_version)"
    release_sha="${RELEASE_SHA:-$("${GIT_BINARY}" rev-parse HEAD)}"
    artifact_dir="${1:-}"

    require_clean_exact_checkout "${release_sha}"

    target="$(tag_target "${TAG_PREFIX}${version}")"
    pypi_has_version "${version}" && pypi_exists=true
    github_release_has_version "${version}" "${slug}" && github_exists=true
    if [[ "${github_exists}" == true ]] && github_release_has_assets "${version}" "${slug}"; then
        github_complete=true
    fi
    if [[ "${pypi_exists}" == true && "${github_complete}" == true ]]; then
        [[ -n "${target}" ]] || { echo "Fully published ${version} is missing tag ${TAG_PREFIX}${version}" >&2; return 1; }
        "${GIT_BINARY}" merge-base --is-ancestor "${target}" "refs/remotes/origin/${RELEASE_BRANCH:-main}" || {
            echo "Fully published tag ${TAG_PREFIX}${version} is not on ${RELEASE_BRANCH:-main}" >&2
            return 1
        }
        echo "${PYPI_PACKAGE} ${version} is already fully released from ${target}"
        return 0
    fi
    latest="$(latest_published_version "${slug}")"
    if [[ -n "${latest}" ]]; then
        relation="$(compare_versions "${version}" "${latest}")"
        if [[ "${relation}" == "lt" ]]; then
            echo "Source version ${version} is behind published version ${latest}" >&2
            return 1
        fi
    fi
    if [[ ( "${pypi_exists}" == true || "${github_exists}" == true ) && "${target}" != "${release_sha}" ]]; then
        echo "Cannot recover partial release ${version}: no tag anchors it to ${release_sha}" >&2
        return 1
    fi

    [[ -n "${artifact_dir}" && -d "${artifact_dir}" ]] || { echo "Usage: $0 TESTED_ARTIFACT_DIR" >&2; return 1; }
    create_release_tag "${version}" "${release_sha}"
    if [[ "${pypi_exists}" != true ]]; then
        publish_to_pypi "${version}" "${artifact_dir}"
    fi
    create_release "${slug}" "${version}" "${release_sha}"
    "${GH_BINARY}" release upload "${TAG_PREFIX}${version}" --repo "${slug}" \
        "${artifact_dir}"/abx_plugins-*.whl "${artifact_dir}"/abx_plugins-*.tar.gz "${artifact_dir}"/SHA256SUMS --clobber
    echo "Released ${PYPI_PACKAGE} ${version} from ${release_sha}"
}

main "$@"
