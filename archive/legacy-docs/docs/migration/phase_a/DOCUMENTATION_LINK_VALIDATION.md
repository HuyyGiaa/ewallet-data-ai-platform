# Phase A documentation link validation

## Scope

- Repository root `README.md`.
- Central `docs/README.md`.
- EDAI-1 and EDAI-2 indexes.
- Runbook and evidence indexes.
- `docs/DOCUMENTATION_REGISTRY.csv` path consistency.

## Result

- Markdown files checked: 6.
- Repository-relative Markdown links checked: 136.
- Missing or repository-escaping link targets: 0.
- Documentation registry rows: 99.
- Registry paths missing from this worktree: 0.
- Registry status counts: `CURRENT=5`, `HISTORICAL=30`, `PARTIAL=35`,
  `PROPOSED=1`, `UNKNOWN=28`.

External HTTP links and in-page anchors were excluded from filesystem resolution.
This is a source-tree link check, not an availability test for external services.

## Reproduction command

Run from the repository root:

```bash
python3 - <<'PY'
from pathlib import Path
import re
import urllib.parse

root = Path('.').resolve()
files = [
    Path('README.md'),
    Path('docs/README.md'),
    Path('docs/edai1/README.md'),
    Path('docs/edai2/README.md'),
    Path('docs/runbooks/README.md'),
    Path('docs/evidence/README.md'),
]
pattern = re.compile(r'(?<!!)\[[^\]]+\]\(([^)]+)\)')
issues = []

for path in files:
    for raw in pattern.findall(path.read_text(encoding='utf-8')):
        target = raw.strip().split()[0].strip('<>')
        if target.startswith(('http://', 'https://', 'mailto:', '#')):
            continue
        target = urllib.parse.unquote(target.split('#', 1)[0])
        if not target:
            continue
        resolved = (path.parent / target).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            issues.append((str(path), raw, 'escapes repository'))
            continue
        if not resolved.exists():
            issues.append((str(path), raw, 'missing'))

for issue in issues:
    print('|'.join(issue))
raise SystemExit(1 if issues else 0)
PY
```

The validation does not modify documentation or evidence.
