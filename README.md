# MIP Channel

This repo is a [MIP](https://github.com/mip-org/mip-package-manager) package channel. It hosts MATLAB packages as GitHub Release assets and publishes a package index via GitHub Pages.

## Creating your own channel

1. **Fork** (or use as template) this repo.
2. **Edit `channel.yaml`** — set `channel` to a short name and `github_repo` to your `owner/repo`.
3. **Enable GitHub Pages** — go to Settings > Pages and set source to "GitHub Actions".
4. **Add packages** — create directories under `packages/` (see below).
5. **Push to `main`** — the CI workflow will build, upload, and index your packages automatically.

No cloud storage credentials are needed. Packages are stored as GitHub Release assets and the index is served via GitHub Pages.

## Adding a package

Create `packages/<name>/releases/<version>/prepare.yaml`:

```yaml
name: my_package
description: "What this package does"
version: "1.0.0"
dependencies: []
homepage: ""
repository: ""
license: "MIT"

defaults:
  release_number: 1
  prepare:
    clone_git:
      url: "https://github.com/someone/some-matlab-repo.git"
      destination: "my_package"
  addpaths:
    - path: "my_package"

builds:
  - architectures: [any]
```

Package names must use underscores (not hyphens). The version in the YAML must match the release folder name.

## Staying up to date

To pull in the latest infrastructure (scripts, workflows) from the base repo:

```bash
git remote add upstream https://github.com/mip-org/mip-channel-base.git
git fetch upstream
git merge upstream/main --allow-unrelated-histories
```

Your `channel.yaml`, `packages/`, and `README.md` won't conflict since those are channel-specific.

## How it works

On every push to `main`, GitHub Actions:

1. **Prepares** packages — clones/downloads source, computes MATLAB paths, generates metadata
2. **Compiles** packages — runs MATLAB compile scripts if specified
3. **Bundles** packages — creates `.mhl` files (ZIP archives)
4. **Uploads** packages — stores `.mhl` files as GitHub Release assets (one release per package-version)
5. **Assembles index** — collects metadata from all releases into `index.json`
6. **Deploys** — publishes `index.json` and `packages.html` to GitHub Pages

The MATLAB client (`mip install <package>`) fetches the index from GitHub Pages and downloads `.mhl` files from the releases.

## Using this channel in MATLAB

Channel names map to index URLs by convention: a channel named `foo` resolves to `https://mip-org.github.io/mip-foo/index.json`.

```matlab
% Install a package from your channel
mip install --channel <channel_name> <package_name>

% List available packages on your channel
mip avail --channel <channel_name>
```

For channels hosted outside the `mip-org` GitHub organization, users would need to point at the GitHub Pages URL directly (see [mip-package-manager](https://github.com/mip-org/mip-package-manager) for details).
