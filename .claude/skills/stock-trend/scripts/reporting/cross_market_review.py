"""Bind cross-market display and background updates to the report's frozen inputs."""
from __future__ import annotations

import copy
import json
import math
import re

INPUT_PREFIX = '<!-- CROSS_MARKET_INPUTS:'
START = '<!-- CROSS_MARKET_OBSERVATION:START -->'
END = '<!-- CROSS_MARKET_OBSERVATION:END -->'


def prepare(ctx, observation_state):
    """Freeze the mapping alongside run evidence; enrichment never fetches data."""
    from analysis.cross_market_observation import build_cross_market_observation, load_mapping
    inputs = {'summary': copy.deepcopy(ctx.get('us_market_summary') or {}),
              'sector_state': copy.deepcopy(ctx.get('sector_persistence') or {}),
              'mappings': {}}
    try:
        inputs['mappings'] = load_mapping()
        state = build_cross_market_observation(
            inputs['summary'], inputs['sector_state'], observation_state,
            mappings=inputs['mappings'])
    except Exception as exc:
        state = {'status': 'unavailable', 'rows': [],
                 'reason': f'跨市场观察不可用：{type(exc).__name__}'}
    return inputs, state


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _render(inputs, state, format):
    from reporting.cross_market_observation import render_html, render_markdown
    encoded = json.dumps(_json_safe(inputs), ensure_ascii=False, sort_keys=True, allow_nan=False)
    # Comment delimiters and HTML tags cannot occur inside this inert JSON.
    for char in ('<', '>', '&'):
        encoded = encoded.replace(char, f'\\u{ord(char):04x}')
    block = (render_html if format == 'html' else render_markdown)(state)
    return block.replace(END, f'{INPUT_PREFIX}{encoded} -->\n{END}')


def render(ctx, format, observation_state=None):
    inputs = ctx.get('cross_market_inputs')
    state = ctx.get('cross_market_observation')
    if inputs is None or state is None:
        inputs, state = prepare(ctx, observation_state)
    return _render(inputs, state, format)


def update_block(original, state, pending, format):
    """Replace supplementary display under the observation updater's shared lock."""
    from analysis.cross_market_observation import build_cross_market_observation
    if START not in original and END not in original:
        return original  # Existing reports predate this optional section.
    if original.count(START) != 1 or original.count(END) != 1:
        raise ValueError('cross_market_block_invalid')
    match = re.search(r'<!-- CROSS_MARKET_INPUTS:(.*?) -->', original, re.DOTALL)
    if not match:
        raise ValueError('cross_market_frozen_inputs_missing')
    inputs = json.loads(match.group(1))
    observation = None if pending else state
    try:
        result = build_cross_market_observation(
            inputs['summary'], inputs['sector_state'], observation, mappings=inputs['mappings'])
    except Exception as exc:
        result = {'status': 'unavailable', 'rows': [],
                  'reason': f'跨市场观察不可用：{type(exc).__name__}'}
    start = original.index(START)
    end = original.index(END, start) + len(END)
    return original[:start] + _render(inputs, result, format) + original[end:]
