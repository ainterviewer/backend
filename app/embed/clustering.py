"""Unsupervised structure over stored embedding vectors.

Two things, computed in one pass:

- **Clusters**, from HDBSCAN on a reduced space. HDBSCAN rather than k-means
  because exploratory work does not know `k` in advance, and because it labels
  points it cannot place as outliers instead of forcing them into the nearest
  blob -- in interview data those outliers are often the interesting part.
- **2D coordinates**, for a scatter plot.

Clustering never runs on the raw 1024 dimensions, because density-based
clustering degrades badly as dimensionality rises: distances concentrate, every
point looks equidistant, and HDBSCAN finds one blob or none. `Projection`
chooses how the dimensions come down, and either way **HDBSCAN runs in exactly
the space the scatter shows**, so a cluster can never be a shape the picture
does not contain.

`Projection.PCA` reduces linearly to `n_components` (50 by default) and plots
the first two columns of that same fit -- PCA's components are nested, so this
is exactly what a 2-component PCA would produce. Milliseconds, and
`explained_variance_2d` reports how much of the total variance the two plotted
axes carry, which is the honest measure of how much the scatter can be trusted.
It is usually low for text embeddings. The weakness is the other side of that
number: 50 dimensions is still high enough for distances to concentrate, so
clusters come out few and large.

`Projection.UMAP` reduces non-linearly to 2 dimensions -- via a PCA to
`n_components` first, which denoises and is what makes the neighbour search
affordable -- and clusters in those two. Neighbourhoods separate far more
sharply, which is usually the readable picture, and it is what to reach for
when PCA returns one undifferentiated blob. Three costs, none of them hidden:
it is seconds rather than milliseconds (plus a one-off numba compile the first
time a process calls it), `explained_variance_2d` is None because UMAP has no
such quantity, and clustering in two dimensions can split one topic into
several -- UMAP will happily manufacture separation between neighbourhoods that
are not really apart. Read the purities and the representatives before
believing a split. `random_state` is fixed so two runs of the same corpus give
the same picture; that forces UMAP single-threaded, which is the deliberate
trade of speed for an analyst being able to compare two runs at all.

**Some of what an embedding encodes is scaffolding, not content**, and left
alone it dominates. Two cases, both observed on the real corpus:

- A QA-pair chunk contains its interview question verbatim, and every respondent
  was asked the same one. Clusters came out 79-100% pure by question -- the
  interview guide, rediscovered.
- A multilingual project embeds every language into the same space, and the
  model separates languages more strongly than it separates topics. On a
  Danish/English project the two largest clusters were simply Danish and
  English.

`GroupAxis` is how a caller names such a confound, and it does two things about
it. Every axis is *reported*: each cluster carries its purity along every axis,
so the pathology is visible rather than inferred -- a cluster at 0.99 on the
language axis is a language. An axis with `center=True` is additionally
*removed*: the mean vector of each group is subtracted from its members before
projection, leaving only how members of that group differ from one another.

Centering on several axes at once uses their **composite** key, so centering on
question and language subtracts the mean of each language-within-question cell
and removes both confounds in one pass. That is stronger than removing either
alone, and the cost is that thin cells lose their content -- a cell of one chunk
becomes the zero vector, honestly, since one chunk says nothing about variation
within its cell.

Centering costs no re-embedding; it is arithmetic on vectors already stored, and
it happens before either projection, so it is orthogonal to the choice between
them.

Nothing is stored. Under PCA a full recompute is milliseconds, which keeps
`min_cluster_size` an interactive control rather than a migration; under UMAP it
is seconds, which keeps it usable but not instant.
"""

import logging
from collections import Counter
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.decomposition import PCA

from ..types import Projection

logger = logging.getLogger(__name__)

DEFAULT_COMPONENTS = 50
DEFAULT_MIN_CLUSTER_SIZE = 5

