"""Local configuration labels and TTL-cached discovery counts."""

import hashlib
import json
import os
import tempfile
import time

import stats

CACHE_TTL_SECONDS = 60
CACHE_VERSION = 1


def _path_key(path):
    return os.path.normcase(os.path.realpath(path))


def _read_object(path):
    try:
        with open(path, encoding='utf-8-sig') as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _plugin_roots(config_dir, enabled):
    roots = []
    for identifier, active in enabled.items():
        if not active or not isinstance(identifier, str) or '@' not in identifier:
            continue
        plugin, publisher = identifier.rsplit('@', 1)
        if any(not part or part in ('.', '..') or
               any(ch in part for ch in '/\\:') for part in (plugin, publisher)):
            continue
        base = os.path.join(config_dir, 'plugins', 'cache')
        root = os.path.join(base, publisher, plugin)
        if os.path.commonpath([_path_key(base), _path_key(root)]) == _path_key(base):
            roots.append(root)
    return roots


def _count_mcp(paths):
    count = 0
    for path in {_path_key(path) for path in paths}:
        data = _read_object(path)
        if data is None:
            return None
        servers = data.get('mcpServers') or {}
        if not isinstance(servers, dict):
            return None
        count += len(servers)
    return count


def _count_skills(roots):
    files = set()
    visited = set()

    def scan(root):
        key = _path_key(root)
        if key in visited:
            return
        visited.add(key)
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    # User Skill directories commonly link to a shared library.
                    # Canonical visited paths prevent cycles without hiding them.
                    if entry.is_dir():
                        scan(entry.path)
                    elif entry.name == 'SKILL.md' and entry.is_file():
                        files.add(_path_key(entry.path))
        except FileNotFoundError:
            # Missing configured roots are normal; disappearing children are not.
            if key not in root_keys:
                raise

    root_keys = {_path_key(root) for root in roots}
    try:
        for root in root_keys:
            scan(root)
    except OSError:
        return None
    return len(files)


def _discover_counts(config_dir, project_dir, enabled):
    mcp_paths = [os.path.join(config_dir, 'mcp.json'),
                 os.path.join(project_dir, '.mcp.json'),
                 os.path.join(project_dir, '.codebuddy', 'mcp.json')]
    skill_roots = [os.path.join(config_dir, 'skills'),
                   os.path.join(project_dir, '.codebuddy', 'skills'),
                   os.path.join(project_dir, '.claude', 'skills')]
    skill_roots.extend(_plugin_roots(config_dir, enabled))
    return {'mcp_count': _count_mcp(mcp_paths),
            'skill_count': _count_skills(skill_roots)}


def _cached_counts(config_dir, project_dir, enabled):
    identity = json.dumps([_path_key(config_dir), _path_key(project_dir)])
    key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    plugin_key = hashlib.sha256(json.dumps(enabled, sort_keys=True).encode('utf-8')).hexdigest()
    cache_path = os.path.join(config_dir, 'plugins', 'data', 'statusline', 'metadata', key + '.json')
    now = time.time()
    cached = _read_object(cache_path)
    if (cached and cached.get('v') == CACHE_VERSION and
            cached.get('plugins') == plugin_key and
            isinstance(cached.get('time'), (int, float)) and
            0 <= now - cached['time'] < CACHE_TTL_SECONDS and
            all(name in cached and (cached[name] is None or
                (type(cached[name]) is int and cached[name] >= 0))
                for name in ('mcp_count', 'skill_count'))):
        return {name: cached[name] for name in ('mcp_count', 'skill_count')}

    counts = _discover_counts(config_dir, project_dir, enabled)
    payload = dict(counts, v=CACHE_VERSION, time=now, plugins=plugin_key)
    temp_path = None
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=os.path.dirname(cache_path),
                                         suffix='.tmp', delete=False) as stream:
            temp_path = stream.name
            json.dump(payload, stream)
        os.replace(temp_path, cache_path)
    except OSError:
        pass
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return counts


def load_local_metadata(input_data):
    """Read config labels; discover MCP/Skill files at most once per TTL."""
    workspace = input_data.get('workspace') or {}
    cwd = workspace.get('current_dir') or input_data.get('cwd') or os.getcwd()
    project_dir = workspace.get('project_dir') or cwd
    config_dir = stats._CONFIG_DIR
    settings = _read_object(os.path.join(config_dir, 'settings.json'))
    settings_valid = settings is not None
    settings = settings or {}
    permissions = settings.get('permissions') or {}
    enabled = settings.get('enabledPlugins') or {}
    plugins_valid = isinstance(enabled, dict)
    enabled = {key: bool(value) for key, value in enabled.items()} if plugins_valid else {}
    metadata = {
        'cwd': cwd,
        'reasoning_effort': settings.get('reasoningEffort'),
        'permission_mode': permissions.get('defaultMode') if isinstance(permissions, dict) else None,
    }
    metadata.update(_cached_counts(config_dir, project_dir, enabled))
    if not settings_valid or not plugins_valid:
        metadata['skill_count'] = None
    return metadata
