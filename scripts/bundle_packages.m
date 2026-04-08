% Bundle all prepared packages using mip bundle.
%
% This script discovers all prepared directories in build/prepared/
% and calls mip.bundle() on each to produce .mhl files in build/bundled/.
%
% Expected to be run from the repository root directory.

fprintf('=== Bundle Packages ===\n');

preparedDir = fullfile(pwd, 'build', 'prepared');
outputDir = fullfile(pwd, 'build', 'bundled');

architecture = getenv('BUILD_ARCHITECTURE');
if isempty(architecture)
    % err
    error('mip:missingArchitecture', 'Environment variable BUILD_ARCHITECTURE is not set');
end

if ~exist(preparedDir, 'dir')
    fprintf('No prepared directory found at %s. Nothing to bundle.\n', preparedDir);
    return;
end

if ~exist(outputDir, 'dir')
    mkdir(outputDir);
end

% List prepared directories
items = dir(preparedDir);
bundled = 0;
failed = 0;

for i = 1:length(items)
    if ~items(i).isdir || startsWith(items(i).name, '.')
        continue;
    end

    pkgDir = fullfile(preparedDir, items(i).name);

    % Check for mip.yaml
    if ~exist(fullfile(pkgDir, 'mip.yaml'), 'file')
        fprintf('Skipping %s (no mip.yaml)\n', items(i).name);
        continue;
    end

    fprintf('\n--- Bundling: %s ---\n', items(i).name);

    try
        % Apply compiler environment from .compiler_env (SIMD builds)
        % Parse JSON manually to preserve keys like _LINK_ that jsondecode
        % mangles (MATLAB struct fields cannot start with underscore).
        originalEnv = containers.Map('KeyType','char','ValueType','char');
        compilerEnvFile = fullfile(pkgDir, '.compiler_env');
        if exist(compilerEnvFile, 'file')
            envMap = readJsonEnvFile(compilerEnvFile);
            envKeys = keys(envMap);
            for j = 1:length(envKeys)
                k = envKeys{j};
                originalEnv(k) = getenv(k);
                setenv(k, envMap(k));
                fprintf('  Setting %s=%s\n', k, envMap(k));
            end
        end

        % Read cpu_level (if present)
        cpuLevel = '';
        cpuLevelFile = fullfile(pkgDir, '.cpu_level');
        if exist(cpuLevelFile, 'file')
            cpuLevel = strtrim(fileread(cpuLevelFile));
            fprintf('  CPU level: %s\n', cpuLevel);
        end

        % Bundle (standard args — no --cpu-level, works with upstream mip)
        mip.bundle(pkgDir, '--output', outputDir, '--arch', architecture);

        % If SIMD variant, rename .mhl and .mip.json to include cpu_level
        if ~isempty(cpuLevel)
            renameBundleWithCpuLevel(outputDir, pkgDir, cpuLevel);
        end

        bundled = bundled + 1;

        % Restore compiler environment
        restoreEnv(originalEnv);
    catch ME
        % Restore compiler environment on failure too
        if exist('originalEnv', 'var')
            restoreEnv(originalEnv);
        end
        fprintf('Error bundling %s: %s\n', items(i).name, ME.message);
        failed = failed + 1;
    end
end

fprintf('\n=== Bundle Summary ===\n');
fprintf('Bundled: %d\n', bundled);
fprintf('Failed: %d\n', failed);

if failed > 0
    error('mip:bundleFailed', '%d package(s) failed to bundle', failed);
end


function restoreEnv(envMap)
%RESTOREENV  Restore environment variables from a containers.Map.
if ~isa(envMap, 'containers.Map')
    return
end
envKeys = keys(envMap);
for j = 1:length(envKeys)
    setenv(envKeys{j}, envMap(envKeys{j}));
end
end


function renameBundleWithCpuLevel(outputDir, ~, cpuLevel)
%RENAMEBUNDLEWITHCPULEVEL  Rename .mhl and .mip.json to include cpu_level.
%
%   mip.bundle produces: name-version-arch.mhl
%   We rename to:        name-version-arch-cpuLevel.mhl
%   Also patches cpu_level into the .mip.json metadata.

% Find .mhl files that don't already have any cpu_level suffix.
% cpu_level suffixes look like: -x86_64_v1.mhl, -x86_64_v2.mhl, etc.
cpuLevelPattern = '-x86_64_v';
mhlFiles = dir(fullfile(outputDir, '*.mhl'));
for j = 1:length(mhlFiles)
    oldName = mhlFiles(j).name;
    if endsWith(oldName, '.mip.json')
        continue
    end
    % Skip if already has any cpu_level suffix
    if contains(oldName, cpuLevelPattern)
        continue
    end

    % Insert -cpuLevel before .mhl
    newName = strrep(oldName, '.mhl', ['-' cpuLevel '.mhl']);
    oldPath = fullfile(outputDir, oldName);
    newPath = fullfile(outputDir, newName);
    movefile(oldPath, newPath);
    fprintf('  Renamed: %s -> %s\n', oldName, newName);

    % Also rename and patch the .mip.json companion
    oldJsonPath = [oldPath '.mip.json'];
    newJsonPath = [newPath '.mip.json'];
    if exist(oldJsonPath, 'file')
        fid = fopen(oldJsonPath, 'r');
        metadata = jsondecode(fread(fid, '*char')');
        fclose(fid);
        metadata.cpu_level = cpuLevel;
        fid = fopen(newJsonPath, 'w');
        fwrite(fid, jsonencode(metadata));
        fclose(fid);
        if ~strcmp(oldJsonPath, newJsonPath)
            delete(oldJsonPath);
        end
        fprintf('  Patched: %s\n', [newName '.mip.json']);
    end
end
end


function envMap = readJsonEnvFile(filepath)
%READJSONENVFILE  Parse a JSON key-value file into a containers.Map.
%   Avoids jsondecode which mangles keys starting with underscore.
envMap = containers.Map('KeyType','char','ValueType','char');
text = fileread(filepath);
% Simple regex extraction of "key": "value" pairs
tokens = regexp(text, '"([^"]+)"\s*:\s*"([^"]*)"', 'tokens');
for j = 1:length(tokens)
    envMap(tokens{j}{1}) = tokens{j}{2};
end
end
