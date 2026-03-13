#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "${ROOT_DIR}" ]]; then
  echo "Error: not inside a git repository." >&2
  exit 1
fi

cd "${ROOT_DIR}"

INSTALL_DEPS=0
SKIP_LDAP=0
TARGETS_RAW="all"
PYTHON_BIN="${PYTHON_BIN:-python}"
ENV_MODE="auto"          # auto | uv | system
USE_UV=0
UV_PROJECT_PYTHON=".venv/bin/python"
UV_PROJECT_ENVIRONMENT=".venv"

LDAP_COMPOSE_FILE="continuous_integration/docker-configs/ldap-docker-compose.yml"

print_help() {
  cat <<'EOF'
Run local equivalents of the GitHub Actions checks.

Usage:
  ./scripts/run-github-actions-local.sh [options]

Options:
  --targets <csv>                  Comma-separated targets:
                                   black,isort,flake8,pre-commit,unit,docs,all
                                   (default: all)
  --install-deps                   Install/upgrade dependencies before running
                                   (auto-runs when required deps are missing)
  --env <auto|uv|system>           Environment mode (default: auto)
                                   auto: use uv if available, otherwise system
  --skip-ldap                      Do not start LDAP for unit tests
  --python <python-bin>            Python executable (default: python)
  -h, --help                       Show this help

Examples:
  ./scripts/run-github-actions-local.sh
  ./scripts/run-github-actions-local.sh --targets black,isort,flake8,pre-commit
  ./scripts/run-github-actions-local.sh --targets unit --install-deps
  ./scripts/run-github-actions-local.sh --env uv --targets black,unit,docs
EOF
}

log_section() {
  echo
  echo "==> $*"
}

run_cmd() {
  echo "+ $*"
  "$@"
}

run_local_cmd() {
  if [[ ${USE_UV} -eq 1 ]]; then
    run_cmd uv run --python "${UV_PROJECT_PYTHON}" "$@"
  else
    run_cmd "$@"
  fi
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

install_dependencies() {
  log_section "Installing dependencies"
  if [[ ${USE_UV} -eq 1 ]]; then
    if [[ ! -x "${UV_PROJECT_PYTHON}" ]]; then
      echo "Error: uv mode requires an existing project environment at ${UV_PROJECT_PYTHON}." >&2
      echo "Create it once with: uv venv" >&2
      exit 1
    fi
    run_cmd uv pip install --python "${UV_PROJECT_PYTHON}" --upgrade pip setuptools numpy
    run_cmd uv pip install --python "${UV_PROJECT_PYTHON}" -e .
    run_cmd uv pip install --python "${UV_PROJECT_PYTHON}" -r requirements-dev.txt
  else
    run_cmd "${PYTHON_BIN}" -m pip install --upgrade pip setuptools numpy
    run_cmd "${PYTHON_BIN}" -m pip install -e .
    run_cmd "${PYTHON_BIN}" -m pip install -r requirements-dev.txt
  fi
}

start_ldap_if_needed() {
  if [[ ${SKIP_LDAP} -eq 1 ]]; then
    log_section "Skipping LDAP startup (--skip-ldap)"
    return
  fi
  if [[ ! -f "${LDAP_COMPOSE_FILE}" ]]; then
    echo "Warning: LDAP compose file not found at ${LDAP_COMPOSE_FILE}; continuing without startup." >&2
    return
  fi
  log_section "Starting LDAP service for LDAP-related tests"
  run_cmd bash continuous_integration/scripts/start_LDAP.sh
}

run_local_target() {
  local target="$1"
  case "${target}" in
    black)
      log_section "Running BLACK (local)"
      run_local_cmd black . --check
      ;;
    isort)
      log_section "Running ISORT (local)"
      run_local_cmd isort . -c
      ;;
    flake8)
      log_section "Running FLAKE8 (local)"
      run_local_cmd flake8
      ;;
    pre-commit)
      log_section "Running pre-commit (local)"
      run_local_cmd pre-commit run --all-files
      ;;
    unit)
      log_section "Running unit tests (local)"
      start_ldap_if_needed
      run_local_cmd coverage run -m pytest -vv
      run_local_cmd coverage report -m
      ;;
    docs)
      log_section "Building docs (local)"
      run_local_cmd make -C docs/ html
      ;;
    *)
      echo "Error: unknown local target '${target}'" >&2
      exit 2
      ;;
  esac
}

