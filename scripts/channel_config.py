"""Load channel configuration from channel.yaml."""

import os
import yaml


def load_channel_config():
    """Load channel.yaml from the project root.

    Returns a dict with keys: channel, github_repo.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(project_root, 'channel.yaml')
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def is_channel_configured():
    """Check whether channel.yaml has been configured (not still placeholders)."""
    cfg = load_channel_config()
    return cfg.get('github_repo') not in (None, 'OWNER/REPO')


def get_base_url(release_tag):
    """Get the download base URL for a given release tag (name-version)."""
    cfg = load_channel_config()
    return f"https://github.com/{cfg['github_repo']}/releases/download/{release_tag}"


def release_tag_from_mhl(mhl_filename):
    """Extract the release tag (name-version) from an .mhl filename.

    Filename format: {name}-{version}-{architecture}.mhl
    Package names use underscores, never hyphens (enforced by prepare_packages.py),
    so the first hyphen separates name from version, and the second separates
    version from architecture.

    Returns: "{name}-{version}" string suitable as a GitHub release tag.
    """
    # Strip .mhl or .mhl.mip.json suffix
    basename = mhl_filename
    if basename.endswith('.mip.json'):
        basename = basename[:-9]  # remove .mip.json
    if basename.endswith('.mhl'):
        basename = basename[:-4]  # remove .mhl

    # Split: name-version-architecture
    parts = basename.split('-')
    # parts[0] = name, parts[1] = version, parts[2:] = architecture
    return f"{parts[0]}-{parts[1]}"
