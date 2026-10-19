"""Redact supplied credentials at diagnostic and approval output boundaries."""
import re

_SECRET = re.compile(r'\b(?:password|passwd|secret|api[_ -]?key|token)\b', re.I)


def collect_secrets(value):
    found = set()
    if hasattr(value, 'model_dump'):
        value = value.model_dump()
    if isinstance(value, dict):
        target = str(value.get('target') or value.get('goal_target') or '').split(' context=', 1)[0]
        command = value.get('command') or (value.get('arguments') or {}).get('command') or {}
        if hasattr(command, 'model_dump'):
            command = command.model_dump()
        target += ' ' + str(command.get('target', '')) + ' ' + str(command.get('locator', ''))
        if _SECRET.search(target):
            for source in (value, command):
                for key in ('value', 'expected'):
                    if isinstance(source.get(key), str) and source[key]:
                        found.add(source[key])
        for key, item in value.items():
            if _SECRET.fullmatch(str(key)) and isinstance(item, str) and item:
                found.add(item)
            found.update(collect_secrets(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(collect_secrets(item))
    elif isinstance(value, str):
        found.update(re.findall(r'(?im)^\s*(?:password|passwd|secret|api[_ -]?key|token)\s*:\s*(\S+)', value))
    return found - {'[REDACTED]'}


def redact(value, secrets=()):
    if hasattr(value, 'model_dump'):
        value = value.model_dump()
    if isinstance(value, dict):
        hidden = {'instruction', 'next_goal', 'goal', 'success_condition', 'expected_result', 'user_evidence'}
        return {key: '[REDACTED]' if _SECRET.fullmatch(str(key)) or
                (key in hidden and isinstance(item, str) and _SECRET.search(item)) else redact(item, secrets)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, '[REDACTED]')
    return value
