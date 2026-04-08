#!/usr/bin/env python3
"""
Prepare package source directories from recipe.yaml specifications.

This script:
1. Reads recipe.yaml files from packages/
2. Computes source hashes for change detection
3. Checks if the package already exists in releases (caching)
4. Clones/downloads source code
5. Overlays channel-provided files (mip.yaml, compile.m, etc.)
6. Outputs prepared directories to build/prepared/

The prepared directories each contain a mip.yaml and are ready for
`mip bundle` to process.
"""

import os
import sys
import stat
import json
import shutil
import subprocess
import hashlib
import argparse
import requests
import yaml
from channel_config import get_github_repo, get_base_url, release_tag_from_mhl
from build_targets import CPU_LEVELS, is_simd_architecture, get_compiler_env


def _rmtree_on_error(func, path, exc_info):
    """Handle read-only files on Windows (e.g. .git/objects/pack)."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def clone_git_repository(url, destination, subdirectory=None, branch=None):
    """Clone a git repository, optionally extracting a subdirectory."""
    branch_args = ["--branch", branch] if branch else []
    if subdirectory:
        temp_clone_dir = destination + "_temp_clone"
        branch_info = f", branch: {branch}" if branch else ""
        print(f'  Cloning {url} (subdirectory: {subdirectory}{branch_info})...')
        subprocess.run(
            ["git", "clone"] + branch_args + [url, temp_clone_dir],
            check=True, capture_output=True
        )
        subdir_path = os.path.join(temp_clone_dir, subdirectory)
        if not os.path.isdir(subdir_path):
            shutil.rmtree(temp_clone_dir, onerror=_rmtree_on_error)
            raise ValueError(f"Subdirectory '{subdirectory}' not found in cloned repository")
        if destination == '.':
            for item in os.listdir(subdir_path):
                s = os.path.join(subdir_path, item)
                d = os.path.join('.', item)
                if os.path.isdir(s):
                    shutil.copytree(s, d)
                else:
                    shutil.copy2(s, d)
        else:
            shutil.copytree(subdir_path, destination)
        shutil.rmtree(temp_clone_dir, onerror=_rmtree_on_error)
    else:
        branch_info = f" (branch: {branch})" if branch else ""
        print(f'  Cloning {url}{branch_info}...')
        subprocess.run(
            ["git", "clone"] + branch_args + [url, destination],
            check=True, capture_output=True
        )

    # Remove .git directories
    for root, dirs, files in os.walk(destination):
        if ".git" in dirs:
            shutil.rmtree(os.path.join(root, ".git"), onerror=_rmtree_on_error)
            dirs.remove(".git")


def download_and_extract_zip(url, destination):
    """Download a ZIP file from a URL and extract it to destination."""
    import zipfile

    download_file = "temp_download.zip"
    print(f'  Downloading {url}...')
    response = requests.get(url, timeout=30)
    response.raise_for_status()

    with open(download_file, 'wb') as f:
        f.write(response.content)

    print(f"  Extracting to {destination}...")
    with zipfile.ZipFile(download_file, 'r') as zip_ref:
        zip_ref.extractall(destination)

    os.remove(download_file)


def resolve_git_commit_hash(url, ref):
    """Resolve a branch or tag to its commit hash via git ls-remote."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", url, ref],
            check=True, capture_output=True, text=True
        )
        for line in result.stdout.strip().splitlines():
            commit_hash, remote_ref = line.split('\t', 1)
            if remote_ref in (f"refs/heads/{ref}", f"refs/tags/{ref}", ref):
                return commit_hash
        raise RuntimeError(f"Could not resolve ref '{ref}' for {url}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git ls-remote failed for {url} {ref}: {e}") from e


def compute_directory_hash(directory):
    """Compute a deterministic SHA1 hash of a directory's contents."""
    sha1 = hashlib.sha1()
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        files.sort()
        for filename in files:
            file_path = os.path.join(root, filename)
            relative_path = os.path.relpath(file_path, directory)
            sha1.update(relative_path.encode('utf-8'))
            sha1.update(b'\0')
            try:
                with open(file_path, 'rb') as f:
                    while True:
                        chunk = f.read(8192)
                        if not chunk:
                            break
                        sha1.update(chunk)
            except (IOError, OSError) as e:
                sha1.update(f"ERROR:{e}".encode('utf-8'))
            sha1.update(b'\0')
    return sha1.hexdigest()


