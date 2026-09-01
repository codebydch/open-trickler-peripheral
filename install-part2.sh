#!/bin/bash
#
# OpenTrickler install, part 2 of 2: the trickler itself.
#
# Run this after install-part1.sh and a reboot. It installs websocketd, configures nginx,
# publishes the web pages and starts the services.
#
# Safe to re-run: every step checks whether it has already been done.

set -euo pipefail

readonly CODE_DIR="/code"
readonly VENV_DIR="${CODE_DIR}/venv"
# Assigned separately from `readonly` so a failed cd aborts under `set -e`
# instead of leaving the path empty.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_DIR
readonly HISTORY_DIR="/var/lib/opentrickler"
readonly WEB_ROOT="/var/www/html"
readonly WEBSOCKETD_VERSION="0.4.1"
readonly WEBSOCKETD_BASE="https://github.com/joewalnes/websocketd/releases/download"

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
skip() { printf '    (already done) %s\n' "$*"; }
warn() { printf '    \033[33m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

check_preconditions() {
  step "Checking that part 1 has been run"
  [[ ${EUID} -ne 0 ]] || die "Run this as your normal login user, not with sudo. It calls sudo itself where it needs to."
  [[ -x "${VENV_DIR}/bin/python" ]] || die "No virtual environment at ${VENV_DIR}. Run install-part1.sh first."
  if ! "${VENV_DIR}/bin/python" -c 'import board' >/dev/null 2>&1; then
    die "Adafruit Blinka is not working yet. Run install-part1.sh, then reboot, then run this."
  fi
  [[ -f "${REPO_DIR}/opentrickler_config.ini" ]] || die "Run this from the repository: ${REPO_DIR} doesn't look like the checkout."
  info "Virtual environment and Blinka are in place."
  sudo -v
}

websocketd_asset() {
  # linux_arm is 32-bit only, so a 64-bit Pi OS needs a different build.
  case "$(uname -m)" in
    armv6l|armv7l) echo "websocketd-${WEBSOCKETD_VERSION}-linux_arm.zip" ;;
    aarch64)       echo "websocketd-${WEBSOCKETD_VERSION}-linux_arm64.zip" ;;
    *)             die "No websocketd build known for architecture '$(uname -m)'. Install it by hand from ${WEBSOCKETD_BASE}/v${WEBSOCKETD_VERSION}/" ;;
  esac
}

install_websocketd() {
  step "Installing websocketd ${WEBSOCKETD_VERSION} (serves the log pages)"
  if command -v websocketd >/dev/null 2>&1 &&
     websocketd --version 2>&1 | grep -q "${WEBSOCKETD_VERSION}"; then
    skip "websocketd ${WEBSOCKETD_VERSION} is installed."
    return
  fi
  local asset work
  asset="$(websocketd_asset)"
  work="$(mktemp -d)"
  info "Architecture $(uname -m) needs ${asset}."
  curl -fsSL "${WEBSOCKETD_BASE}/v${WEBSOCKETD_VERSION}/${asset}" -o "${work}/websocketd.zip"
  unzip -q -o "${work}/websocketd.zip" -d "${work}"
  sudo install -m 0755 "${work}/websocketd" /usr/sbin/websocketd
  rm -rf "${work}"
  info "Installed $(websocketd --version 2>&1 | head -1)."
}

configure_nginx() {
  step "Configuring nginx"
  sudo install -m 0644 "${REPO_DIR}/nginx/default" /etc/nginx/sites-available/default
  # A stock Raspberry Pi OS image already has this symlink, but a tidied one may not.
  if [[ -L /etc/nginx/sites-enabled/default ]]; then
    skip "sites-enabled/default is already linked."
  else
    sudo ln -sfn /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
    info "Linked sites-enabled/default."
  fi
  # Check before reloading, so a bad config doesn't take the pages down.
  sudo nginx -t
  sudo systemctl reload nginx
}

publish_web_pages() {
  step "Publishing the web pages to ${WEB_ROOT}"
  # nginx serves from here, so these are copies. This is the step a plain `git pull`
  # misses, which is why update.sh does it too.
  sudo install -d -m 0755 "${WEB_ROOT}"
  local pages=("${REPO_DIR}"/html/*.html)
  sudo install -m 0644 "${pages[@]}" "${WEB_ROOT}/"
  info "Copied ${#pages[@]} pages."
}

create_history_dir() {
  step "Creating ${HISTORY_DIR} for the charge history"
  if [[ -d ${HISTORY_DIR} ]]; then
    skip "${HISTORY_DIR} exists."
  else
    sudo install -d -m 0755 "${HISTORY_DIR}"
  fi
}

enable_pigpiod() {
  step "Enabling pigpiod (drives the servo)"
  sudo systemctl enable pigpiod
  sudo systemctl start pigpiod
}

install_services() {
  step "Installing the systemd services"
  sudo install -m 0644 "${REPO_DIR}"/system/*.service /etc/systemd/system/
  sudo systemctl daemon-reload
  for service in "${SERVICES[@]}"; do
    sudo systemctl enable "${service}.service"
    sudo systemctl restart "${service}.service"
    info "Started ${service}."
  done
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
    warn "Something did not start. Check it with:"
    warn "    journalctl -u opentrickler -n 50 --no-pager"
  fi

  cat <<EOF

$(printf '\033[1m')Install finished.$(printf '\033[0m')

Open http://opentrickler.local (or this Pi's IP address).

Before the first charge, check the scale settings in
${REPO_DIR}/opentrickler_config.ini -- the [scale] model and port have to match
your hardware. Then set the trickler motors' stall speed on the tuning page at
http://opentrickler.local/app/config/

EOF
}

main() {
  check_preconditions
  install_websocketd
  configure_nginx
  publish_web_pages
  create_history_dir
  enable_pigpiod
  install_services
  report
}

main "$@"
