#!/bin/bash
#
# Update an existing OpenTrickler install: pull the code, republish the web pages,
# refresh the services and restart them.
#
# This exists because `git pull` on its own is not enough. nginx serves copies of the
# pages from /var/www/html, and the service files live in /etc/systemd/system, so a pull
# alone leaves both stale.

set -euo pipefail

readonly VENV_DIR="/code/venv"
# Assigned separately from `readonly` so a failed cd aborts under `set -e`
# instead of leaving the path empty.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_DIR
readonly WEB_ROOT="/var/www/html"

readonly SERVICES=(
  opentrickler
  opentrickler_screen
  opentrickler_flask_app
  opentrickler_flask_servo_app
  websocketd-1
  websocketd-2
  websocketd-4
  websocketd-5
)

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
skip() { printf '    (unchanged) %s\n' "$*"; }
die()  { printf '\n\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

check_clean_tree() {
  step "Checking the working tree"
  [[ ${EUID} -ne 0 ]] || die "Run this as your normal login user, not with sudo."
  cd "${REPO_DIR}"
  local dirty
  dirty="$(git status --porcelain)"
  if [[ -n ${dirty} ]]; then
    printf '\n\033[31mThe working tree has local changes:\033[0m\n\n%s\n\n' "${dirty}" >&2
    # Tuning no longer shows up here: opentrickler_config.ini is git-ignored, and only
    # opentrickler_config.ini.example is tracked. Anything listed above is a real edit.
    die "Commit, stash or revert these first."
  fi
  info "Clean."
  sudo -v
}

# Reports settings the update added, so a new tuning value doesn't sit at its fallback
# unnoticed. Never edits the live config: the values in it are the machine's, and only
# its owner knows which of them were arrived at with a scale and a pan of powder.
check_config() {
  step "Checking the tuning config"
  local live="${REPO_DIR}/opentrickler_config.ini"
  local example="${REPO_DIR}/opentrickler_config.ini.example"

  if [[ ! -f ${live} ]]; then
    cp "${example}" "${live}"
    info "Created opentrickler_config.ini from the shipped example."
    return
  fi

  local added=()
  local key
  while IFS= read -r key; do
    grep -qE "^[[:space:]]*${key}[[:space:]]*=" "${live}" || added+=("${key}")
  # Upper-case too: the [memcache_vars] keys are, and a daemon built against a config
  # that lacks one of them fails on first use with an AttributeError.
  done < <(grep -oE '^[A-Za-z_]+[[:space:]]*=' "${example}" | tr -d ' =' | sort -u)

  if [[ ${#added[@]} -eq 0 ]]; then
    skip "no new settings."
    return
  fi
  info "This update adds ${#added[@]} setting(s) your config doesn't have yet:"
  for key in "${added[@]}"; do
    printf '      %s = %s\n' "${key}" \
      "$(grep -m1 -E "^${key}[[:space:]]*=" "${example}" | cut -d= -f2- | xargs)"
  done
  info "They fall back to those values until you set them. See the example file for what"
  info "each one does, or set them from the tuning page at http://opentrickler.local"
}

pull_code() {
  step "Pulling the latest code"
  local before after
  before="$(git rev-parse HEAD)"
  git pull --ff-only
  after="$(git rev-parse HEAD)"
  if [[ ${before} == "${after}" ]]; then
    info "Already up to date at ${after:0:8}."
  else
    info "Updated ${before:0:8} -> ${after:0:8}:"
    git --no-pager log --oneline "${before}..${after}" | sed 's/^/      /'
  fi
  # Used below to decide what actually needs refreshing.
  CHANGED="$(git diff --name-only "${before}" "${after}")"
}

update_dependencies() {
  step "Checking Python dependencies"
  if [[ -n ${CHANGED} ]] && ! grep -q '^requirements-to-freeze.txt$' <<<"${CHANGED}"; then
    skip "requirements-to-freeze.txt did not change."
    return
  fi
  info "Installing from requirements-to-freeze.txt."
  "${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements-to-freeze.txt"
}

publish_web_pages() {
  step "Republishing the web pages"
  # Always, regardless of what changed: this is the step people forget, and it is cheap.
  sudo install -m 0644 "${REPO_DIR}"/html/*.html "${WEB_ROOT}/"
  info "Copied to ${WEB_ROOT}."
}

update_nginx() {
  step "Checking the nginx configuration"
  if [[ -n ${CHANGED} ]] && ! grep -q '^nginx/' <<<"${CHANGED}"; then
    skip "nginx/default did not change."
    return
  fi
  sudo install -m 0644 "${REPO_DIR}/nginx/default" /etc/nginx/sites-available/default
  sudo nginx -t
  sudo systemctl reload nginx
  info "Reloaded nginx."
}

update_services() {
  step "Refreshing the systemd services"
  if [[ -n ${CHANGED} ]] && ! grep -q '^system/' <<<"${CHANGED}"; then
    skip "no service files changed."
  else
    sudo install -m 0644 "${REPO_DIR}"/system/*.service /etc/systemd/system/
    sudo systemctl daemon-reload
    info "Service files updated."
  fi

  for service in "${SERVICES[@]}"; do
    sudo systemctl restart "${service}.service"
  done
  info "Restarted ${#SERVICES[@]} services."
}

report() {
  step "Checking the services"
  local failed=0
  for service in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "${service}.service"; then
      printf '    \033[32m%-32s active\033[0m\n' "${service}"
    else
      printf '    \033[31m%-32s NOT RUNNING\033[0m\n' "${service}"
      failed=1
    fi
  done
  if [[ ${failed} -eq 1 ]]; then
    printf '\n\033[33mCheck what went wrong: journalctl -u opentrickler -n 50 --no-pager\033[0m\n'
    exit 1
  fi
  printf '\n\033[1mUpdate finished.\033[0m http://opentrickler.local\n\n'
}

main() {
  CHANGED=""
  check_clean_tree
  pull_code
  check_config
  update_dependencies
  publish_web_pages
  update_nginx
  update_services
  report
}

main "$@"
