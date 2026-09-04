#!/bin/bash
#
# OpenTrickler install, part 1 of 2: system preparation.
#
# Everything up to and including Adafruit Blinka, which requires a reboot. Run this,
# reboot, then run install-part2.sh.
#
# Safe to re-run: every step checks whether it has already been done.
#
# This deliberately does not touch swap. How Raspberry Pi OS manages it changed with
# Trixie, and getting it wrong silently is worse than leaving it alone. If you are on a
# Pi with little RAM, see the swap note in the README before running this -- the upgrade
# below is where a 512 MB Pi runs out of memory.

set -euo pipefail

readonly REPO_URL="https://github.com/codebydch/open-trickler-peripheral.git"
readonly CODE_DIR="/code"
readonly REPO_DIR="${CODE_DIR}/open-trickler-peripheral"
readonly VENV_DIR="${CODE_DIR}/venv"
readonly BLINKA_URL="https://raw.githubusercontent.com/adafruit/Raspberry-Pi-Installer-Scripts/master/raspi-blinka.py"

readonly APT_PACKAGES=(
  git
  unzip
  memcached
  nginx
  pigpio
  fonts-dejavu
  python3-pil
  python3-numpy
)

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
skip() { printf '    (already done) %s\n' "$*"; }
die()  { printf '\n\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

check_environment() {
  step "Checking the environment"
  [[ ${EUID} -ne 0 ]] || die "Run this as your normal login user, not with sudo. It calls sudo itself where it needs to."
  command -v apt-get >/dev/null || die "This expects Raspberry Pi OS (or another Debian). No apt-get found."
  command -v sudo >/dev/null || die "sudo is required."
  info "Running as $(whoami) on $(uname -m)."
  # Ask for the sudo password once, up front, rather than partway through a long upgrade.
  sudo -v
}

install_packages() {
  step "Installing system packages"
  info "Updating the package lists. This takes a while on a Pi Zero."
  sudo apt-get update
  info "Upgrading installed packages."
  info "On a Pi with little RAM this is the step that can run out of memory. If it dies"
  info "here, set up swap (see the README) and run this script again."
  sudo DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
  info "Installing: ${APT_PACKAGES[*]}"
  sudo DEBIAN_FRONTEND=noninteractive apt-get -y install "${APT_PACKAGES[@]}"
}

create_code_dir() {
  step "Preparing ${CODE_DIR}"
  if [[ -d ${CODE_DIR} ]]; then
    skip "${CODE_DIR} exists."
  else
    sudo mkdir -p "${CODE_DIR}"
  fi
  sudo chown "$(id -un):$(id -gn)" "${CODE_DIR}"
  info "${CODE_DIR} is owned by $(id -un)."
}

clone_repository() {
  step "Fetching the OpenTrickler code"
  if [[ -d "${REPO_DIR}/.git" ]]; then
    # Never touch an existing checkout: it may hold a tuned config or local changes.
    skip "${REPO_DIR} is already a git checkout. Leaving it alone; use update.sh to update it."
    return
  fi
  [[ ! -e ${REPO_DIR} ]] || die "${REPO_DIR} exists but is not a git checkout. Move it aside and re-run."
  git clone "${REPO_URL}" "${REPO_DIR}"
}

create_virtualenv() {
  step "Creating the Python virtual environment"
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    skip "${VENV_DIR} exists."
  else
    # --system-site-packages so the apt-installed PIL and numpy are visible to it.
    python3 -m venv "${VENV_DIR}" --system-site-packages
  fi
  info "Installing Python dependencies from requirements-to-freeze.txt."
  "${VENV_DIR}/bin/pip" install --upgrade pip
  "${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements-to-freeze.txt"
}

install_blinka() {
  step "Installing Adafruit Blinka (for the Mini PiTFT screen)"
  if "${VENV_DIR}/bin/python" -c 'import board' >/dev/null 2>&1; then
    skip "Blinka is already working in the virtual environment."
    return
  fi
  # raspi-blinka.py imports adafruit_shell, so its own dependency has to go in first.
  info "Installing adafruit-python-shell, which the Adafruit installer needs."
  "${VENV_DIR}/bin/pip" install --upgrade adafruit-python-shell

  local work script
  work="$(mktemp -d)"
  script="${work}/raspi-blinka.py"
  info "Downloading the Adafruit installer."
  curl -fsSL "${BLINKA_URL}" -o "${script}"
  info "Running it as root, against the virtual environment's Python so the libraries"
  info "land where the trickler will look for them."
  info "Answer NO when it offers to reboot -- reboot yourself once this script finishes."
  sudo -E "${VENV_DIR}/bin/python" "${script}"
  rm -rf "${work}"
}

main() {
  check_environment
  install_packages
  create_code_dir
  clone_repository
  create_virtualenv
  install_blinka

  cat <<EOF

$(printf '\033[1m')Part 1 finished.$(printf '\033[0m')

Blinka needs a reboot before the screen libraries will work. Reboot now:

    sudo reboot

Then finish the install:

    ${REPO_DIR}/install-part2.sh

EOF
}

main "$@"
