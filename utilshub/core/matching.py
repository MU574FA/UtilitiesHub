"""Compare two lists of names (spelling, word order, extra words, Arabic variants).

    results = match_lists(names_a, names_b, top_n=3)        # results[i] = [(index in B, score), ...] best first
    pairs   = one_to_one(results, minimum=0.8)              # best non-conflicting assignment

A trigram index keeps this fast for lists of thousands of names: each name is only scored against
the ~80 entries of B that share the most letter groups with it.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Callable

from .fuzzy import name_score, prep_name


def _grams(tokens: tuple) -> set[str]:
    out: set[str] = set()
    for tok in tokens:
        padded = f" {tok} "
        if len(padded) < 3:
            out.add(padded)
        out.update(padded[i:i + 3] for i in range(len(padded) - 2))
    return out


def match_lists(a_names: list[str], b_names: list[str], top_n: int = 3, floor: float = 0.4,
                candidates: int = 80, progress: Callable[[int, int], None] | None = None,
                cancelled: Callable[[], bool] | None = None) -> list[list[tuple[int, float]]]:
    pa = [prep_name(x) for x in a_names]
    pb = [prep_name(x) for x in b_names]
    index: dict[str, list[int]] = defaultdict(list)
    for j, p in enumerate(pb):
        for g in _grams(p.tokens):
            index[g].append(j)
    exact: dict[str, list[int]] = defaultdict(list)
    for j, p in enumerate(pb):
        if p.s:
            exact[p.sorted].append(j)
    out: list[list[tuple[int, float]]] = []
    total = len(pa)
    for i, p in enumerate(pa):
        if cancelled and cancelled():
            break
        scored: dict[int, float] = {}
        if p.s:
            for j in exact.get(p.sorted, []):
                scored[j] = 1.0 if pb[j].s == p.s else max(0.99, name_score(p, pb[j]))
            votes: Counter = Counter()
            for g in _grams(p.tokens):
                bucket = index.get(g)
                if bucket and len(bucket) < 4000:            # ignore grams that appear everywhere
                    votes.update(bucket)
            for j, _ in votes.most_common(candidates):
                if j not in scored:
                    scored[j] = name_score(p, pb[j])
        ranked = sorted(((j, s) for j, s in scored.items() if s >= floor), key=lambda x: -x[1])[:top_n]
        out.append(ranked)
        if progress and (i % 20 == 0 or i == total - 1):
            progress(i + 1, total)
    return out


def one_to_one(results: list[list[tuple[int, float]]], minimum: float = 0.0) -> dict[int, tuple[int, float]]:
    """Greedy best-first assignment so each B entry is claimed by at most one A entry."""
    pairs = sorted(((s, i, j) for i, cands in enumerate(results) for j, s in cands if s >= minimum), reverse=True)
    taken_a: set[int] = set()
    taken_b: set[int] = set()
    out: dict[int, tuple[int, float]] = {}
    for s, i, j in pairs:
        if i in taken_a or j in taken_b:
            continue
        taken_a.add(i)
        taken_b.add(j)
        out[i] = (j, s)
    return out


def status_for(score: float, threshold: float, strong: float = 0.9) -> str:
    """strong | probable | weak"""
    return "strong" if score >= strong else "probable" if score >= threshold else "weak"


def find_internal_duplicates(names: list[str], threshold: float = 0.88) -> list[tuple[int, int, float]]:
    """Pairs inside ONE list that are probably the same person."""
    res = match_lists(names, names, top_n=4, floor=threshold)
    pairs = []
    for i, cands in enumerate(res):
        for j, s in cands:
            if j > i:
                pairs.append((i, j, s))
    return sorted(pairs, key=lambda x: -x[2])
