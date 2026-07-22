#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

TAG_PREFIX="v"
PYPI_PACKAGE="abx-plugins"
source_optional_env() {
    if [[ -f "${REPO_DIR}/.env" ]]; then
        set -a
        # shellcheck disable=SC1091
        source "${REPO_DIR}/.env"
        set +a
    fi
}

repo_slug() {
    gh repo view --json nameWithOwner --jq .nameWithOwner
}

current_version() {
    uv run python - <<'PY'
from pathlib import Path
import re

match = re.search(r'^version = "([^"]+)"$', Path('pyproject.toml').read_text(), re.MULTILINE)
if not match:
    raise SystemExit('Failed to find version in pyproject.toml')
print(match.group(1))
PY
}

compare_versions() {
    uv run python - "$1" "$2" <<'PY'
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
    local pypi_versions github_versions versions
    pypi_versions="$(curl -fsSL "https://pypi.org/pypi/${PYPI_PACKAGE}/json" | jq -r '.releases | keys[]')"
    github_versions="$(gh api "repos/${slug}/releases?per_page=100" --jq '.[].tag_name' | sed "s/^${TAG_PREFIX}//")"
    versions="$(printf '%s\n%s\n' "${pypi_versions}" "${github_versions}" | sort -u)"
    RELEASE_VERSIONS="${versions}" uv run python - <<'PY'
import os
import re

def parse(version):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-?rc(\d+))?', version)
    if not match:
        return -1, -1, -1, -1, -1
    major, minor, patch, rc = match.groups()
    return int(major), int(minor), int(patch), 0 if rc is not None else 1, int(rc or 0)

versions = [line for line in os.environ['RELEASE_VERSIONS'].splitlines() if parse(line)[0] >= 0]
print(max(versions, key=parse) if versions else '')
PY
}

pypi_has_version() {
    curl -fsSL "https://pypi.org/pypi/${PYPI_PACKAGE}/json" \
        | jq -e --arg version "$1" '.releases[$version] | length > 0' >/dev/null
}

tag_target() {
    local tag="$1"
    local target
    target="$(git ls-remote origin "refs/tags/${tag}^{}" | awk 'NR == 1 {print $1}')"
    if [[ -z "${target}" ]]; then
        target="$(git ls-remote origin "refs/tags/${tag}" | awk 'NR == 1 {print $1}')"
    fi
    printf '%s\n' "${target}"
}

github_release_has_version() {
    gh release view "${TAG_PREFIX}$1" --repo "$2" >/dev/null 2>&1
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
    [[ "$(git rev-parse HEAD)" == "${sha}" ]] || { echo "HEAD does not match RELEASE_SHA ${sha}" >&2; return 1; }
    [[ -z "$(git status --short)" ]] || { echo "Refusing to release from a dirty worktree" >&2; return 1; }
    git fetch --quiet --no-tags origin "+refs/heads/${branch}:refs/remotes/origin/${branch}"
    git merge-base --is-ancestor "${sha}" "refs/remotes/origin/${branch}" || { echo "${sha} is not on ${branch}" >&2; return 1; }
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
    uv publish --trusted-publishing always "${artifacts[@]}"
)

create_release() {
    local slug="$1" version="$2" sha="$3"
    local release_args=()
    if github_release_has_version "${version}" "${slug}"; then
        verify_existing_tag "${version}" "${sha}"
        return 0
    fi
    verify_existing_tag "${version}" "${sha}"
    if [[ "${version}" =~ rc[0-9]+$ ]]; then
        release_args+=(--prerelease)
    fi
    gh release create "${TAG_PREFIX}${version}" --repo "${slug}" --target "${sha}" \
        --title "${TAG_PREFIX}${version}" --generate-notes "${release_args[@]}"
}

main() {
    local slug version latest relation release_sha target artifact_dir pypi_exists=false github_exists=false
    source_optional_env
    slug="$(repo_slug)"
    version="$(current_version)"
    release_sha="${RELEASE_SHA:-$(git rev-parse HEAD)}"
    artifact_dir="${1:-}"

    [[ -n "${artifact_dir}" && -d "${artifact_dir}" ]] || { echo "Usage: $0 TESTED_ARTIFACT_DIR" >&2; return 1; }

    require_clean_exact_checkout "${release_sha}"

    latest="$(latest_published_version "${slug}")"
    if [[ -n "${latest}" ]]; then
        relation="$(compare_versions "${version}" "${latest}")"
        if [[ "${relation}" == "lt" ]]; then
            echo "Source version ${version} is behind published version ${latest}" >&2
            return 1
        fi
    fi

    target="$(tag_target "${TAG_PREFIX}${version}")"
    pypi_has_version "${version}" && pypi_exists=true
    github_release_has_version "${version}" "${slug}" && github_exists=true
    if [[ "${pypi_exists}" == true && "${github_exists}" == true ]]; then
        [[ -n "${target}" ]] || { echo "Fully published ${version} is missing tag ${TAG_PREFIX}${version}" >&2; return 1; }
        git merge-base --is-ancestor "${target}" "refs/remotes/origin/${RELEASE_BRANCH:-main}" || {
            echo "Fully published tag ${TAG_PREFIX}${version} is not on ${RELEASE_BRANCH:-main}" >&2
            return 1
        }
    fi
    if [[ "${github_exists}" == true && "${target}" != "${release_sha}" ]]; then
        echo "Cannot recover partial release ${version}: no tag anchors it to ${release_sha}" >&2
        return 1
    fi
    if [[ "${pypi_exists}" == true && -n "${target}" && "${target}" != "${release_sha}" ]]; then
        echo "Cannot recover partial release ${version}: tag does not point to ${release_sha}" >&2
        return 1
    fi

    if [[ "${pypi_exists}" != true ]]; then
        publish_to_pypi "${version}" "${artifact_dir}"
    fi
    create_release "${slug}" "${version}" "${release_sha}"
    gh release upload "${TAG_PREFIX}${version}" --repo "${slug}" \
        "${artifact_dir}"/abx_plugins-*.whl "${artifact_dir}"/abx_plugins-*.tar.gz "${artifact_dir}"/SHA256SUMS --clobber
    echo "Released ${PYPI_PACKAGE} ${version} from ${release_sha}"
}

main "$@"