def overlay_channel_files(release_folder, target_dir):
    """Copy channel-provided files (everything except recipe.yaml) into target."""
    for item in os.listdir(release_folder):
        if item == 'recipe.yaml':
            continue
        src = os.path.join(release_folder, item)
        dst = os.path.join(target_dir, item)
        if os.path.isdir(src):
            if os.path.exists(dst):
                for root, dirs, files in os.walk(src):
                    rel_root = os.path.relpath(root, src)
                    dst_root = os.path.join(dst, rel_root)
                    os.makedirs(dst_root, exist_ok=True)
                    for f in files:
                        shutil.copy2(os.path.join(root, f), os.path.join(dst_root, f))
            else:
                shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def read_mip_yaml_architectures(mip_yaml_path):
    """Read mip.yaml and return list of architectures from all builds."""
    with open(mip_yaml_path, 'r') as f:
        mip_yaml = yaml.safe_load(f)
    archs = set()
    for build in mip_yaml.get('builds', []):
        for a in build.get('architectures', []):
            archs.add(a)
    return archs, mip_yaml


def check_existing_package(mhl_filename, source_hash, mip_yaml):
    """Check if package already exists in releases with matching source hash."""
    release_tag = release_tag_from_mhl(mhl_filename)
    base_url = get_base_url(release_tag)
    mip_json_url = f"{base_url}/{mhl_filename}.mip.json"

    try:
        response = requests.get(mip_json_url, timeout=10)
        if response.status_code == 404:
            print(f"  Package not found in releases")
            return False

        response.raise_for_status()
        existing = response.json()

        if existing.get('source_hash') != source_hash:
            print(f"  Source hash mismatch")
            return False

        # Compare key metadata
        for field in ('name', 'description', 'version', 'dependencies',
                      'homepage', 'repository', 'license'):
            if existing.get(field) != mip_yaml.get(field):
                print(f"  Metadata mismatch in '{field}'")
                return False

        # Compare release_number
        release_number = mip_yaml.get('release_number', 1)
        # Check build-level override
        for build in mip_yaml.get('builds', []):
            if 'release_number' in build:
                release_number = build['release_number']
                break
        if existing.get('release_number') != release_number:
            print(f"  Release number mismatch")
            return False

        print(f"  Package exists with matching metadata and source hash")
        return True

    except requests.RequestException as e:
        print(f"  Error checking existing package: {e}")
        return False


