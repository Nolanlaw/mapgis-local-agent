"""Deterministic point-feature placement with editable WT move proposals."""
from __future__ import annotations

from dataclasses import dataclass
import io
import math
from pathlib import Path
import random
from time import perf_counter
import unicodedata

from pymapgis.reader import _read_crs
from shapely import STRtree
from shapely.geometry import box

from .comparison import _extract_style
from .core import BridgeError, annotation_text, json_text, native_ids, read_native, sha


DIRECTIONS = (('NE', 1, 1), ('NW', -1, 1), ('SE', 1, -1), ('SW', -1, -1),
              ('N', 0, 1), ('S', 0, -1), ('E', 1, 0), ('W', -1, 0))


@dataclass(frozen=True)
class Label:
    record_id: int
    text: str
    x: float
    y: float
    height: float
    width: float
    angle: float = 0
    spacing: float = 0
    priority: float = 1


@dataclass(frozen=True)
class Position:
    name: str
    dx: float
    dy: float
    bounds: tuple[float, float, float, float]
    penalty: float


def _glyph_width(character):
    if unicodedata.combining(character):
        return 0
    if unicodedata.east_asian_width(character) in ('W', 'F'):
        return 1
    if character.isspace():
        return .35
    return .58


def text_box(label: Label, *, padding: float = .15):
    """Return a rotated AABB in source XY. The WT anchor is the text origin."""
    rows = label.text.splitlines() or ['']
    width = max(sum(_glyph_width(ch) for ch in row) * label.width
                + max(len(row) - 1, 0) * label.spacing for row in rows)
    margin = label.height * padding
    height = label.height * (1 + (len(rows) - 1) * 1.2)
    sine, cosine = math.sin(math.radians(label.angle)), math.cos(math.radians(label.angle))
    corners = [(x * cosine - y * sine, x * sine + y * cosine)
               for x in (-margin, width + margin) for y in (-margin, height + margin)]
    return (label.x + min(x for x, _ in corners), label.y + min(y for _, y in corners),
            label.x + max(x for x, _ in corners), label.y + max(y for _, y in corners))


def _offset(bounds, dx, dy):
    return (bounds[0] + dx, bounds[1] + dy, bounds[2] + dx, bounds[3] + dy)


def candidate_positions(label: Label, *, padding=.15, gap=.25, steps=1):
    """Eight directional placements per ring, measured relative to the original anchor."""
    if type(steps) is not int or not 1 <= steps <= 8:
        raise BridgeError('INVALID_TOOL_ARGUMENTS: label steps must be between 1 and 8')
    bounds = text_box(label, padding=padding)
    left, bottom, right, top = bounds
    for ring in range(1, steps + 1):
        clearance = label.height * gap * ring
        lateral = {-1: label.x - right - clearance, 0: label.x - (left + right) / 2,
                   1: label.x - left + clearance}
        vertical = {-1: label.y - top - clearance, 0: label.y - (bottom + top) / 2,
                    1: label.y - bottom + clearance}
        for rank, (name, horizontal, direction) in enumerate(DIRECTIONS):
            dx, dy = lateral[horizontal], vertical[direction]
            yield Position(name if ring == 1 else f'{name}{ring}', dx, dy, _offset(bounds, dx, dy),
                           rank / 8 + ring - 1)


def _intersects(left, right):
    return left[0] < right[2] and right[0] < left[2] and left[1] < right[3] and right[1] < left[3]


def _neighbors(positions):
    envelopes = [box(min(p.bounds[0] for p in row), min(p.bounds[1] for p in row),
                     max(p.bounds[2] for p in row), max(p.bounds[3] for p in row)) for row in positions]
    tree = STRtree(envelopes)
    return [sorted(int(peer) for peer in tree.query(envelope) if peer != index)
            for index, envelope in enumerate(envelopes)]


