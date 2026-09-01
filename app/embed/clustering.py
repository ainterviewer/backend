"""Unsupervised structure over stored embedding vectors.

Two things, computed in one pass:

- **Clusters**, from HDBSCAN on a PCA-reduced space. HDBSCAN rather than k-means
  because exploratory work does not know `k` in advance, and because it labels
  points it cannot place as outliers instead of forcing them into the nearest
  blob -- in interview data those outliers are often the interesting part.
- **2D coordinates**, for a scatter plot.

Clustering runs on the first `n_components` principal components (50 by
default), not on the raw 1024 dimensions, because density-based clustering
degrades badly as dimensionality rises: distances concentrate, every point looks
equidistant, and HDBSCAN finds one blob or none. The 2D coordinates are the
first two columns of that *same* projection rather than a separate fit -- PCA's
components are nested, so this is exactly what a 2-component PCA would produce,
and it guarantees the picture is a faithful sub-projection of the space the
clusters were actually found in.

They can still disagree: HDBSCAN separates using all 50 dimensions, so two
clusters may overlap on screen while being cleanly apart in the space that
matters. `explained_variance_2d` reports how much of the total variance those
two axes carry, which is the honest measure of how much the scatter can be
trusted. It is usually low for text embeddings; a plot is a navigation aid here,
not evidence.

**A QA-pair chunk contains its interview question verbatim, and every
respondent was asked the same one.** Left alone that shared text dominates the
vector, and clustering rediscovers the interview guide rather than anything
respondents said -- measured on real data, clusters came out 79-100% pure by
question. Two things address it, and neither is a silent correction:
`question_purity` is reported per cluster so the pathology is visible rather
than inferred, and `center_by_group` subtracts each question's mean vector
before clustering, removing the shared component and leaving the variation
between answers. Centering costs no re-embedding -- it is arithmetic on vectors
already stored.

Nothing is stored. At this corpus size a full recompute is milliseconds, which
keeps `min_cluster_size` an interactive control rather than a migration.
"""

import logging
from collections import Counter
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.decomposition import PCA

logger = logging.getLogger(__name__)

DEFAULT_COMPONENTS = 50
DEFAULT_MIN_CLUSTER_SIZE = 5

#: HDBSCAN's label for a point it will not assign.
OUTLIER = -1


@dataclass(frozen=True)
class ClusteredPoint:
    embedding_id: UUID
    cluster: int | None
    probability: float
    x: float
    y: float


@dataclass(frozen=True)
class Cluster:
    id: int
    size: int
    #: Embedding ids closest to the cluster's centre, best first. The material
    #: for naming a cluster, by eye or by a model.
    representatives: list[UUID] = field(default_factory=list)
    #: Share of members coming from the single most common group -- for QA
    #: pairs, one interview question. 1.0 means the cluster *is* a question and
    #: says nothing about what was answered; low values mean it found something
    #: that crosses questions. None when no groups were supplied.
    question_purity: float | None = None


@dataclass(frozen=True)
class ClusteringResult:
    points: list[ClusteredPoint] = field(default_factory=list)
    clusters: list[Cluster] = field(default_factory=list)
    n_outliers: int = 0
    components: int = 0
    explained_variance_2d: float = 0.0


def center_by_groups(matrix: np.ndarray, group_keys: Sequence[Hashable]) -> np.ndarray:
    """Subtract each group's mean vector from its members.

    For QA pairs the group is the interview question, and what this removes is
    the question text every member shares -- leaving how the answers differ,
    which is the thing worth clustering. A group with a single member becomes
    the zero vector: honest, since one answer says nothing about variation
    within its question, but it does mean singleton questions collect at the
    origin rather than sorting by content.
    """
    centered = matrix.astype(np.float64, copy=True)

    groups: dict[Hashable, list[int]] = {}
    for index, key in enumerate(group_keys):
        groups.setdefault(key, []).append(index)

    for indices in groups.values():
        centered[indices] -= centered[indices].mean(axis=0)

    return centered


