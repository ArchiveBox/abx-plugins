#!/usr/bin/env bash
set -euo pipefail

version="${CHROME_VENDOR_VERSION:?}"
checksum="${CHROME_VENDOR_SHA256:?}"
archive_dir="$RUNNER_TEMP/chrome-vendor-archive"
filename="$version-chrome-linux64.zip"
archive="$archive_dir/$filename"
cache="$RUNNER_TEMP/runtime-abx-lib/puppeteer/cache/chrome"
browser="$cache/linux-$version/chrome-linux64/chrome"

case "${1:?}" in
  check)
    # Chrome for Testing includes a trailing space in --version output.
    read -r actual <<< "$("$browser" --version 2>/dev/null || true)"
    if [[ "${2:-}" == --always || "$actual" != "Google Chrome for Testing $version" ]]; then
      echo 'needed=true' >> "$GITHUB_OUTPUT"
    fi
    ;;
  download|stage)
    jq -e --arg version "$version" '
      any(.required_binaries[]; .name == "{CHROME_BINARY}"
        and .min_version == $version and .max_version == $version
        and .overrides.puppeteer.install_args == ["chrome@" + $version, "--install-deps"])
    ' abx_plugins/plugins/chrome/config.json >/dev/null
    if [[ "$1" == download ]]; then
      mkdir -p "$archive_dir"
      temporary="$(mktemp "$archive_dir/download.XXXXXX")"
      trap 'rm -f -- "$temporary"' EXIT
      curl --fail --location --silent --show-error --max-time 240 \
        "https://storage.googleapis.com/chrome-for-testing-public/$version/linux64/chrome-linux64.zip?generation=${CHROME_VENDOR_GENERATION:?}" \
        --output "$temporary"
      printf '%s  %s\n' "$checksum" "$temporary" | sha256sum --check --strict
      mv "$temporary" "$archive"
    else
      printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --strict
      mkdir -p "$cache"
      # Puppeteer owns extraction and dependency installation. The timeout
      # test can also use this verified source for its separate setup cache.
      ln -f "$archive" "$cache/$filename"
      printf 'CHROME_VENDOR_ARCHIVE=%s\n' "$archive" >> "$GITHUB_ENV"
    fi
    ;;
  *)
    exit 2
    ;;
esac