def _solve(labels, *, solver, locked_ids, seed, padding, gap, steps, iterations):
    starts = perf_counter()
    if solver not in {'greedy_local', 'simulated_annealing'}:
        raise BridgeError('INVALID_TOOL_ARGUMENTS: unknown placement solver')
    if type(iterations) is not int or iterations < 1:
        raise BridgeError('INVALID_TOOL_ARGUMENTS: iterations must be a positive integer')
    if not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in (padding, gap)):
        raise BridgeError('INVALID_TOOL_ARGUMENTS: finite nonnegative label padding and gap required')
    positions = []
    for label in labels:
        origin = Position('original', 0., 0., text_box(label, padding=padding), 0.)
        moves = [] if label.record_id in locked_ids else list(candidate_positions(label, padding=padding, gap=gap, steps=steps))
        positions.append([origin, *moves])
    neighbors = _neighbors(positions)
    movable = sorted((i for i, label in enumerate(labels) if label.record_id not in locked_ids),
                     key=lambda i: (-labels[i].priority, labels[i].record_id))
    state = [-1 if i in movable else 0 for i in range(len(labels))]
    penalties = [[(math.hypot(p.dx, p.dy) / label.height + p.penalty * .2) * label.priority
                  for p in row] for label, row in zip(labels, positions)]
    collision_cost = 1 + sum(max(row) for row in penalties)

    def local(index, choice):
        bounds = positions[index][choice].bounds
        conflicts = sum(state[peer] >= 0 and _intersects(bounds, positions[peer][state[peer]].bounds)
                        for peer in neighbors[index])
        return conflicts * collision_cost + penalties[index][choice]

    def pairs(choices):
        return [(i, peer) for i, row in enumerate(neighbors) for peer in row if peer > i
                and _intersects(positions[i][choices[i]].bounds, positions[peer][choices[peer]].bounds)]

    def energy(choices):
        return collision_cost * len(pairs(choices)) + sum(row[choice] for row, choice in zip(penalties, choices))

    for index in movable:
        state[index] = min(range(len(positions[index])), key=lambda c: local(index, c))
    for _ in range(8):
        changed = False
        for index in movable:
            best = min(range(len(positions[index])), key=lambda c: local(index, c))
            if local(index, best) < local(index, state[index]):
                state[index] = best
                changed = True
        if not changed:
            break
    original = [0] * len(labels)
    if energy(original) < energy(state):
        state = original.copy()
    if solver == 'simulated_annealing' and movable:
        rng = random.Random(seed)
        best_state = state.copy()
        score = best_score = energy(state)
        for step in range(iterations):
            index = rng.choice(movable)
            proposed = rng.randrange(len(positions[index]))
            change = local(index, proposed) - local(index, state[index])
            temperature = collision_cost * .55 * (.002 ** (step / max(iterations - 1, 1)))
            if change <= 0 or rng.random() < math.exp(-change / temperature):
                state[index] = proposed
                score += change
                if score < best_score - 1e-9:
                    best_state, best_score = state.copy(), score
        state = best_state

    overlaps_before, remaining = len(pairs(original)), pairs(state)
    occupied = [row[choice] for row, choice in zip(positions, state)]
    distances = [math.hypot(p.dx, p.dy) for p in occupied]
    updates = [{'record_id': label.record_id, 'x': label.x + p.dx, 'y': label.y + p.dy}
               for label, p, distance in zip(labels, occupied, distances) if distance]
    return {
        'solver': solver, 'seed': seed, 'annotation_count': len(labels), 'locked_ids': sorted(locked_ids),
        'overlaps_before': overlaps_before, 'overlaps_after': len(remaining),
        'resolution_rate': (overlaps_before - len(remaining)) / overlaps_before if overlaps_before else 1.0,
        'displacement': {'moved': len(updates), 'mean': sum(distances) / len(labels) if labels else 0.,
                         'max': max(distances, default=0.)},
        'residual_overlaps': [{'record_ids': [labels[i].record_id, labels[j].record_id],
                               'coords': [(max(occupied[i].bounds[axis], occupied[j].bounds[axis])
                                           + min(occupied[i].bounds[axis+2], occupied[j].bounds[axis+2])) / 2
                                          for axis in (0, 1)]} for i, j in remaining],
        'positions': [{'record_id': label.record_id, 'candidate': p.name, 'dx': p.dx, 'dy': p.dy,
                       'bounds': list(p.bounds)} for label, p in zip(labels, occupied)],
        'runtime_ms': (perf_counter() - starts) * 1000, 'updates': updates,
    }


def _source(file, expected_sha256, priority_by_id):
    path = Path(file).resolve(strict=True)
    blob, reader, sections = read_native(path)
    digest = sha(blob)
    if expected_sha256 is not None and digest != expected_sha256:
        raise BridgeError('STALE_SOURCE')
    if reader.shapeType != 'POINT':
        raise BridgeError('INVALID_TOOL_ARGUMENTS: label placement requires WT annotation records')
    _, factor = _read_crs(io.BytesIO(blob))
    if not math.isfinite(factor) or factor <= 0:
        raise BridgeError('NATIVE_COORDINATE_SCALE')

    labels = []
    for index, (rid, anchor) in enumerate(zip(native_ids(reader), reader.geom)):
        text = annotation_text(blob, sections, rid)
        if not text:
            continue
        style = _extract_style(reader, index)
        priority = priority_by_id.get(str(rid), priority_by_id.get(rid, 1.))
        height, width = style.get('height'), style.get('width')
        angle, spacing = style.get('angle') or 0., style.get('spacing') or 0.
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (priority, height, width, angle, spacing)):
            raise BridgeError('INVALID_TOOL_ARGUMENTS: finite label dimensions and priorities required')
        if min(priority, height, width) <= 0 or spacing < 0 or anchor.is_empty:
            raise BridgeError('INVALID_TOOL_ARGUMENTS: invalid native label dimensions or anchor')
        labels.append(Label(rid, text, anchor.x, anchor.y, height * factor, width * factor,
                            angle, spacing * factor, priority))
    if len(labels) > 1000:
        raise BridgeError('POINT_UPDATES: select a WT with at most 1000 annotations for one editable patch')
    return path, digest, labels


def _report(path, digest, labels, *, solver, locked_ids=None, seed=0, padding=.15,
            gap=.25, steps=2, iterations=2500):
    identities = {label.record_id for label in labels}
    locked = set(locked_ids or [])
    if any(type(rid) is not int for rid in locked) or not locked <= identities:
        raise BridgeError('INVALID_TOOL_ARGUMENTS: locked_ids must identify active WT annotations')
    result = _solve(labels, solver=solver, locked_ids=locked, seed=seed, padding=padding, gap=gap,
                    steps=steps, iterations=iterations)
    updates = result.pop('updates')
    patch = {'operation': 'point.move', 'file': str(path), 'expected_sha256': digest,
             'parameters': {'updates': updates}}
    request_id = 'label-' + sha(json_text(patch).encode('utf8'))[:32]
    return result | {'file': str(path), 'expected_sha256': digest,
                     'patch_plan': [patch | {'request_id': request_id}] if updates else []}


def place_labels(file, expected_sha256=None, *, solver='greedy_local', priority_by_id=None, **options):
    """Read active annotation records and propose a standard native point.move request."""
    return _report(*_source(file, expected_sha256, priority_by_id or {}), solver=solver, **options)


def compare_solvers(file, expected_sha256=None, *, priority_by_id=None, **options):
    source = _source(file, expected_sha256, priority_by_id or {})
    return {solver: _report(*source, solver=solver, **options)
            for solver in ('greedy_local', 'simulated_annealing')}