class PackagePreparer:
    """Prepares package source directories from recipe.yaml specifications."""

    def __init__(self, dry_run=False, force=False, output_dir=None,
                 architecture=None):
        self.dry_run = dry_run
        self.force = force
        self.architecture = architecture or os.environ.get(
            'BUILD_ARCHITECTURE', 'any')

        if output_dir:
            self.output_dir = output_dir
        else:
            project_root = os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))
            self.output_dir = os.path.join(project_root, 'build', 'prepared')

        if not self.dry_run:
            os.makedirs(self.output_dir, exist_ok=True)

    def _fetch_source(self, recipe, target_dir):
        """Fetch source code based on recipe.yaml into target_dir."""
        source = recipe.get('source')
        if not source:
            return  # Inline package

        original_dir = os.getcwd()
        os.chdir(target_dir)
        try:
            if 'git' in source:
                clone_git_repository(
                    url=source['git'],
                    destination='.',
                    subdirectory=source.get('subdirectory'),
                    branch=source.get('branch'),
                )
                for dir_name in source.get('remove_dirs', []):
                    dir_path = os.path.join(target_dir, dir_name)
                    if os.path.isdir(dir_path):
                        shutil.rmtree(dir_path, onerror=_rmtree_on_error)
                        print(f"    Removed directory: {dir_name}")
            elif 'zip' in source:
                download_and_extract_zip(source['zip'], '.')
        finally:
            os.chdir(original_dir)

    def prepare_package(self, package_dir, release=None):
        """Prepare a single package directory."""
        package_name = os.path.basename(package_dir)
        print(f"\nProcessing package: {package_name}")

        if '-' in package_name:
            print(f"  Error: Package name contains hyphens. Use underscores.")
            return False

        releases_path = os.path.join(package_dir, 'releases')
        if not os.path.isdir(releases_path):
            print(f"  No releases/ directory found")
            return True

        for release_version in sorted(os.listdir(releases_path)):
            if release is not None and release_version != release:
                continue

            release_folder = os.path.join(releases_path, release_version)
            if not os.path.isdir(release_folder):
                continue

            print(f"  Processing release: {release_version}")

            recipe_path = os.path.join(release_folder, 'recipe.yaml')
            if not os.path.exists(recipe_path):
                print(f"  Warning: No recipe.yaml found, skipping")
                continue

            with open(recipe_path, 'r') as f:
                recipe = yaml.safe_load(f) or {}

            # Compute source hash
            source_hash = compute_directory_hash(release_folder)

            # Resolve git commit hashes for branches
            remote_hashes = []
            source = recipe.get('source', {})
            if source and 'git' in source:
                branch = source.get('branch')
                if branch:
                    commit_hash = resolve_git_commit_hash(
                        source['git'], branch)
                    print(f"  Resolved {source['git']} {branch} -> "
                          f"{commit_hash[:12]}")
                    remote_hashes.append(commit_hash)

            if remote_hashes:
                combined = hashlib.sha1()
                combined.update(source_hash.encode('utf-8'))
                for h in sorted(remote_hashes):
                    combined.update(h.encode('utf-8'))
                source_hash = combined.hexdigest()

            print(f"  Source hash: {source_hash}")

            # We need to read mip.yaml to check architecture match.
            # mip.yaml may be in the release folder or in the source repo.
            # Do a temp fetch to read it.
            temp_dir = os.path.join(
                self.output_dir,
                f"_temp_{package_name}_{release_version}")
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, onerror=_rmtree_on_error)
            os.makedirs(temp_dir)

            try:
                self._fetch_source(recipe, temp_dir)
                overlay_channel_files(release_folder, temp_dir)

                mip_yaml_path = os.path.join(temp_dir, 'mip.yaml')
                if not os.path.exists(mip_yaml_path):
                    print(f"  Error: No mip.yaml found")
                    return False

                archs, mip_yaml = read_mip_yaml_architectures(mip_yaml_path)
            finally:
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir, onerror=_rmtree_on_error)

            # Check architecture match
            arch_matches = self.architecture in archs

            if not arch_matches:
                print(f"  No builds match architecture={self.architecture}, "
                      f"skipping")
                continue

            # Determine effective architecture for filename
            if self.architecture in archs:
                effective_arch = self.architecture
            else:
                effective_arch = 'any'

            version = mip_yaml.get('version', release_version)

            # Determine CPU level variants to build
            use_simd = (recipe.get('simd', False)
                        and is_simd_architecture(self.architecture))
            cpu_levels = list(CPU_LEVELS) if use_simd else [None]

            # For SIMD packages, clone source once and copy for each variant
            # to avoid redundant git clones.
            first_prepared_path = None
            for cpu_level in cpu_levels:
                ok = self._prepare_variant(
                    recipe=recipe,
                    release_folder=release_folder,
                    mip_yaml=mip_yaml,
                    effective_arch=effective_arch,
                    version=version,
                    source_hash=source_hash,
                    remote_hashes=remote_hashes,
                    cpu_level=cpu_level,
                    copy_from=first_prepared_path,
                )
                if not ok:
                    return False
                # After the first successful variant, record its path
                # so subsequent variants can copy instead of cloning.
                if first_prepared_path is None and not self.dry_run:
                    suffix = f"-{cpu_level}" if cpu_level else ""
                    first_prepared_path = os.path.join(
                        self.output_dir,
                        f"{mip_yaml['name']}-{version}{suffix}")
                    if not os.path.isdir(first_prepared_path):
                        first_prepared_path = None

        return True

    def _prepare_variant(self, *, recipe, release_folder, mip_yaml,
                         effective_arch, version, source_hash,
                         remote_hashes, cpu_level, copy_from=None):
        """Prepare one variant (one cpu_level or None for non-SIMD).

        When copy_from is set, copies that directory instead of cloning
        the source again — avoids redundant git clones for SIMD variants.
        """
        name = mip_yaml['name']

        # Build .mhl filename (with optional cpu_level suffix)
        if cpu_level:
            mhl_filename = f"{name}-{version}-{effective_arch}-{cpu_level}.mhl"
            output_name = f"{name}-{version}-{cpu_level}"
            print(f"  Variant: {effective_arch} / {cpu_level}")
        else:
            mhl_filename = f"{name}-{version}-{effective_arch}.mhl"
            output_name = f"{name}-{version}"

        # Check cache
        if not self.force and check_existing_package(
                mhl_filename, source_hash, mip_yaml):
            print(f"  Skipping - package already up to date")
            return True

        if self.dry_run:
            print(f"  [DRY RUN] Would prepare {output_name}")
            return True

        output_path = os.path.join(self.output_dir, output_name)
        if os.path.exists(output_path):
            shutil.rmtree(output_path, onerror=_rmtree_on_error)

        try:
            if copy_from and os.path.isdir(copy_from):
                print(f"  Copying from {os.path.basename(copy_from)}...")
                shutil.copytree(copy_from, output_path)
            else:
                os.makedirs(output_path)
                self._fetch_source(recipe, output_path)
                overlay_channel_files(release_folder, output_path)

            # Write source_hash
            with open(os.path.join(output_path, '.source_hash'), 'w') as f:
                f.write(source_hash)

            # Write raw git commit hash if available
            if remote_hashes:
                with open(os.path.join(output_path, '.commit_hash'), 'w') as f:
                    f.write(remote_hashes[0])

            # Write SIMD metadata for bundle_packages.m
            if cpu_level:
                with open(os.path.join(output_path, '.cpu_level'), 'w') as f:
                    f.write(cpu_level)
                compiler_env = get_compiler_env(effective_arch, cpu_level)
                with open(os.path.join(output_path, '.compiler_env'), 'w') as f:
                    json.dump(compiler_env, f, indent=2)

            print(f"  Prepared: {output_path}")
            return True

        except Exception as e:
            print(f"  Error preparing package: {e}")
            import traceback
            traceback.print_exc()
            if os.path.exists(output_path):
                shutil.rmtree(output_path, onerror=_rmtree_on_error)
            return False

    def prepare_all(self):
        """Prepare all packages."""
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        packages_dir = os.path.join(project_root, 'packages')

        if not os.path.exists(packages_dir):
            print(f"Error: packages directory not found at {packages_dir}")
            return False

        package_dirs = sorted([
            os.path.join(packages_dir, d)
            for d in os.listdir(packages_dir)
            if os.path.isdir(os.path.join(packages_dir, d))
        ])

        print(f"Found {len(package_dirs)} package(s)")
        print(f"Output directory: {self.output_dir}")
        print(f"Architecture: {self.architecture}")

        for package_dir in package_dirs:
            if not self.prepare_package(package_dir):
                return False

        return True


def main():
    parser = argparse.ArgumentParser(
        description='Prepare package source directories from recipe.yaml')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--output-dir', type=str)
    parser.add_argument('--package', type=str)
    parser.add_argument('--release', type=str)

    args = parser.parse_args()

    preparer = PackagePreparer(
        dry_run=args.dry_run,
        force=args.force,
        output_dir=args.output_dir,
    )

    if args.package:
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        package_dir = os.path.join(project_root, 'packages', args.package)
        if not os.path.exists(package_dir):
            print(f"Error: Package '{args.package}' not found")
            return 1
        success = preparer.prepare_package(package_dir, release=args.release)
    else:
        success = preparer.prepare_all()

    if success:
        print("\nAll packages prepared successfully")
        return 0
    else:
        print("\nPreparation failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
