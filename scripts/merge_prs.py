#!/usr/bin/env python3
"""Chronological three-way, game-entry-aware preview merge for PureDOS DATs."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

UPSTREAM = 'PureDOS/DAT'
ENTRY_TAGS = {'game', 'machine'}


def git(*args, check=True):
    p = subprocess.run(['git', *args], capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p


def blob(ref, path):
    p = subprocess.run(['git', 'show', f'{ref}:{path}'], capture_output=True)
    return p.stdout if p.returncode == 0 else None


def relevant(path):
    p = Path(path)
    return p.suffix.lower() == '.xml' and (len(p.parts) == 1 or (len(p.parts) == 2 and p.parts[0].lower() == 'unverified'))


def parse(data):
    if data is None:
        return None
    # Reject DTD/entity declarations; these are unnecessary for DAT entries.
    if re.search(rb'<!\s*(DOCTYPE|ENTITY)\b', data, re.I):
        raise ValueError('DTD/entity declaration is unsupported')
    root = ET.fromstring(data)
    if root.tag != 'datafile':
        raise ValueError(f'expected <datafile>, got <{root.tag}>')
    entries = {}
    for child in root:
        if child.tag not in ENTRY_TAGS:
            continue
        key = (child.tag, child.get('name'))
        if not key[1] or key in entries:
            raise ValueError(f'missing or duplicate game/machine name: {key}')
        entries[key] = child
    return root, entries


def canonical(node):
    if node is None:
        return None
    def norm(el):
        return (el.tag, tuple(sorted(el.attrib.items())), (el.text or '').strip(),
                tuple(norm(x) for x in el), (el.tail or '').strip() if len(el) == 0 else '')
    return norm(node)


def replace_entries(original, changes):
    """Change only game/machine blocks; retain original header and other XML text."""
    text = original.decode('utf-8-sig')
    newline = '\r\n' if '\r\n' in text else '\n'
    # Match complete top-level game/machine blocks. No nested game/machine blocks expected.
    rx = re.compile(r'(?P<indent>^[ \t]*)<(?P<tag>game|machine)\b[^>]*>.*?^[ \t]*</(?P=tag)>[ \t]*(?:\r?\n|$)', re.M | re.S)
    spans = []
    found = set()
    for m in rx.finditer(text):
        try:
            node = ET.fromstring(m.group(0).strip())
        except ET.ParseError as exc:
            raise ValueError(f'cannot isolate entry block: {exc}') from exc
        key = (node.tag, node.get('name'))
        if key in found:
            raise ValueError(f'duplicate source entry {key}')
        found.add(key)
        if key in changes:
            replacement = changes[key]
            if replacement is None:
                out = ''
            else:
                out = serialize_entry(replacement, m.group('indent'), newline)
            spans.append((m.start(), m.end(), out))
    missing = [key for key in changes if key not in found and changes[key] is None]
    if missing:
        raise ValueError(f'deletion target missing: {missing[:3]}')
    for start, end, value in reversed(spans):
        text = text[:start] + value + text[end:]
    additions = [serialize_entry(v, '  ', newline) for k, v in changes.items() if k not in found and v is not None]
    if additions:
        match = list(re.finditer(r'^[ \t]*</datafile\s*>', text, re.M))
        if len(match) != 1:
            raise ValueError('could not locate unique closing </datafile>')
        pos = match[0].start()
        text = text[:pos] + ''.join(additions) + text[pos:]
    result = text.encode('utf-8')
    parse(result)
    return result


def serialize_entry(node, indent, newline):
    el = copy.deepcopy(node)
    ET.indent(el, space='  ')
    s = ET.tostring(el, encoding='unicode', short_empty_elements=True)
    return ''.join(indent + line + newline for line in s.splitlines())


def write(path, data):
    p = Path(path)
    if data is None:
        p.unlink(missing_ok=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def process(base, head, path, report, owners, pr_number):
    old, incoming = blob(base, path), blob(head, path)
    current = Path(path).read_bytes() if Path(path).exists() else None
    if old == incoming:
        return
    try:
        a = parse(old)
        b = parse(incoming)
        c = parse(current)
    except (ValueError, ET.ParseError) as exc:
        report['conflicts'].append({'file': path, 'entry': '(file)', 'reason': f'XML validation: {exc}'})
        return
    if incoming is None or old is None:
        if current == old:
            write(path, incoming)
            report['applied'] += 1
        elif current != incoming:
            report['conflicts'].append({'file': path, 'entry': '(file)', 'reason': 'file addition/deletion conflicts with existing file'})
        return
    # A PR that changes non-game metadata must not silently lose those changes.
    def metadata(root):
        return (root.tag, tuple(sorted(root.attrib.items())),
                tuple(canonical(x) for x in root if x.tag not in ENTRY_TAGS))
    if metadata(a[0]) != metadata(b[0]):
        report['conflicts'].append({'file': path, 'entry': '(metadata)', 'reason': 'PR modifies DAT header or non-game XML; manual review needed'})
    keys = set(a[1]) | set(b[1])
    changes = {}
    for key in sorted(keys):
        before = canonical(a[1].get(key))
        after = canonical(b[1].get(key))
        if before == after:
            continue
        present = canonical(c[1].get(key)) if c else None
        if present == after:
            continue
        owner_key = (path, key)
        if present != before and owner_key not in owners:
            report['conflicts'].append({'file': path, 'entry': key[1], 'reason': 'differs from PR base and was not changed by an earlier processed PR; skipped to protect upstream'})
            continue
        if present != before and owner_key in owners:
            report['overrides'].append({'file': path, 'entry': key[1], 'previous_pr': owners[owner_key], 'winning_pr': pr_number})
        changes[key] = b[1].get(key)
    if changes:
        try:
            result = replace_entries(current, changes)
            write(path, result)
            report['applied'] += len(changes)
            for key in changes:
                owners[(path, key)] = pr_number
        except (ValueError, ET.ParseError, UnicodeError) as exc:
            report['conflicts'].append({'file': path, 'entry': '(file)', 'reason': f'could not safely rewrite XML: {exc}'})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--report', default='pr-preview-report.json')
    args = ap.parse_args()
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        sys.exit('GH_TOKEN/GITHUB_TOKEN required')
    import urllib.request
    url = f'https://api.github.com/repos/{UPSTREAM}/pulls?state=open&per_page=100&sort=created&direction=asc'
    prs = []
    while url:
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json', 'User-Agent': 'puredos-pr-preview'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            prs.extend(json.load(resp))
            link = resp.headers.get('Link', '')
            url = next((x.split(';')[0].strip(' <>') for x in link.split(',') if 'rel="next"' in x), None)
    report = {'upstream': UPSTREAM, 'ordering': 'PR head commit timestamp (oldest first), then PR number', 'prs': [], 'conflicts': [], 'overrides': [], 'applied': 0}
    owners = {}
    queued = []
    for pr in prs:
        n = pr['number']
        fetch = git('fetch', '--no-tags', 'upstream', f'refs/pull/{n}/head:refs/remotes/upstream/pr-{n}', check=False)
        if fetch.returncode:
            report['conflicts'].append({'file': '(PR)', 'entry': str(n), 'reason': f'fetch failed: {fetch.stderr.strip()[:300]}'})
            continue
        head = f'refs/remotes/upstream/pr-{n}'
        timestamp = git('show', '-s', '--format=%cI', head).stdout.strip()
        queued.append((timestamp, n, pr, head))
    for timestamp, n, pr, head in sorted(queued, key=lambda x: (x[0], x[1])):
        entry = {'number': n, 'title': pr['title'], 'url': pr['html_url'], 'head_commit_date': timestamp, 'applied_entries': 0, 'conflicts': 0, 'overrides': 0}
        before_applied, before_conflicts, before_overrides = report['applied'], len(report['conflicts']), len(report['overrides'])
        base_result = git('merge-base', 'upstream/main', head, check=False)
        if base_result.returncode:
            report['conflicts'].append({'file': '(PR)', 'entry': str(n), 'reason': 'cannot find merge base'})
        else:
            base = base_result.stdout.strip()
            paths = git('diff', '--name-only', '--diff-filter=ACDMRT', base, head).stdout.splitlines()
            for path in paths:
                if relevant(path):
                    process(base, head, path, report, owners, n)
        entry['applied_entries'] = report['applied'] - before_applied
        entry['conflicts'] = len(report['conflicts']) - before_conflicts
        entry['overrides'] = len(report['overrides']) - before_overrides
        report['prs'].append(entry)
        print(f"PR #{n} ({timestamp}): {entry['applied_entries']} applied, {entry['overrides']} overrides, {entry['conflicts']} conflicts", flush=True)
    # Final validation of every relevant XML, including files not changed by PRs.
    report['invalid_files'] = []
    for path in Path('.').glob('*.xml'):
        try: parse(path.read_bytes())
        except (ValueError, ET.ParseError) as exc: report['invalid_files'].append({'file': str(path), 'error': str(exc)})
    if Path('unverified').exists():
        for path in Path('unverified').glob('*.xml'):
            try: parse(path.read_bytes())
            except (ValueError, ET.ParseError) as exc: report['invalid_files'].append({'file': str(path), 'error': str(exc)})
    Path(args.report).write_text(json.dumps(report, indent=2) + '\n')
    with open('pr-preview-summary.md', 'w') as f:
        f.write(f"# PureDOS PR preview\n\nOpen PRs examined: {len(prs)}  \nEntries/files applied: {report['applied']}  \nConflicts: {len(report['conflicts'])}  \nInvalid XML files: {len(report['invalid_files'])}\n\n")
        f.write('| PR | Head commit date | Applied | Overrides | Conflicts |\n|---|---|---:|---:|---:|\n')
        for item in report['prs']:
            f.write(f"| [#{item['number']}]({item['url']}) | {item['head_commit_date']} | {item['applied_entries']} | {item['overrides']} | {item['conflicts']} |\n")
        f.write('\n## Chronological overrides\n\n')
        for x in report['overrides']:
            f.write(f"- `{x['file']}` / `{x['entry']}`: PR #{x['winning_pr']} superseded PR #{x['previous_pr']}\n")
        f.write('\n## Conflicts\n\n')
        for x in report['conflicts']:
            f.write(f"- `{x['file']}` / `{x['entry']}`: {x['reason']}\n")
        for x in report['invalid_files']:
            f.write(f"- INVALID `{x['file']}`: {x['error']}\n")
    if report['invalid_files']:
        sys.exit('Invalid XML detected; refusing to publish')


if __name__ == '__main__':
    main()