#!/usr/bin/env bash
# cc roster — reads Claude Code's native session registry
# Usage: bash roster.sh [cwd]

claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
sessions_dir="${claude_dir}/sessions"
enrich_dir="${claude_dir}/cc/enrich"
my_cwd="${1:-$(pwd)}"
my_project=$(basename "$my_cwd")

[[ -d "$sessions_dir" ]] || { echo "No active sessions."; exit 0; }

# Collect all live session data into a temp file (avoids bash 3 assoc array issues)
tmpfile=$(mktemp)
trap 'rm -f "$tmpfile"' EXIT

total=0; busy_total=0

for sf in "$sessions_dir"/*.json; do
    [[ -f "$sf" ]] || continue
    pid=$(basename "$sf" .json)
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    ps -p "$pid" &>/dev/null || continue
    ((total++))

    name=$(jq -r '.name // .kind // "session"' "$sf" 2>/dev/null)
    scwd=$(jq -r '.cwd // ""' "$sf" 2>/dev/null)
    sid=$(jq -r '.sessionId // ""' "$sf" 2>/dev/null)
    proj=$(basename "$scwd")

    cpu=$(ps -p "$pid" -o %cpu= 2>/dev/null | tr -d ' ')
    is_busy=0
    (( $(echo "${cpu:-0} > 5" | bc 2>/dev/null || echo 0) )) && { is_busy=1; ((busy_total++)); }

    files="" task=""
    ef="${enrich_dir}/${sid}.json"
    if [[ -f "$ef" ]]; then
        files=$(jq -r '(.files // [])[-3:] | join(", ")' "$ef" 2>/dev/null)
        task=$(jq -r '.task // ""' "$ef" 2>/dev/null)
    fi

    # Truncate
    [[ ${#name} -gt 25 ]] && name="${name:0:22}..."
    [[ ${#task} -gt 45 ]] && task="${task:0:42}..."

    status="·"; (( is_busy )) && status="▶"

    # Write to temp file: proj|status|name|files|task
    echo "${proj}|${status}|${name}|${files}|${task}" >> "$tmpfile"
done

(( total == 0 )) && { echo "No active sessions."; exit 0; }

idle_total=$((total - busy_total))
echo "cc — ${total} sessions (${busy_total} busy, ${idle_total} idle)"
echo ""

# Get unique projects, current first
projects=()
if grep -q "^${my_project}|" "$tmpfile" 2>/dev/null; then
    projects+=("$my_project")
fi
while IFS= read -r p; do
    [[ "$p" != "$my_project" ]] && projects+=("$p")
done < <(cut -d'|' -f1 "$tmpfile" | sort | uniq -c | sort -rn | awk '{print $2}')

for proj in "${projects[@]}"; do
    entries=$(grep "^${proj}|" "$tmpfile")
    count=$(echo "$entries" | wc -l | tr -d ' ')
    marker=""
    [[ "$proj" == "$my_project" ]] && marker="  ← YOU ARE HERE"

    echo "  ${proj} (${count})${marker}"

    i=0
    while IFS='|' read -r _proj status name files task; do
        [[ -z "$name" || "$name" == "session" ]] && { ((i++)); continue; }
        ((i++))
        conn="├"; (( i >= count )) && conn="└"
        line="  ${conn} ${status} ${name}"
        [[ -n "$files" ]] && line+="  ${files}"
        [[ -n "$task" ]] && line+="  \"${task}\""
        echo "$line"
    done <<< "$entries"

    echo ""
done
