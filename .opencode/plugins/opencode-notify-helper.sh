#!/bin/bash
# OpenCode plugin helper - calls ntfy-notify-common.sh with adapted event data
# Receives: NOTIFICATION_TYPE MESSAGE DIRECTORY

NOTIFICATION_TYPE="$1"
MESSAGE="$2"
DIRECTORY="$3"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/scripts/ntfy-notify-common.sh" 2>/dev/null \
    || source "$HOME/.local/bin/ntfy-notify-common.sh" 2>/dev/null \
    || { echo "ERROR: ntfy-notify-common.sh not found" >&2; exit 1; }

check_required_tools jq || exit 1

COOLDOWN_SECONDS="${NTFY_COOLDOWN_SECONDS:-86400}"

PROJECT=""
if [[ -n "$DIRECTORY" ]]; then
    PROJECT=$(basename "$DIRECTORY")
fi

ntfy_log INFO "OpenCode plugin fired: type=${NOTIFICATION_TYPE} project=${PROJECT}"

# Directory exclusions
if [[ -n "${NOTIFY_EXCLUDE_DIRS:-}" && -n "$DIRECTORY" ]]; then
    IFS=: read -ra _exclude_list <<< "$NOTIFY_EXCLUDE_DIRS"
    for _excl in "${_exclude_list[@]}"; do
        [[ -z "$_excl" ]] && continue
        if [[ "$DIRECTORY" == "$_excl"* ]]; then
            ntfy_log INFO "CWD '$DIRECTORY' matches exclusion '$_excl', skipping"
            exit 0
        fi
    done
fi

# Deduplication
COOLDOWN_DIR="${XDG_RUNTIME_DIR:-/tmp}/tap-to-tmux-cooldown"
mkdir -p "$COOLDOWN_DIR"
COOLDOWN_FILE="${COOLDOWN_DIR}/${PROJECT:-unknown}"

if [[ -f "$COOLDOWN_FILE" ]]; then
    last_sent=$(cat "$COOLDOWN_FILE")
    now=$(date +%s)
    elapsed=$(( now - last_sent ))
    if [[ "$elapsed" -lt "$COOLDOWN_SECONDS" ]]; then
        ntfy_log INFO "Cooldown active for ${PROJECT}: ${elapsed}s < ${COOLDOWN_SECONDS}s"
        exit 0
    fi
fi

date +%s > "$COOLDOWN_FILE"

# Tmux session discovery
TMUX_SESSION=""
TMUX_PANE_INDEX=""
TMUX_WINDOW_INDEX=""
_pid=$$
while [[ "$_pid" -gt 1 ]]; do
    _match=$(tmux list-panes -a -F '#{pane_pid} #{session_name} #{pane_index} #{window_index}' 2>/dev/null \
        | awk -v pid="$_pid" '$1 == pid && $2 !~ /^mob-/ {print $2, $3, $4}')
    if [[ -n "$_match" ]]; then
        TMUX_SESSION=$(echo "$_match" | awk '{print $1}')
        TMUX_PANE_INDEX=$(echo "$_match" | awk '{print $2}')
        TMUX_WINDOW_INDEX=$(echo "$_match" | awk '{print $3}')
        break
    fi
    _pid=$(ps -o ppid= -p "$_pid" 2>/dev/null | tr -d ' ')
done

if [[ -z "$TMUX_SESSION" ]]; then
    TMUX_SESSION="$PROJECT"
fi

# Build deep link
BLINK_LINK=$(build_blink_url "$TMUX_SESSION" "$TMUX_PANE_INDEX")

# Build notification
case "$NOTIFICATION_TYPE" in
    idle_prompt)
        TITLE="${MACHINE}/${PROJECT} [oc]: Waiting for Input"
        PRIORITY="default"
        TAGS="hourglass,opencode,${MACHINE}"
        BODY="${MESSAGE:-OpenCode needs input}"
        ;;
    stop)
        TITLE="${MACHINE}/${PROJECT} [oc]: Done"
        PRIORITY="default"
        TAGS="white_check_mark,opencode,${MACHINE}"
        BODY="${MESSAGE:-Session finished}"
        ;;
    *)
        TITLE="${MACHINE}/${PROJECT} [oc]: Needs Attention"
        PRIORITY="default"
        TAGS="bell,opencode,${MACHINE}"
        BODY="${MESSAGE:-OpenCode needs attention}"
        ;;
esac

ntfy_log INFO "Sending: '${TITLE}'"
send_ntfy_notification "$TITLE" "$PRIORITY" "$TAGS" "$BODY" "$BLINK_LINK" &

exit 0