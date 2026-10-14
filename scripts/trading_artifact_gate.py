#!/usr/bin/env python3
"""Prepare trusted release evidence locally; verify a Git-free export remotely."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

MANIFEST = 'release-provenance.json'
MARKER = 'market-build.json'


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def hashes(root, source=False):
    result = {}
    total = 0
    for path in sorted(root.rglob('*')):
        name = path.relative_to(root).as_posix()
        if source and (name == MANIFEST or name.startswith('frontend/dist/')):
            continue
        if path.is_symlink():
            raise ValueError(f'symlink forbidden: {name}')
        if path.is_file():
            total += path.stat().st_size
            if len(result) >= 50000 or total > 1024 ** 3:
                raise ValueError('export exceeds verification bounds')
            result[name] = digest(path)
    return result


def binding(tag, commit):
    match = re.fullmatch(r'(\d{14})-([0-9a-f]{7,40})', tag)
    if not match or not re.fullmatch(r'[0-9a-f]{40}', commit) or not commit.startswith(match[2]):
        raise ValueError('release tag must bind to the exact command commit')
    datetime.strptime(match[1], '%Y%m%d%H%M%S')


def prepare(repo, commit_arg, tag, output):
    commit = run('git', 'rev-parse', '--verify', commit_arg + '^{commit}', cwd=repo).decode().strip()
    binding(tag, commit)
    if output.exists():
        raise ValueError('export destination must not exist')
    with tempfile.TemporaryDirectory(prefix='market-provenance-') as directory:
        stage = Path(directory) / 'release'
        stage.mkdir()
        # Use a single Git archive process; never extract arbitrary tar paths or links.
        with tempfile.TemporaryFile() as stream:
            subprocess.run(['git', 'archive', '--format=tar', commit], cwd=repo, stdout=stream, check=True)
            stream.seek(0)
            with tarfile.open(fileobj=stream) as archive:
                count = total = 0
                for member in archive:
                    name = member.name
                    if Path(name).is_absolute() or '..' in Path(name).parts:
                        raise ValueError('unsafe archive path')
                    if member.isdir() or name.startswith('frontend/dist/'):
                        continue
                    count += 1
                    total += member.size
                    if not member.isfile() or count > 50000 or total > 1024 ** 3:
                        raise ValueError('export requires bounded regular committed files')
                    target = stage / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source_file, target.open('wb') as destination:
                        shutil.copyfileobj(source_file, destination)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
        frontend = stage / 'frontend'
        if (frontend / 'public' / MARKER).exists():
            raise ValueError('build marker must be generated after build, outside public')
        for name, value in hashes(frontend).items():
            if name.startswith('tests/'):
                continue
            local = repo / 'frontend' / name
            if not local.is_file() or local.is_symlink() or digest(local) != value:
                raise ValueError(f'frontend differs from command commit: {name}')
        for subdir in ('src', 'public'):
            if hashes(repo / 'frontend' / subdir) != hashes(frontend / subdir):
                raise ValueError('uncommitted frontend source/public')
        source = hashes(stage, source=True)
        subprocess.run(['npm', 'ci', '--no-audit', '--no-fund'], cwd=frontend, check=True)
        subprocess.run(['npm', 'run', 'build'], cwd=frontend, check=True)
        assets = hashes(frontend / 'dist')
        local_assets = hashes(repo / 'frontend/dist')
        local_assets.pop(MARKER, None)
        if not assets.get('index.html') or assets != local_assets:
            raise ValueError('dist differs from clean committed-source rebuild')
        shutil.rmtree(frontend / 'node_modules', ignore_errors=True)
        if hashes(stage, source=True) != source:
            raise ValueError('build modified exported committed inputs')
        marker = {'commit': commit, 'tag': tag, 'assets_sha256': assets}
        (frontend / 'dist' / MARKER).write_text(json.dumps(marker, sort_keys=True) + '\n')
        manifest = {'schema': 1, 'commit': commit, 'tag': tag, 'source_sha256': source,
                    'dist_sha256': hashes(frontend / 'dist'), 'verification': 'clean-committed-rebuild'}
        (stage / MANIFEST).write_text(json.dumps(manifest, sort_keys=True) + '\n')
        shutil.copytree(stage, output)
    return {'commit': commit, 'tag': tag, 'manifest_sha256': digest(output / MANIFEST)}


def verify_export(stage, commit, tag, expected_digest):
    # Short Git SHAs remain supported, but evidence carries the full resolved commit.
    if not re.fullmatch(r'[0-9a-f]{7,40}', commit):
        raise ValueError('invalid command commit')
    if not re.fullmatch(r'[0-9a-f]{64}', expected_digest) or digest(stage / MANIFEST) != expected_digest:
        raise ValueError('manifest differs from trusted pipeline digest')
    manifest = json.loads((stage / MANIFEST).read_text())
    full_commit = manifest.get('commit', '')
    binding(tag, full_commit)
    if manifest.get('schema') != 1 or not full_commit.startswith(commit) or manifest.get('tag') != tag:
        raise ValueError('manifest release binding mismatch')
    if hashes(stage, source=True) != manifest.get('source_sha256'):
        raise ValueError('exported source differs from trusted manifest')
    assets = hashes(stage / 'frontend/dist')
    if assets != manifest.get('dist_sha256') or 'index.html' not in assets:
        raise ValueError('dist differs from trusted manifest')
    marker = json.loads((stage / 'frontend/dist' / MARKER).read_text())
    assets.pop(MARKER, None)
    if marker != {'commit': full_commit, 'tag': tag, 'assets_sha256': assets}:
        raise ValueError('compiled marker release/assets mismatch')
    return {'commit': full_commit, 'tag': tag, 'manifest_sha256': expected_digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'verify'])
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--commit', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args()
    try:
        if args.mode == 'prepare':
            if args.output is None:
                raise ValueError('--output required')
            result = prepare(args.repo.resolve(), args.commit, args.tag, args.output.resolve())
        else:
            if args.manifest_sha256 is None:
                raise ValueError('--manifest-sha256 from trusted local pipeline required')
            result = verify_export(args.repo.resolve(), args.commit, args.tag, args.manifest_sha256)
        print(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Provenance rejected: {exc}\n')


if __name__ == '__main__':
    main()
