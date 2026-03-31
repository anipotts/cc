#!/usr/bin/env bash
# cc roster — instant multi-session overview
# Usage: bash roster.sh [cwd]
# Scans /tmp/claude-{uid}/ for live sessions, enriches with team file metadata.

uid=$(id -u)
base="/tmp/claude-${uid}"
my_cwd="${1:-$(pwd)}"
my_project=$(basename "$my_cwd")
teams_dir="$HOME/.claude/cc/teams"

[[ -d "$base" ]] || { echo "No active sessions."; exit 0; }

# --- Collect sessions from /tmp ---
declare -A proj_count
declare -A proj_encoded
declare -A proj_worktrees
total=0

for pdir in "$base"/-*/; do
    [[ -d "$pdir" ]] || continue
    enc=$(basename "$pdir")
    count=0
    for sdir in "$pdir"*/; do [[ -d "$sdir" ]] && ((count++)); done
    (( count == 0 )) && continue
    total=$((total + count))

    is_wt=0
    [[ "$enc" == *"claude-worktrees"* ]] && is_wt=1

    # --- Resolve project name ---
    # Strategy: try decoding the /tmp path back to a real directory.
    # The encoding is cwd.replace("/", "-"), so we reverse it by trying
    # progressively from the full path. This handles hyphens in dir names.
    proj=""
    if (( is_wt )); then
        parent_enc="${enc%%--claude-worktrees*}"
        wt_name="${enc##*claude-worktrees-}"
    else
        parent_enc="$enc"
    fi

    # Resolve project name: check team files first (they store the real cwd),
    # then try decoding the /tmp path, then fallback to last segment.
    for tf in "$teams_dir"/*/config.json; do
        [[ -f "$tf" ]] || continue
        tf_cwd=$(jq -r '.members[0].cwd // empty' "$tf" 2>/dev/null)
        [[ -z "$tf_cwd" ]] && continue
        tf_enc=$(echo "$tf_cwd" | tr '/' '-')
        if [[ "$tf_enc" == "$parent_enc" ]]; then
            proj=$(basename "$tf_cwd")
            break
        fi
    done

    if [[ -z "$proj" ]]; then
        # Try progressively joining last N segments as the basename
        IFS='-' read -ra segs <<< "${parent_enc#-}"
        n=${#segs[@]}
        for join_count in 1 2 3; do
            (( join_count > n )) && break
            start=$((n - join_count))
            candidate=$(IFS=-; echo "${segs[*]:$start}")
            prefix_path="/"
            for ((k=0; k<start; k++)); do prefix_path+="${segs[$k]}/"; done
            if [[ -d "${prefix_path}${candidate}" ]]; then
                proj="$candidate"
                break
            fi
            # Also try with dots (for domain-style names like anipotts.com)
            if (( join_count == 2 )); then
                dot_candidate="${segs[$start]}.${segs[$((start+1))]}"
                if [[ -d "${prefix_path}${dot_candidate}" ]]; then
                    proj="$dot_candidate"
                    break
                fi
            fi
        done
        [[ -z "$proj" ]] && proj="${segs[$((n-1))]}"
    fi

    # --- Aggregate ---
    if (( is_wt )); then
        # Find parent project and add worktree info
        proj_worktrees["$proj"]+="${wt_name:-unknown} "
        proj_count["$proj"]=$(( ${proj_count["$proj"]:-0} + count ))
    else
        proj_count["$proj"]=$(( ${proj_count["$proj"]:-0} + count ))
    fi
    proj_encoded["$proj"]+="$enc "
done

(( total == 0 )) && { echo "No active sessions."; exit 0; }

echo "cc — ${total} sessions across ${#proj_count[@]} projects"
echo ""

# --- Sort: current project first, then by count descending ---
sorted=()
for proj in "${!proj_count[@]}"; do
    [[ "$proj" == "$my_project" ]] && { sorted=("$proj" "${sorted[@]}"); continue; }
    sorted+=("$proj")
done

if (( ${#sorted[@]} > 1 )); then
    first="${sorted[0]}"
    rest=()
    for proj in $(for p in "${sorted[@]:1}"; do
        echo "${proj_count[$p]} $p"
    done | sort -rn | awk '{print $2}'); do
        rest+=("$proj")
    done
    sorted=("$first" "${rest[@]}")
fi

# --- Render ---
for proj in "${sorted[@]}"; do
    count=${proj_count[$proj]}
    marker=""
    [[ "$proj" == "$my_project" ]] && marker="  ← YOU ARE HERE"

    echo "  ${proj} (${count})${marker}"

    # Team file metadata
    tf="${teams_dir}/${proj}/config.json"
    if [[ -f "$tf" ]] && command -v jq &>/dev/null; then
        mapfile -t members < <(jq -r '.members[]? | "\(.name // "?")\t\(.branch // "")\t\(.files // [] | .[-3:] | join(", "))\t\(.task // "")"' "$tf" 2>/dev/null)
        nm=${#members[@]}
        has_wt="${proj_worktrees[$proj]:-}"
        for ((i=0; i<nm; i++)); do
            IFS=$'\t' read -r name branch files task <<< "${members[$i]}"
            conn="├"
            (( i == nm - 1 )) && [[ -z "$has_wt" ]] && conn="└"
            [[ ${#task} -gt 50 ]] && task="${task:0:50}…"
            line="  ${conn} ${name}"
            [[ -n "$branch" ]] && line+="  ${branch}"
            [[ -n "$files" && "$files" != "null" && -n "$files" ]] && line+="  ${files}"
            [[ -n "$task" ]] && line+="  \"${task}\""
            echo "$line"
        done
    fi

    # Worktrees
    if [[ -n "${proj_worktrees[$proj]:-}" ]]; then
        wts=(${proj_worktrees[$proj]})
        for ((i=0; i<${#wts[@]}; i++)); do
            conn="├"; (( i == ${#wts[@]} - 1 )) && conn="└"
            echo "  ${conn} worktree  ${wts[$i]}"
        done
    fi

    echo ""
done
