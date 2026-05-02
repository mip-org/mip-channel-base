#!/usr/bin/env python3
"""
Auto-update package versions by detecting new upstream tags.

For each package directory under packages/:
1. Find the reference recipe (highest-numeric version dir or first branch dir).
2. Skip if the reference recipe sets `auto_update: false`.
3. Query upstream git tags via `git ls-remote --tags`.
4. Determine the tag pattern: explicit `tag_pattern`, inferred from the
   reference recipe's `source.branch`, or auto-detected from upstream tags.
5. Find numeric versions newer than the highest existing numeric dir.
6. Create `packages/<name>/<version>/` for each new version, with
   updated `recipe.yaml` (`source.branch` -> tag) and `mip.yaml`
   (`version` -> the new numeric version, if a channel-side mip.yaml exists).
"""

import os
import sys
import re
import shutil
import subprocess
import argparse
import yaml
from packaging.version import Version, InvalidVersion


VERSION_TAG_RE = re.compile(r'^(v?)(\d+(?:\.\d+){0,2})$')


def is_numeric_version(s):
    """Return True if s is a dot-separated numeric version (e.g. '1.2.3')."""
    if not s:
        return False
    parts = s.split('.')
    return all(p.isdigit() for p in parts) and len(parts) >= 1


def list_release_dirs(package_dir):
    """List release dir names that contain a recipe.yaml."""
    result = []
    for name in sorted(os.listdir(package_dir)):
        full = os.path.join(package_dir, name)
        if not os.path.isdir(full):
            continue
        if not os.path.exists(os.path.join(full, 'recipe.yaml')):
            continue
        result.append(name)
    return result


def pick_reference_dir(release_dirs):
    """Pick the reference release dir: highest-numeric, else first non-numeric."""
    numeric = [d for d in release_dirs if is_numeric_version(d)]
    if numeric:
        return max(numeric, key=Version)
    if release_dirs:
        return release_dirs[0]
    return None


def load_yaml(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f) or {}


def list_remote_tags(git_url):
    """Return list of tag short-names from git ls-remote --tags."""
    proc = subprocess.run(
        ['git', 'ls-remote', '--tags', git_url],
        capture_output=True, text=True, check=True,
    )
    tags = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            _hash, ref = line.split('\t', 1)
        except ValueError:
            continue
        if not ref.startswith('refs/tags/'):
            continue
        # Annotated-tag dereference suffix points to the same logical tag.
        if ref.endswith('^{}'):
            continue
        tags.append(ref[len('refs/tags/'):])
    return tags


def detect_pattern_from_tags(tags):
    """Return the dominant `v{version}` vs `{version}` shape, or None."""
    v_count = 0
    bare_count = 0
    for tag in tags:
        m = VERSION_TAG_RE.match(tag)
        if not m:
            continue
        if m.group(1) == 'v':
            v_count += 1
        else:
            bare_count += 1
    if v_count == 0 and bare_count == 0:
        return None
    return 'v{version}' if v_count >= bare_count else '{version}'


def infer_pattern_from_recipe(recipe, ref_version):
    """Locate ref_version inside source.branch and substitute {version}."""
    source = recipe.get('source') or {}
    branch = source.get('branch')
    if not branch or not ref_version:
        return None
    if ref_version not in branch:
        return None
    return branch.replace(ref_version, '{version}')


def resolve_pattern(reference_recipe, ref_version, upstream_tags):
    """Return (pattern_string, source_kind) where source_kind is one of
    'explicit', 'inferred', 'detected', or (None, None) if no pattern."""
    explicit = reference_recipe.get('tag_pattern')
    if explicit:
        return explicit, 'explicit'

    if ref_version and is_numeric_version(ref_version):
        inferred = infer_pattern_from_recipe(reference_recipe, ref_version)
        if inferred:
            return inferred, 'inferred'

    detected = detect_pattern_from_tags(upstream_tags)
    if detected:
        return detected, 'detected'

    return None, None


def pattern_to_regex(pattern):
    """Compile a tag pattern (with {version}) into a regex anchored full-match."""
    placeholder = '__VERSION_PLACEHOLDER__'
    escaped = re.escape(pattern.replace('{version}', placeholder))
    body = escaped.replace(placeholder, r'(\d+(?:\.\d+){0,2})')
    return re.compile('^' + body + '$')


def find_new_versions(tags, pattern, current_highest):
    """Return list of (version_str, tag) for tags newer than current_highest."""
    pat_re = pattern_to_regex(pattern)
    found = []
    for tag in tags:
        m = pat_re.match(tag)
        if not m:
            continue
        version_str = m.group(1)
        try:
            v = Version(version_str)
        except InvalidVersion:
            continue
        if current_highest is not None and v <= current_highest:
            continue
        found.append((v, version_str, tag))
    found.sort(key=lambda x: x[0])
    return [(vs, t) for _v, vs, t in found]


def update_recipe_yaml(path, new_branch):
    """Set source.branch in recipe.yaml to the resolved tag."""
    data = load_yaml(path)
    source = data.get('source')
    if not isinstance(source, dict):
        source = {}
        data['source'] = source
    source['branch'] = new_branch
    with open(path, 'w') as f:
        yaml.safe_dump(data, f, sort_keys=False)


