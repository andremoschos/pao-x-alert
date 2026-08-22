from pathlib import Path

PATH = Path('.github/workflows/fast-2min-watch.yml')

text = PATH.read_text(encoding='utf-8')

if 'already $state; no disable needed' in text:
    print('Cutover already repaired')
    raise SystemExit(0)

lines = text.splitlines(keepends=True)
start = None

for idx in range(len(lines) - 4):
    if (
        lines[idx].strip() == 'for wf in "${workflows[@]}"; do'
        and lines[idx + 1].strip() == 'gh api \\'
        and lines[idx + 2].strip() == '--method PUT \\'
        and lines[idx + 3].strip() == '"repos/$GITHUB_REPOSITORY/actions/workflows/$wf/disable"'
        and lines[idx + 4].strip() == 'done'
    ):
        start = idx
        break

if start is None:
    raise SystemExit('Target disable block not found; refusing broad edit')

raw = lines[start]
indent = raw[: len(raw) - len(raw.lstrip())]
bs = chr(92)

replacement = [
    f'{indent}for wf in "${{workflows[@]}}"; do\n',
    f'{indent}  state="$(\n',
    f'{indent}    gh api {bs}\n',
    f'{indent}      "repos/$GITHUB_REPOSITORY/actions/workflows/$wf" {bs}\n',
    f"{indent}      --jq '.state'\n",
    f'{indent}  )"\n',
    '\n',
    f'{indent}  if [ "$state" = "active" ]; then\n',
    f'{indent}    gh api {bs}\n',
    f'{indent}      --method PUT {bs}\n',
    f'{indent}      "repos/$GITHUB_REPOSITORY/actions/workflows/$wf/disable"\n',
    f'{indent}    echo "$wf disabled for fast cutover"\n',
    f'{indent}  else\n',
    f'{indent}    echo "$wf already $state; no disable needed"\n',
    f'{indent}  fi\n',
    f'{indent}done\n',
]

lines[start : start + 5] = replacement
PATH.write_text(''.join(lines), encoding='utf-8')
print('Cutover repaired safely')
