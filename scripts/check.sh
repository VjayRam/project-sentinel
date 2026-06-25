#!/usr/bin/env bash
# Local pre-push checks — mirrors the CI pipeline exactly.
# Run manually:  ./scripts/check.sh
# Install as git hook:  ./scripts/check.sh --install-hook
set -euo pipefail

# ── colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'

pass() { echo -e "${GREEN}  ✓ $1${RESET}"; }
fail() { echo -e "${RED}  ✗ $1${RESET}"; }
info() { echo -e "${YELLOW}  → $1${RESET}"; }
header() { echo -e "\n${BOLD}$1${RESET}"; }

# ── hook installer ────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--install-hook" ]]; then
  HOOK=".git/hooks/pre-push"
  cat > "$HOOK" <<'EOF'
#!/usr/bin/env bash
exec "$(git rev-parse --show-toplevel)/scripts/check.sh"
EOF
  chmod +x "$HOOK"
  echo -e "${GREEN}Installed pre-push hook → .git/hooks/pre-push${RESET}"
  echo "The checks will now run automatically on every 'git push'."
  echo "To skip in an emergency:  git push --no-verify"
  exit 0
fi

# ── setup ─────────────────────────────────────────────────────────────────────
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

FAILURES=()
START=$(date +%s)

run_step() {
  local name="$1"; shift
  header "[$name]"
  if "$@"; then
    pass "$name passed"
  else
    fail "$name FAILED"
    FAILURES+=("$name")
  fi
}

# ── 1. ruff lint ──────────────────────────────────────────────────────────────
if command -v ruff &>/dev/null; then
  run_step "ruff lint" ruff check .
else
  info "ruff not found — skipping lint (pip install ruff)"
  FAILURES+=("ruff lint [not installed]")
fi

# ── 2. ruff format ────────────────────────────────────────────────────────────
if command -v ruff &>/dev/null; then
  run_step "ruff format" ruff format --check .
fi

# ── 3. unit tests ─────────────────────────────────────────────────────────────
if command -v pytest &>/dev/null; then
  run_step "unit tests" pytest tests/unit -v --tb=short -q
else
  info "pytest not found — skipping tests (pip install pytest)"
  FAILURES+=("unit tests [not installed]")
fi

# ── 4. terraform validate (optional) ─────────────────────────────────────────
if command -v terraform &>/dev/null; then
  header "[terraform validate]"
  info "checking format..."
  if terraform fmt -check -recursive terraform/ 2>&1; then
    pass "terraform fmt"
  else
    fail "terraform fmt FAILED  (run: terraform fmt -recursive terraform/)"
    FAILURES+=("terraform fmt")
  fi
  info "validating local environment..."
  if (cd terraform/environments/local && terraform init -backend=false -input=false &>/dev/null && terraform validate); then
    pass "terraform validate"
  else
    fail "terraform validate FAILED"
    FAILURES+=("terraform validate")
  fi
else
  info "terraform not found — skipping (not required to push)"
fi

# ── summary ───────────────────────────────────────────────────────────────────
ELAPSED=$(( $(date +%s) - START ))
echo -e "\n${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

if [[ ${#FAILURES[@]} -eq 0 ]]; then
  echo -e "${GREEN}${BOLD}  ALL CHECKS PASSED${RESET}  (${ELAPSED}s)"
  echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
  exit 0
else
  echo -e "${RED}${BOLD}  FAILED (${#FAILURES[@]} check(s)):${RESET}"
  for f in "${FAILURES[@]}"; do
    echo -e "${RED}    • $f${RESET}"
  done
  echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
  exit 1
fi
