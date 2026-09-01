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
    # opentrickler_config.ini is tracked, so a blind checkout would throw away tuning.
    die "Commit, stash or revert these first. Note that opentrickler_config.ini is tracked, so anything you tuned by hand shows up here -- don't discard it without looking."
  fi
  info "Clean."
  sudo -v
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
  update_dependencies
  publish_web_pages
  update_nginx
  update_services
  report
}

main "$@"