#: UMAP's neighbourhood size -- the knob that trades local detail against global
#: structure. UMAP's own default, which is a reasonable middle for a corpus of
#: this size.
DEFAULT_N_NEIGHBORS = 15
#: How tightly UMAP is allowed to pack points. Lower than UMAP's 0.1 default:
#: HDBSCAN is looking for density, and slack between points blurs exactly the
#: gaps it separates on.
DEFAULT_MIN_DIST = 0.0
#: Fixed so the same corpus produces the same picture twice. UMAP falls back to
#: a single thread when seeded; comparability is worth more here than the
#: seconds.
UMAP_RANDOM_STATE = 42

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
class GroupAxis:
    """A labelling of the points that clustering should be read against.

    `keys` is one key per row of the matrix, in the same order. Rows sharing a
    key are one group -- one interview question, one language.

    Naming an axis always reports it: every cluster comes back with its purity
    along this axis. `center` additionally removes it, subtracting each group's
    mean vector before projection. Report first and centre only if the report
    says you need to: centering a group that was not a confound throws away real
    variation.
    """

    name: str
    keys: Sequence[Hashable]
    center: bool = False


@dataclass(frozen=True)
class Cluster:
    id: int
    size: int
    #: Embedding ids closest to the cluster's centre, best first. The material
    #: for naming a cluster, by eye or by a model.
    representatives: list[UUID] = field(default_factory=list)
    #: Per axis name, the share of members coming from that axis's single most
    #: common group. 1.0 on the question axis means the cluster *is* a question
    #: and says nothing about what was answered; 1.0 on the language axis means
    #: it is a language. Low values mean the cluster crosses that axis, which is
    #: the only case where it is telling you something about content. Empty when
    #: no axes were supplied.
    purity: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ClusteringResult:
    points: list[ClusteredPoint] = field(default_factory=list)
    clusters: list[Cluster] = field(default_factory=list)
    n_outliers: int = 0
    #: How the vectors were reduced -- and so how much the scatter can be read
    #: as geometry rather than as adjacency.
    projection: Projection = Projection.PCA
    #: Dimensions HDBSCAN ran in. Always 2 under UMAP.
    components: int = 0
    #: Share of total variance the two plotted axes carry. None under UMAP,
    #: which has no such quantity -- a UMAP plot's distances do not mean what a
    #: PCA plot's do, and there is no single number that says how far off they
    #: are.
    explained_variance_2d: float | None = None


def center_by_groups(matrix: np.ndarray, group_keys: Sequence[Hashable]) -> np.ndarray:
    """Subtract each group's mean vector from its members.

    For QA pairs the group is the interview question, and what this removes is
    the question text every member shares -- leaving how the answers differ,
    which is the thing worth clustering. For a multilingual project it is the
    language, and what it removes is the model's habit of separating Danish from
    English before it separates anything either of them says.

    A group with a single member becomes the zero vector: honest, since one
    member says nothing about variation within its group, but it does mean
    singleton groups collect at the origin rather than sorting by content. Worth
    watching when centering on a composite key, which makes groups thinner.
    """
    centered = matrix.astype(np.float64, copy=True)

    groups: dict[Hashable, list[int]] = {}
    for index, key in enumerate(group_keys):
        groups.setdefault(key, []).append(index)

    for indices in groups.values():
        centered[indices] -= centered[indices].mean(axis=0)

    return centered


