"""Small, standard-library-only statistics for offline score analysis.

Correlations use only positions containing two finite int/float values; missing
or invalid values are omitted as pairs, never replaced by zero. Booleans are not
scores. Unequal input lengths are errors. Undefined correlations have a null
value and a reason, so all results can be serialized with ``allow_nan=False``.
"""
import math


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _paired(x, y):
    x, y = list(x), list(y)
    if len(x) != len(y):
        raise ValueError('paired inputs must have equal lengths')
    pairs = [(a, b) for a, b in zip(x, y) if _finite_number(a) and _finite_number(b)]
    return [a for a, _ in pairs], [b for _, b in pairs]


def _result(value, n, reason=None):
    return {'value': value, 'n': n, 'reason': reason}


def average_ranks(values, descending=True):
    """Return one-based, average tied ranks in the original input order.

    Unlike correlations, ranking cannot silently drop positions. It requires
    every value to be finite and raises ValueError for missing/invalid values.
    Ties mean exact equality of the stored numeric scores, without rounding.
    """
    values = list(values)
    if any(not _finite_number(value) for value in values):
        raise ValueError('ranking requires finite numeric values')
    order = sorted(range(len(values)), key=values.__getitem__, reverse=descending)
    ranks = [0.] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + 1 + end) / 2.
        for position in range(start, end):
            ranks[order[position]] = rank
        start = end
    return ranks


def _centered_unit(values):
    # Subtract an observed value first: a mean near a large common offset may
    # round to one endpoint, destroying the symmetry of adjacent float scores.
    # Differences retain that variation before their own mean is calculated.
    anchor = values[0]
    shifted = [value - anchor for value in values]
    if any(not _finite_number(value) for value in shifted):
        # Opposite extreme signs can overflow during subtraction. Only in this
        # wide-range case, scale the original observations before translating.
        source_scale = max(abs(value) for value in values)
        shifted = [value / source_scale - anchor / source_scale for value in values]
    scale = max(abs(value) for value in shifted)
    if scale == 0:
        return None
    # Scaling the differences also protects subnormal variation and avoids
    # overflowing their sum. A second scale keeps squared deviations stable.
    scaled = [value / scale for value in shifted]
    center = math.fsum(scaled) / len(scaled)
    centered = [value - center for value in scaled]
    spread = max(abs(value) for value in centered)
    if spread == 0:
        return None
    return [value / spread for value in centered]


def pearson(x, y):
    """Pearson product-moment correlation of finite paired observations."""
    x, y = _paired(x, y)
    n = len(x)
    if n < 2:
        return _result(None, n, 'insufficient_pairs')
    x, y = _centered_unit(x), _centered_unit(y)
    if x is None or y is None:
        return _result(None, n, 'constant_input')
    numerator = math.fsum(a * b for a, b in zip(x, y))
    denominator = math.sqrt(math.fsum(a * a for a in x)) * math.sqrt(math.fsum(b * b for b in y))
    # Floating-point roundoff must not report a correlation outside [-1, 1].
    return _result(max(-1., min(1., numerator / denominator)), n)


def spearman(x, y):
    """Pearson correlation of average tied ranks after pairwise filtering."""
    x, y = _paired(x, y)
    return pearson(average_ranks(x), average_ranks(y))


def kendall_tau_b(x, y):
    """Tie-corrected Kendall tau-b using O(n**2) time and O(n) storage.

    Joint ties contribute to neither denominator factor. The direct pair count
    is intended for benchmark case sets, rather than very large population data.
    """
    x, y = _paired(x, y)
    n = len(x)
    if n < 2:
        return _result(None, n, 'insufficient_pairs')
    concordant = discordant = tied_x = tied_y = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            dx = (x[i] > x[j]) - (x[i] < x[j])
            dy = (y[i] > y[j]) - (y[i] < y[j])
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                tied_x += 1
            elif dy == 0:
                tied_y += 1
            elif dx == dy:
                concordant += 1
            else:
                discordant += 1
    comparable = concordant + discordant
    denominator = math.sqrt((comparable + tied_x) * (comparable + tied_y))
    if denominator == 0:
        return _result(None, n, 'constant_input')
    return _result((concordant - discordant) / denominator, n)