def update_mip_yaml_if_present(path, new_version):
    """If a channel-side mip.yaml exists, set its version to new_version."""
    if not os.path.exists(path):
        return False
    data = load_yaml(path)
    data['version'] = str(new_version)
    with open(path, 'w') as f:
        yaml.safe_dump(data, f, sort_keys=False)
    return True


def process_package(package_dir, dry_run=False):
    package_name = os.path.basename(package_dir)
    print(f"\nProcessing package: {package_name}")

    release_dirs = list_release_dirs(package_dir)
    if not release_dirs:
        print(f"  No release dirs with recipe.yaml, skipping")
        return []

    ref_dir_name = pick_reference_dir(release_dirs)
    ref_path = os.path.join(package_dir, ref_dir_name)
    ref_recipe = load_yaml(os.path.join(ref_path, 'recipe.yaml'))

    if ref_recipe.get('auto_update') is False:
        print(f"  auto_update: false on reference recipe, skipping")
        return []

    source = ref_recipe.get('source') or {}
    git_url = source.get('git')
    if not git_url:
        print(f"  No source.git on reference recipe, skipping "
              f"(only git-sourced packages are auto-updated)")
        return []

    print(f"  Reference dir: {ref_dir_name}")
    print(f"  Upstream: {git_url}")

    try:
        tags = list_remote_tags(git_url)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or '').strip() if hasattr(e, 'stderr') else ''
        print(f"  Error listing remote tags: {e}\n    {stderr}")
        return []

    if not tags:
        print(f"  Upstream has no tags, skipping")
        return []

    ref_version = ref_dir_name if is_numeric_version(ref_dir_name) else None
    pattern, source_kind = resolve_pattern(ref_recipe, ref_version, tags)
    if not pattern:
        print(f"  Could not determine tag pattern, skipping")
        return []

    print(f"  Tag pattern ({source_kind}): {pattern}")

    numeric_dirs = [d for d in release_dirs if is_numeric_version(d)]
    current_highest = max((Version(d) for d in numeric_dirs), default=None)
    if current_highest is not None:
        print(f"  Current highest numeric version: {current_highest}")
    else:
        print(f"  No existing numeric versions")

    new_versions = find_new_versions(tags, pattern, current_highest)
    if not new_versions:
        print(f"  No new versions found")
        return []

    created = []
    for version_str, tag in new_versions:
        new_dir = os.path.join(package_dir, version_str)
        if os.path.exists(new_dir):
            print(f"  {version_str} already exists, skipping")
            continue
        verb = '[DRY RUN] Would create' if dry_run else 'Creating'
        print(f"  {verb}: packages/{package_name}/{version_str}/ "
              f"(from tag {tag})")
        if not dry_run:
            shutil.copytree(ref_path, new_dir)
            update_recipe_yaml(os.path.join(new_dir, 'recipe.yaml'), tag)
            mip_yaml_path = os.path.join(new_dir, 'mip.yaml')
            if update_mip_yaml_if_present(mip_yaml_path, version_str):
                print(f"    Updated mip.yaml version -> {version_str}")
        created.append((package_name, version_str, tag))

    return created


def process_all(packages_dir, dry_run=False):
    package_names = sorted(
        d for d in os.listdir(packages_dir)
        if os.path.isdir(os.path.join(packages_dir, d))
    )
    all_created = []
    for name in package_names:
        all_created.extend(
            process_package(os.path.join(packages_dir, name), dry_run))
    return all_created


def write_summary(path, created):
    with open(path, 'w') as f:
        if not created:
            f.write("No new package versions detected.\n")
            return
        f.write("Auto-update detected new upstream versions:\n\n")
        for name, version, tag in created:
            f.write(f"- `{name}` → `{version}` (upstream tag `{tag}`)\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='Print what would be done without writing files')
    parser.add_argument('--package', type=str,
                        help='Process only this package')
    parser.add_argument('--packages-dir', type=str,
                        help='Override packages directory (for testing)')
    parser.add_argument('--summary-file', type=str,
                        help='Write a Markdown summary of created versions')
    args = parser.parse_args()

    if args.packages_dir:
        packages_dir = args.packages_dir
    else:
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        packages_dir = os.path.join(project_root, 'packages')

    if not os.path.isdir(packages_dir):
        print(f"No packages directory at {packages_dir}, nothing to do")
        if args.summary_file:
            write_summary(args.summary_file, [])
        return 0

    if args.package:
        package_dir = os.path.join(packages_dir, args.package)
        if not os.path.isdir(package_dir):
            print(f"Package not found: {args.package}")
            return 1
        created = process_package(package_dir, dry_run=args.dry_run)
    else:
        created = process_all(packages_dir, dry_run=args.dry_run)

    print()
    if not created:
        print("No new versions detected.")
    else:
        print(f"Detected {len(created)} new version(s):")
        for name, version, tag in created:
            print(f"  {name} {version} (tag: {tag})")

    if args.summary_file:
        write_summary(args.summary_file, created)

    return 0


if __name__ == '__main__':
    sys.exit(main())