def _project(
    features: np.ndarray,
    projection: Projection,
    *,
    n_components: int,
    n_neighbors: int,
    min_dist: float,
) -> tuple[np.ndarray, float | None]:
    """Reduce `features` to the space clustering and the scatter both use.

    Returns that space and, for PCA only, the share of variance its first two
    columns carry. Both branches guarantee the same invariant: the returned
    array's first two columns are the picture, and the whole array is what
    HDBSCAN sees, so the two can never come from different fits.
    """
    n_samples, n_features = features.shape

    # Neither reducer can produce more components than it has samples or
    # features, and the 2D scatter needs at least two.
    components = max(2, min(n_components, n_samples, n_features))

    pca = PCA(n_components=components, svd_solver="full")
    reduced = pca.fit_transform(features)

    if projection is Projection.PCA:
        return reduced, float(pca.explained_variance_ratio_[:2].sum())

    # Imported here, not at module scope: umap drags in numba, which costs
    # seconds of import and a JIT compile that a deployment never running a
    # UMAP clustering should not pay for at startup.
    from umap import UMAP

    # UMAP needs at least two neighbours, and cannot ask for more than there
    # are other points to ask about.
    neighbors = max(2, min(n_neighbors, n_samples - 1))

    embedding = UMAP(
        n_components=2,
        n_neighbors=neighbors,
        min_dist=min_dist,
        # The PCA above leaves L2-normalised vectors no longer normalised, so
        # euclidean rather than cosine -- and on a variance-preserving linear
        # map of normalised vectors the two orderings barely differ anyway.
        metric="euclidean",
        random_state=UMAP_RANDOM_STATE,
    ).fit_transform(reduced)

    return np.asarray(embedding, dtype=np.float64), None


def cluster_vectors(
    ids: list[UUID],
    matrix: np.ndarray,
    *,
    projection: Projection = Projection.PCA,
    n_components: int = DEFAULT_COMPONENTS,
    n_neighbors: int = DEFAULT_N_NEIGHBORS,
    min_dist: float = DEFAULT_MIN_DIST,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    min_samples: int | None = None,
    n_representatives: int = 3,
    axes: Sequence[GroupAxis] = (),
) -> ClusteringResult:
    """Cluster `matrix` (one L2-normalised row per id) and project it to 2D.

    `n_components`, `n_neighbors` and `min_dist` are all read under UMAP --
    the first sizes the PCA it reduces through, the other two are its own. Only
    `n_components` is read under PCA.

    Every axis in `axes` is reported as a per-cluster purity; those marked
    `center` are removed first, on their composite key.
    """
    n_samples = len(ids)
    if n_samples == 0:
        return ClusteringResult(projection=projection)

    features = matrix.astype(np.float64)

    centered_axes = [axis for axis in axes if axis.center]
    if centered_axes:
        # One composite key rather than one pass per axis: centering on
        # question and then on language would subtract each language's mean
        # across all questions, which is not the same thing as -- and weaker
        # than -- removing the mean of each language-within-question cell.
        features = center_by_groups(
            features, list(zip(*(axis.keys for axis in centered_axes)))
        )

    reduced, explained_2d = _project(
        features,
        projection,
        n_components=n_components,
        n_neighbors=n_neighbors,
        min_dist=min_dist,
    )
    components = reduced.shape[1]

    # The scatter is the first two columns of the space clustering runs in, so
    # the picture and the clustering cannot come from different fits.
    coords = reduced[:, :2]

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
            projection=projection,
            components=components,
            explained_variance_2d=explained_2d,
        )

    hdbscan = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        # Euclidean on a PCA of L2-normalised vectors is monotonic with cosine
        # distance, which is the metric the embeddings were trained for; in a
        # UMAP embedding euclidean is the only distance that means anything at
        # all, the axes having no units of their own.
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
        _describe_cluster(label, ids, reduced, labels, n_representatives, axes)
        for label in sorted({int(x) for x in labels if x != OUTLIER})
    ]
    clusters.sort(key=lambda c: c.size, reverse=True)

    return ClusteringResult(
        points=points,
        clusters=clusters,
        n_outliers=int((labels == OUTLIER).sum()),
        projection=projection,
        components=components,
        explained_variance_2d=explained_2d,
    )


def _describe_cluster(
    label: int,
    ids: list[UUID],
    reduced: np.ndarray,
    labels: np.ndarray,
    n_representatives: int,
    axes: Sequence[GroupAxis] = (),
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

    purity = {}
    for axis in axes:
        if not len(member_indices):
            continue
        counts = Counter(axis.keys[int(i)] for i in member_indices)
        purity[axis.name] = counts.most_common(1)[0][1] / len(member_indices)

    return Cluster(
        id=label,
        size=len(member_indices),
        representatives=representatives,
        purity=purity,
    )
