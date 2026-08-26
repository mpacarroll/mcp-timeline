"""Turn raw location fixes into stays and movement.

Google Timeline shipped its own inference: it decided you "visited" a place
for seven hours and wrote that as a visit row. A continuous location feed
like OwnTracks does not. It gives breadcrumbs, and something has to decide
which runs of breadcrumbs mean "stayed here" rather than "passed through".
That is what this module does, so raw fixes can answer the same questions
Google's visit rows used to.

Deliberately done at query time rather than at ingest:

- The parameters are judgment calls, not facts. A 100m/10min threshold
  splits a coffee stop from a walk past a cafe differently than 50m/20min
  would, and neither is objectively right. Deriving at query time means
  changing your mind costs nothing, while baking the answer into ingest
  would mean re-importing to try a different one.
- The raw fixes stay the source of truth, so a bug here loses no data.
"""

from datetime import timedelta

from geo import haversine_m

DEFAULT_RADIUS_M = 100
DEFAULT_MIN_DWELL_MIN = 10


def _centroid(cluster):
    n = len(cluster)
    return (sum(p[1] for p in cluster) / n, sum(p[2] for p in cluster) / n)


def _cluster_points(points, radius_m):
    """Group consecutive fixes that stay within radius_m of their own
    running centroid. Returns a list of clusters, each a list of points."""
    clusters = []
    current = []
    for point in points:
        if not current:
            current = [point]
            continue
        lat, lng = _centroid(current)
        if haversine_m(lat, lng, point[1], point[2]) <= radius_m:
            current.append(point)
        else:
            clusters.append(current)
            current = [point]
    if current:
        clusters.append(current)
    return clusters


def _span_minutes(cluster):
    return (cluster[-1][0] - cluster[0][0]).total_seconds() / 60


def _path_length_m(points):
    return sum(haversine_m(a[1], a[2], b[1], b[2])
               for a, b in zip(points, points[1:]))


def derive_entries(points, radius_m=DEFAULT_RADIUS_M,
                   min_dwell_min=DEFAULT_MIN_DWELL_MIN, gap_min=None):
    """Turn sorted (datetime, lat, lng) fixes into stays and movement.

    Returns a chronological list of dicts, each either:
      {"type": "stay", "lat", "lng", "start", "end", "duration_min", "fixes"}
      {"type": "movement", "start", "end", "duration_min", "km", "fixes"}

    A cluster of fixes that holds within radius_m for at least
    min_dwell_min becomes a stay. Everything else is movement, with
    consecutive non-stay clusters merged into one segment so a walk does
    not come back as fifty one-point hops.

    gap_min, when set, splits movement wherever the feed goes quiet for
    that long. Without it a phone that stops reporting overnight and
    resumes across town produces one absurd "movement" covering the gap.
    """
    points = sorted(points, key=lambda p: p[0])
    if not points:
        return []

    entries = []
    pending_transit = []

    def flush_transit():
        """Emit accumulated non-stay clusters as movement segments."""
        if not pending_transit:
            return
        runs = [pending_transit] if gap_min is None else _split_on_gaps(
            pending_transit, gap_min)
        for run in runs:
            if len(run) < 2:
                # A single isolated fix is a position, not a journey.
                continue
            length_m = _path_length_m(run)
            if length_m < radius_m:
                # Fixes that never travel as far as the clustering radius
                # went nowhere: GPS jitter while stationary, or a pause too
                # short to count as a stay. Reporting that as movement would
                # tell the reader they travelled when they did not, which is
                # worse than staying quiet about it.
                continue
            entries.append({
                "type": "movement",
                "start": run[0][0],
                "end": run[-1][0],
                "duration_min": round((run[-1][0] - run[0][0]).total_seconds() / 60, 1),
                "km": round(length_m / 1000, 2),
                "fixes": len(run)})
        pending_transit.clear()

    for cluster in _cluster_points(points, radius_m):
        if _span_minutes(cluster) >= min_dwell_min:
            flush_transit()
            lat, lng = _centroid(cluster)
            entries.append({
                "type": "stay",
                "lat": round(lat, 6), "lng": round(lng, 6),
                "start": cluster[0][0],
                "end": cluster[-1][0],
                "duration_min": round(_span_minutes(cluster), 1),
                "fixes": len(cluster)})
        else:
            pending_transit.extend(cluster)
    flush_transit()
    return entries


def _split_on_gaps(points, gap_min):
    """Split a run of fixes wherever the feed went quiet for gap_min."""
    runs, current = [], [points[0]]
    limit = timedelta(minutes=gap_min)
    for prev, point in zip(points, points[1:]):
        if point[0] - prev[0] > limit:
            runs.append(current)
            current = [point]
        else:
            current.append(point)
    runs.append(current)
    return runs