def cluster_vectors(
    ids: list[UUID],
    matrix: np.ndarray,
    *,
    n_components: int = DEFAULT_COMPONENTS,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    min_samples: int | None = None,
    n_representatives: int = 3,
    group_keys: Sequence[Hashable] | None = None,
    center_by_group: bool = False,
) -> ClusteringResult:
    """Cluster `matrix` (one L2-normalised row per id) and project it to 2D."""
    n_samples = len(ids)
    if n_samples == 0:
        return ClusteringResult()

    # PCA cannot produce more components than it has samples or features, and
    # the 2D projection needs at least two.
    components = max(2, min(n_components, n_samples, matrix.shape[1]))

    features = matrix.astype(np.float64)
    if center_by_group and group_keys is not None:
        features = center_by_groups(features, group_keys)

    pca = PCA(n_components=components, svd_solver="full")
    reduced = pca.fit_transform(features)

    # The scatter is the first two columns of the same projection, so the
    # picture and the clustering cannot come from different fits.
    coords = reduced[:, :2]
    explained_2d = float(pca.explained_variance_ratio_[:2].sum())

    if n_samples < max(min_cluster_size, 2):
        # Too few points for HDBSCAN to say anything. Return the projection
        # anyway: a scatter of a handful of points is still worth showing.
        logger.debug("Only %s point(s); skipping clustering", n_samples)
        return ClusteringResult(
            points=[
                ClusteredPoint(
                    embedding_id=embedding_id,
                    cluster=None,
                    probability=0.0,
                    x=float(coords[i, 0]),
                    y=float(coords[i, 1]),
                )
                for i, embedding_id in enumerate(ids)
            ],
            n_outliers=n_samples,
            components=components,
            explained_variance_2d=explained_2d,
        )

    hdbscan = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        # Euclidean on a PCA of L2-normalised vectors is monotonic with cosine
        # distance, which is the metric the embeddings were trained for.
        metric="euclidean",
        # Explicit: the default flips in scikit-learn 1.10, and `reduced` is
        # ours alone so there is nothing to protect from being written to.
        copy=False,
    )
    labels = hdbscan.fit_predict(reduced)
    probabilities = hdbscan.probabilities_

    points = [
        ClusteredPoint(
            embedding_id=embedding_id,
            cluster=None if labels[i] == OUTLIER else int(labels[i]),
            probability=float(probabilities[i]),
            x=float(coords[i, 0]),
            y=float(coords[i, 1]),
        )
        for i, embedding_id in enumerate(ids)
    ]

    clusters = [
        _describe_cluster(label, ids, reduced, labels, n_representatives, group_keys)
        for label in sorted({int(x) for x in labels if x != OUTLIER})
    ]
    clusters.sort(key=lambda c: c.size, reverse=True)

    return ClusteringResult(
        points=points,
        clusters=clusters,
        n_outliers=int((labels == OUTLIER).sum()),
        components=components,
        explained_variance_2d=explained_2d,
    )


def _describe_cluster(
    label: int,
    ids: list[UUID],
    reduced: np.ndarray,
    labels: np.ndarray,
    n_representatives: int,
    group_keys: Sequence[Hashable] | None = None,
) -> Cluster:
    """Size a cluster and pick the members nearest its centre.

    Centre-nearest rather than arbitrary members: those are the points that
    actually characterise the cluster, and they are what a reader (or a model
    asked to name it) should be shown.
    """
    member_indices = np.flatnonzero(labels == label)
    members = reduced[member_indices]
    centroid = members.mean(axis=0)

    order = np.argsort(np.linalg.norm(members - centroid, axis=1))
    representatives = [ids[member_indices[i]] for i in order[:n_representatives]]

    purity = None
    if group_keys is not None and len(member_indices):
        counts = Counter(group_keys[int(i)] for i in member_indices)
        purity = counts.most_common(1)[0][1] / len(member_indices)

    return Cluster(
        id=label,
        size=len(member_indices),
        representatives=representatives,
        question_purity=purity,
    )
