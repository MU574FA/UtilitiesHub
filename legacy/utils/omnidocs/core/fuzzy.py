"""Fuzzy Matching and Similarity Scoring Engine.
Implements Levenshtein Ratio, Token Sort Ratio, and Length-Weighted Token Overlap
with RapidFuzz hardware acceleration and clean Python fallback.
"""
from __future__ import annotations

import difflib
from typing import Any

try:
    import rapidfuzz.distance.Levenshtein as rf_lev
    import rapidfuzz.fuzz as rf_fuzz
    HAS_RAPIDFUZZ = True
except Exception:
    HAS_RAPIDFUZZ = False


def levenshtein_dist(a: str, b: str) -> int:
    """Compute standard Levenshtein edit distance."""
    if HAS_RAPIDFUZZ:
        return rf_lev.distance(a, b)
    
    n, m = len(a), len(b)
    if not n:
        return m
    if not m:
        return n
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        ca = a[i - 1]
        for j in range(1, m + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[m]


def levenshtein_ratio(a: str, b: str) -> float:
    """1 - (levenshtein_distance / max_len). Range [0.0, 1.0]."""
    if a == b:
        return 1.0
    max_len = max(len(a), len(b))
    if max_len == 0:
        return 1.0
    return max(0.0, 1.0 - (levenshtein_dist(a, b) / max_len))


def token_overlap_score(tokens_a: list[str], tokens_b: list[str]) -> float:
    """
    Length-Weighted Token Overlap & Coverage Ratio (r3) as specified in name_matcher.md.
    Compares every token in the shorter string against tokens in the longer string,
    weights matched tokens by character length, and applies coverage penalty:
    r3 = tf * (0.85 + 0.15 * (short_len / long_len))
    """
    if not tokens_a or not tokens_b:
        return 0.0
    
    sm = tokens_a if len(tokens_a) <= len(tokens_b) else tokens_b
    lg = tokens_b if len(tokens_a) <= len(tokens_b) else tokens_a
    
    short_total_len = sum(len(t) for t in sm) or 1
    long_total_len = sum(len(t) for t in lg) or 1
    
    matched_weight = 0.0
    used_lg_indices = set()
    
    for t_s in sm:
        best_token_score = 0.0
        best_idx = -1
        for idx, t_l in enumerate(lg):
            if idx in used_lg_indices:
                continue
            r = levenshtein_ratio(t_s, t_l)
            if r > best_token_score:
                best_token_score = r
                best_idx = idx
        
        # Consider a token matched if similarity >= 0.82
        if best_token_score >= 0.82 and best_idx != -1:
            used_lg_indices.add(best_idx)
            matched_weight += len(t_s) * best_token_score

    tf = matched_weight / short_total_len
    # Coverage scale factor
    coverage_factor = 0.85 + 0.15 * min(1.0, short_total_len / long_total_len)
    return min(1.0, tf * coverage_factor)


def compute_name_similarity(item_a: dict[str, Any], item_b: dict[str, Any]) -> float:
    """
    Computes multi-strategy similarity between two prepared name items:
    score = max(r1, r2, r3)
    where:
      r1 = levenshtein ratio
      r2 = token sort ratio
      r3 = length-weighted token overlap
    """
    s_a, s_b = item_a.get("norm", ""), item_b.get("norm", "")
    if not s_a or not s_b:
        return 0.0
    if s_a == s_b:
        return 1.0

    r1 = levenshtein_ratio(s_a, s_b)
    
    sorted_a, sorted_b = item_a.get("sorted", ""), item_b.get("sorted", "")
    if sorted_a and sorted_b and sorted_a == sorted_b:
        r2 = 1.0
    else:
        r2 = levenshtein_ratio(sorted_a, sorted_b)
        
    r3 = token_overlap_score(item_a.get("tokens", []), item_b.get("tokens", []))
    
    return max(r1, r2, r3)


def rank_candidates(target: dict[str, Any], candidates: list[dict[str, Any]],
                    top_n: int = 3, min_score: float = 0.40) -> list[dict[str, Any]]:
    """
    Find top N matching candidates for a target entry.
    Returns list of dicts: {'index': j, 'score': score, 'candidate': candidates[j]}
    """
    scored = []
    for j, cand in enumerate(candidates):
        sc = compute_name_similarity(target, cand)
        if sc >= min_score:
            scored.append({"index": j, "score": sc, "candidate": cand})
            
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_n]

