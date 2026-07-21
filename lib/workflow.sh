#!/usr/bin/env bash

workflow_die() {
  printf 'ERROR: %s\n' "$*" >&2
  return 1
}

physical_pwd() {
  pwd -P
}

assert_owned_session() {
  local workflow_root="$1"
  local run_dir="$2"
  local context_sha256="$3"
  (
    cd "$workflow_root" || exit 1
    python3 -m integrations.common.session_config verify \
      --workflow-root "$workflow_root" \
      --run-dir "$run_dir" \
      --context-sha256 "$context_sha256"
  )
}

remove_managed_claude_base_url() {
  local input_file="$1"
  local output_file="$2"
  jq --indent 2 'if (.env? | type) == "object" then del(.env.ANTHROPIC_BASE_URL) else . end' \
    "$input_file" >"$output_file"
}

render_claudex_config() {
  local output_file="$1"
  local default_model="$2"
  local fast_model="$3"
  local balanced_model="$4"
  local powerful_model="$5"
  local haiku_model="$6"
  local sonnet_model="$7"
  local opus_model="$8"
  local claude_binary="${9:-}"

  if [[ -z "$claude_binary" ]]; then
    claude_binary="$(command -v claude)" || {
      workflow_die "claude is not installed or not on PATH"
      return 1
    }
  fi

  printf '%s\n' \
    "claude_binary = \"$claude_binary\"" \
    'proxy_port = 13456' \
    'proxy_host = "127.0.0.1"' \
    'log_level = "info"' \
    'hyperlinks = "auto"' \
    '' \
    '[model_aliases]' \
    "fast = \"$fast_model\"" \
    "balanced = \"$balanced_model\"" \
    "powerful = \"$powerful_model\"" \
    '' \
    '[[profiles]]' \
    'name = "gpt"' \
    'provider_type = "DirectAnthropic"' \
    'base_url = "http://127.0.0.1:8787"' \
    'api_key = "claudex-passthrough"' \
    "default_model = \"$default_model\"" \
    'enabled = true' \
    'priority = 100' \
    '' \
    '[profiles.models]' \
    "haiku = \"$haiku_model\"" \
    "sonnet = \"$sonnet_model\"" \
    "opus = \"$opus_model\"" \
    '' \
    '[profiles.custom_headers]' \
    'X-Headroom-Base-Url = "http://127.0.0.1:8317"' \
    '' \
    '[router]' \
    'enabled = false' \
    '' \
    '[context.compression]' \
    'enabled = false' \
    '' \
    '[context.sharing]' \
    'enabled = false' \
    '' \
    '[context.rag]' \
    'enabled = false' >"$output_file"
}

render_cliproxy_config() {
  local output_file="$1"
  local auth_dir="$2"

  printf '%s\n' \
    'host: "127.0.0.1"' \
    'port: 8317' \
    'tls:' \
    '  enable: false' \
    'remote-management:' \
    '  allow-remote: false' \
    '  secret-key: ""' \
    '  disable-control-panel: true' \
    "auth-dir: \"$auth_dir\"" \
    'api-keys: []' \
    'debug: false' \
    'pprof:' \
    '  enable: false' \
    'plugins:' \
    '  enabled: false' \
    'commercial-mode: true' \
    'logging-to-file: false' \
    'usage-statistics-enabled: false' \
    'passthrough-headers: true' \
    'request-retry: 1' \
    'max-retry-credentials: 1' \
    'max-retry-interval: 10' >"$output_file"
}

sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

select_first_available() {
  local available_models="$1"
  shift
  local candidate
  for candidate in "$@"; do
    if printf '%s\n' "$available_models" | rg -Fxq "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  printf '%s\n' "$available_models" | head -1
}

select_required_model() {
  local available_models="$1"
  shift
  local candidate
  for candidate in "$@"; do
    if printf '%s\n' "$available_models" | rg -Fxq "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  workflow_die "none of the required models are available: $*"
}

login_flag_for_provider() {
  case "$1" in
    codex) printf '%s' '--codex-login' ;;
    claude) printf '%s' '--claude-login' ;;
    *) workflow_die "login provider must be 'codex' or 'claude'" ;;
  esac
}

render_discovered_claudex_config() {
  local models_json="$1"
  local output_file="$2"
  local model_ids gpt_models claude_models
  local fast_model balanced_model powerful_model
  local haiku_model sonnet_model opus_model

  model_ids="$(jq -r '.data[]?.id // empty' "$models_json")" || return 1
  gpt_models="$(printf '%s\n' "$model_ids" | rg '^gpt-' | rg -v '^gpt-image-' || true)"
  claude_models="$(printf '%s\n' "$model_ids" | rg '^claude-' || true)"

  fast_model="$(select_required_model "$gpt_models" gpt-5.6-luna)" || return 1
  balanced_model="$(select_required_model "$gpt_models" gpt-5.6-terra)" || return 1
  powerful_model="$(select_required_model "$gpt_models" gpt-5.6-sol)" || return 1
  haiku_model="$(select_required_model "$claude_models" \
    claude-haiku-4-5-20251001 claude-haiku-4-5)" || return 1
  sonnet_model="$(select_required_model "$claude_models" \
    claude-sonnet-5 claude-sonnet-4-6 claude-sonnet-4-5)" || return 1
  opus_model="$(select_required_model "$claude_models" claude-opus-4-8)" || return 1

  render_claudex_config "$output_file" \
    "$powerful_model" "$fast_model" "$balanced_model" "$powerful_model" \
    "$haiku_model" "$sonnet_model" "$opus_model"
}

