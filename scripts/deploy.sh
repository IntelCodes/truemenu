#!/usr/bin/env bash
# Deploy one exact commit to the server, or roll back. Stops at the first problem; never leaves a mix of versions.
#
#   bash deploy.sh <commit-sha>     deploy that commit
#   bash deploy.sh --rollback       put back the version that was running before the last deploy
#
# Run it from a copy outside the app folder (the deploy replaces scripts/), for example:
#   curl -fsSL https://raw.githubusercontent.com/IntelCodes/truemenu/<sha>/scripts/deploy.sh -o /tmp/deploy.sh
#   bash /tmp/deploy.sh <sha>
#
# Steps: download the whole commit into a temp folder (abort if any download fails), compile every .py,
# run the unit tests there, move the running code aside, move the new code in, restart, then check
# /health and the booth. If any check fails, the old code is moved back and restarted automatically.
# Never touches .env, state/, logs/, runs/ or venv/.

main() {
  set -euo pipefail
  APP=${APP:-$HOME/truemenu}
  REPO=IntelCodes/truemenu
  RELEASES=$HOME/truemenu-releases
  DIRS="truemenu scripts agent data docs tests"
  FILES="ask check feed requirements.txt README.md"
  cd "$APP"
  PORT=$(set -a; . ./.env; echo "${TRUEMENU_PORT:-8088}")    # the port start.sh uses
  mkdir -p "$RELEASES"

  if [ "${1:-}" = "--rollback" ]; then
    LAST=$(ls -1d "$RELEASES"/2*/ 2>/dev/null | tail -1)
    [ -n "$LAST" ] || { echo "No earlier version saved in $RELEASES"; exit 1; }
    echo "Rolling back to the version saved in $LAST"
    AWAY="$RELEASES/rolled-back-$(date -u +%Y%m%dT%H%M%SZ)"
    swap_in "$LAST" "$AWAY"
    cp -f "$LAST/DEPLOYED_SHA" DEPLOYED_SHA 2>/dev/null || true
    restart_and_check || { echo "ROLLBACK FAILED HEALTH CHECK: see logs/api.log (the version rolled away from is in $AWAY)"; exit 1; }
    rm -rf "$LAST" "$AWAY"
    echo "Rolled back to $(cat DEPLOYED_SHA 2>/dev/null || echo "the earlier version")."
    exit 0
  fi

  SHA=${1:?usage: deploy.sh <commit-sha> | --rollback}
  [[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "Give the full 40-character commit SHA"; exit 1; }

  TMP=$(mktemp -d "$HOME/.truemenu-deploy.XXXXXX")     # same disk as the app, so the switch is only renames
  trap 'rm -rf "$TMP"' EXIT
  echo "1/5 Downloading $SHA"
  curl -fsSL --retry 3 "https://codeload.github.com/$REPO/tar.gz/$SHA" -o "$TMP/src.tgz"
  mkdir "$TMP/new"
  tar -xzf "$TMP/src.tgz" -C "$TMP/new" --strip-components=1
  for p in $DIRS $FILES; do [ -e "$TMP/new/$p" ] || { echo "Download is missing $p; nothing changed"; exit 1; }; done

  echo "2/5 Checking the new code before it goes live"
  if ! cmp -s requirements.txt "$TMP/new/requirements.txt"; then
    echo "requirements.txt changed: install them first (venv/bin/pip install -r <new requirements>); nothing changed"; exit 1
  fi
  ./venv/bin/python -m compileall -q "$TMP/new/truemenu" "$TMP/new/scripts" "$TMP/new/agent" >/dev/null
  ( cd "$TMP/new" && env -i PATH="$PATH" HOME="$TMP" "$APP/venv/bin/python" -m unittest discover -s tests >"$TMP/tests.log" 2>&1 ) \
    || { tail -20 "$TMP/tests.log"; echo "Unit tests failed on the new code; nothing changed"; exit 1; }
  echo "    $(grep -E '^Ran [0-9]+ tests' "$TMP/tests.log"): $(tail -1 "$TMP/tests.log")"

  echo "3/5 Saving the running version"
  SAVE="$RELEASES/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$SAVE"
  cp -f DEPLOYED_SHA "$SAVE/DEPLOYED_SHA" 2>/dev/null || echo "unknown (before deploy.sh)" > "$SAVE/DEPLOYED_SHA"
  echo "4/5 Switching to $SHA"
  swap_in "$TMP/new" "$SAVE"
  echo "$SHA" > DEPLOYED_SHA

  echo "5/5 Restarting and checking"
  if restart_and_check; then
    ls -1d "$RELEASES"/2*/ | head -n -3 | xargs -r rm -rf      # keep the last 3 saved versions
    echo "Deployed $SHA. Earlier version saved in $SAVE (bash deploy.sh --rollback to return to it)."
  else
    echo "Checks failed: putting the earlier version back"
    swap_in "$SAVE" "$TMP/failed"
    cp -f "$SAVE/DEPLOYED_SHA" DEPLOYED_SHA
    rm -rf "$SAVE"
    restart_and_check && echo "Earlier version is running again. Nothing else changed." \
      || echo "EARLIER VERSION ALSO FAILED: see logs/api.log"
    exit 1
  fi
}

# Move the current code into $2 and the code in $1 into place. Only renames, so each step is instant.
swap_in() {
  local src=$1 old=$2
  mkdir -p "$old"
  for p in $DIRS $FILES; do
    [ -e "$p" ] && mv "$p" "$old/$p"
    mv "$src/$p" "$p"
  done
}

restart_and_check() {
  scripts/stop.sh >/dev/null 2>&1 || true
  scripts/start.sh || return 1
  sleep 3
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null || { echo "    /health failed"; return 1; }
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/demo" || { echo "    /demo page failed"; return 1; }
  curl -sf -o /dev/null -H "x-visitor: deploy-check-0001" "http://127.0.0.1:$PORT/demo/api/status" \
    || { echo "    /demo/api/status failed"; return 1; }
  echo "    API, booth page and booth status answer"
}

main "$@"
exit