normalize_targets() {
  local raw="$1"
  raw="${raw//testing/unit}"
  if [[ "${raw}" == "all" ]]; then
    echo "black isort flake8 pre-commit unit docs"
    return
  fi

  local csv="${raw//,/ }"
  local out=()
  local item
  for item in ${csv}; do
    case "${item}" in
      black|isort|flake8|pre-commit|unit|docs)
        out+=("${item}")
        ;;
      testing)
        out+=("unit")
        ;;
      *)
        echo "Error: unknown target '${item}'." >&2
        exit 2
        ;;
    esac
  done

  if [[ ${#out[@]} -eq 0 ]]; then
    echo "Error: no targets specified." >&2
    exit 2
  fi

  echo "${out[*]}"
}

detect_environment_mode() {
  case "${ENV_MODE}" in
    system)
      USE_UV=0
      ;;
    uv)
      if ! has_cmd uv; then
        echo "Error: --env uv requested but 'uv' is not installed or not on PATH." >&2
        exit 1
      fi
      if [[ ! -x "${UV_PROJECT_PYTHON}" ]]; then
        echo "Error: --env uv requested but ${UV_PROJECT_PYTHON} does not exist." >&2
        echo "Create it once with: uv venv" >&2
        exit 1
      fi
      USE_UV=1
      ;;
    auto)
      if has_cmd uv; then
        if [[ -x "${UV_PROJECT_PYTHON}" ]]; then
          USE_UV=1
        else
          USE_UV=0
        fi
      else
        USE_UV=0
      fi
      ;;
    *)
      echo "Error: --env must be one of: auto, uv, system" >&2
      exit 2
      ;;
  esac
}

missing_local_tools() {
  # In uv mode, commands are launched via 'uv run', so PATH checks are not useful.
  if [[ ${USE_UV} -eq 1 ]]; then
    return
  fi

  local -a targets=("$@")
  local -a required_tools=()
  local target

  for target in "${targets[@]}"; do
    case "${target}" in
      black)
        required_tools+=("black")
        ;;
      isort)
        required_tools+=("isort")
        ;;
      flake8)
        required_tools+=("flake8")
        ;;
      pre-commit)
        required_tools+=("pre-commit")
        ;;
      unit)
        required_tools+=("coverage" "pytest")
        ;;
      docs)
        required_tools+=("make")
        ;;
    esac
  done

  local -a missing=()
  local tool
  for tool in "${required_tools[@]}"; do
    if ! has_cmd "${tool}"; then
      missing+=("${tool}")
    fi
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "${missing[*]}"
  fi
}

uv_env_needs_bootstrap() {
  local -a targets=("$@")
  local module_check_script="import importlib.util as u\nmodules=[]\n"

  local target
  for target in "${targets[@]}"; do
    case "${target}" in
      black)
        module_check_script+="modules.append('black')\n"
        ;;
      isort)
        module_check_script+="modules.append('isort')\n"
        ;;
      flake8)
        module_check_script+="modules.append('flake8')\n"
        ;;
      pre-commit)
        module_check_script+="modules.append('pre_commit')\n"
        ;;
      unit)
        module_check_script+="modules.extend(['pytest', 'coverage'])\n"
        ;;
      docs)
        module_check_script+="modules.append('sphinx')\n"
        ;;
    esac
  done
  module_check_script+="missing=[m for m in modules if u.find_spec(m) is None]\n"
  module_check_script+="raise SystemExit(1 if missing else 0)\n"

  if [[ ! -x "${UV_PROJECT_PYTHON}" ]]; then
    return 0
  fi

  if uv run --python "${UV_PROJECT_PYTHON}" python -c "${module_check_script}" >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --targets)
      TARGETS_RAW="$2"
      shift 2
      ;;
    --install-deps)
      INSTALL_DEPS=1
      shift
      ;;
    --env)
      ENV_MODE="$2"
      shift 2
      ;;
    --skip-ldap)
      SKIP_LDAP=1
      shift
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    -h|--help)
      print_help
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      print_help
      exit 2
      ;;
  esac
done

IFS=' ' read -r -a TARGETS <<< "$(normalize_targets "${TARGETS_RAW}")"
detect_environment_mode

log_section "Configuration"
echo "Repository: ${ROOT_DIR}"
echo "Mode: local"
echo "Targets: ${TARGETS[*]}"
if [[ ${USE_UV} -eq 1 ]]; then
  export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT}"
  echo "Environment: uv (${UV_PROJECT_PYTHON})"
else
  echo "Environment: system (${PYTHON_BIN})"
fi

if [[ ${INSTALL_DEPS} -eq 1 ]]; then
  install_dependencies
fi

if [[ ${USE_UV} -eq 1 ]]; then
  if uv_env_needs_bootstrap "${TARGETS[@]}"; then
    log_section "uv environment missing required packages"
    echo "Auto-installing dependencies (equivalent to --install-deps)."
    install_dependencies
  fi
else
  MISSING_TOOLS="$(missing_local_tools "${TARGETS[@]}" || true)"
  if [[ -n "${MISSING_TOOLS}" ]]; then
    log_section "Missing local tools detected"
    echo "Missing: ${MISSING_TOOLS}"
    echo "Auto-installing dependencies (equivalent to --install-deps)."
    install_dependencies
  fi
fi

FAILURES=()

for target in "${TARGETS[@]}"; do
  if ! run_local_target "${target}"; then
    FAILURES+=("${target}")
    log_section "Target failed"
    echo "Failed target: ${target}"
  fi
done

if [[ ${#FAILURES[@]} -gt 0 ]]; then
  log_section "Summary"
  echo "Completed with failures in: ${FAILURES[*]}"
  exit 1
fi

log_section "Done"
echo "All requested checks completed."