extract_semver() {
  printf '%s\n' "$1" | rg -o -m1 '[0-9]+\.[0-9]+\.[0-9]+'
}

semver_at_least() {
  local current="$1"
  local minimum="$2"
  local current_major current_minor current_patch
  local minimum_major minimum_minor minimum_patch

  [[ "$current" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  [[ "$minimum" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1

  IFS=. read -r current_major current_minor current_patch <<<"$current"
  IFS=. read -r minimum_major minimum_minor minimum_patch <<<"$minimum"

  ((10#$current_major > 10#$minimum_major)) && return 0
  ((10#$current_major < 10#$minimum_major)) && return 1
  ((10#$current_minor > 10#$minimum_minor)) && return 0
  ((10#$current_minor < 10#$minimum_minor)) && return 1
  ((10#$current_patch >= 10#$minimum_patch))
}

remove_managed_symlink() {
  local link_path="$1"
  local expected_target="$2"
  if [[ -L "$link_path" ]] && [[ "$(readlink "$link_path")" == "$expected_target" ]]; then
    unlink "$link_path"
  fi
}

install_latest_github_binary() {
  local repository="$1"
  local prefix="$2"
  local suffix="$3"
  local archive_binary="$4"
  local destination="$5"
  local temp_dir metadata archive row url digest asset version actual_sha

  temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/claudex-install.XXXXXX")"
  metadata="$temp_dir/release.json"
  archive="$temp_dir/release.tar.gz"
  curl --fail --location --silent --show-error \
    "https://api.github.com/repos/$repository/releases/latest" --output "$metadata"
  row="$(jq -er --arg prefix "$prefix" --arg suffix "$suffix" '
    [.assets[] | select(.name | startswith($prefix) and endswith($suffix))] |
    if length == 1 then .[0] else error("expected exactly one release asset") end |
    [.browser_download_url, .digest, .name] | @tsv
  ' "$metadata")"
  IFS=$'\t' read -r url digest asset <<<"$row"
  version="$(jq -er '.tag_name | sub("^v"; "")' "$metadata")"
  [[ "$digest" == sha256:* ]] || workflow_die "GitHub did not publish a SHA-256 digest for $asset"

  if [[ -x "$destination" ]] && \
     "$destination" --version 2>&1 | rg -Fq "$version"; then
    printf '%s' "$version"
    rm -rf -- "$temp_dir"
    return 0
  fi

  curl --fail --location --silent --show-error "$url" --output "$archive"
  actual_sha="$(sha256_file "$archive")"
  [[ "$actual_sha" == "${digest#sha256:}" ]] || \
    workflow_die "checksum mismatch for $asset"
  tar -xzf "$archive" -C "$temp_dir" "$archive_binary"
  install -m 0755 "$temp_dir/$archive_binary" "$destination"
  rm -rf -- "$temp_dir"
  printf '%s' "$version"
}

backup_path() {
  local source_path="$1"
  local backup_dir="$2"
  local backup_name="$3"
  if [[ -e "$source_path" || -L "$source_path" ]]; then
    cp -pPR "$source_path" "$backup_dir/$backup_name"
  fi
}

render_launch_agent() {
  local output_file="$1"
  local workflow_root="$2"
  printf '%s\n' \
    '<?xml version="1.0" encoding="UTF-8"?>' \
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
    '<plist version="1.0">' \
    '<dict>' \
    '  <key>Label</key>' \
    '  <string>com.user.claudex-cliproxy</string>' \
    '  <key>ProgramArguments</key>' \
    '  <array>' \
    "    <string>$workflow_root/bin/cli-proxy-api</string>" \
    '    <string>--config</string>' \
    "    <string>$workflow_root/runtime/cliproxy.yaml</string>" \
    '  </array>' \
    '  <key>RunAtLoad</key>' \
    '  <true/>' \
    '  <key>KeepAlive</key>' \
    '  <true/>' \
    '  <key>ProcessType</key>' \
    '  <string>Background</string>' \
    '  <key>Umask</key>' \
    '  <integer>63</integer>' \
    '  <key>StandardOutPath</key>' \
    "  <string>$workflow_root/logs/cliproxy.log</string>" \
    '  <key>StandardErrorPath</key>' \
    "  <string>$workflow_root/logs/cliproxy.log</string>" \
    '  <key>EnvironmentVariables</key>' \
    '  <dict>' \
    '    <key>HOME</key>' \
    "    <string>$HOME</string>" \
    '  </dict>' \
    '</dict>' \
    '</plist>' >"$output_file"
}

systemd_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\%/%%}"
  value="${value//\$/\$\$}"
  printf '"%s"' "$value"
}

render_systemd_user_unit() {
  local output_file="$1"
  local workflow_root="$2"
  local executable config log
  executable="$(systemd_quote "$workflow_root/bin/cli-proxy-api")"
  config="$(systemd_quote "$workflow_root/runtime/cliproxy.yaml")"
  log="$(systemd_quote "append:$workflow_root/logs/cliproxy.log")"
  printf '%s\n' \
    '[Unit]' \
    'Description=Claudex CLIProxyAPI' \
    'StartLimitIntervalSec=60' \
    'StartLimitBurst=3' \
    '' \
    '[Service]' \
    'Type=exec' \
    "ExecStart=$executable --config $config" \
    'Restart=on-failure' \
    'RestartSec=5' \
    "StandardOutput=$log" \
    "StandardError=$log" \
    '' \
    '[Install]' \
    'WantedBy=default.target' >"$output_file"
}
