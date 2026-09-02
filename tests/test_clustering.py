"""Tests for embedding clustering.

Synthetic vectors throughout: no database, no inference server, and no
dependence on what happens to be in anyone's corpus.
"""

import numpy as np
import pytest

from app.embed.clustering import (
    GroupAxis,
    center_by_groups,
    cluster_vectors,
)
from app.types import Projection

RNG = np.random.default_rng(0)


def blob(centre: np.ndarray, n: int, spread: float = 0.02) -> np.ndarray:
    """`n` points scattered tightly around `centre`, L2-normalised as the
    embedding server's output always is."""
    points = centre + RNG.normal(0, spread, size=(n, centre.shape[0]))
    return points / np.linalg.norm(points, axis=1, keepdims=True)


def corpus(n_groups: int = 3, per_group: int = 20, dim: int = 64):
    centres = RNG.normal(0, 1, size=(n_groups, dim))
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    matrix = np.vstack([blob(c, per_group) for c in centres])
    ids = [f"id-{i}" for i in range(len(matrix))]
    return ids, matrix


class TestClusterVectors:
    def test_empty_input(self):
        result = cluster_vectors([], np.empty((0, 0)))
        assert result.points == []
        assert result.clusters == []
        assert result.n_outliers == 0

    def test_finds_separated_blobs(self):
        ids, matrix = corpus(n_groups=3, per_group=20)
        result = cluster_vectors(ids, matrix, min_cluster_size=5)

        assert len(result.clusters) == 3
        assert sum(c.size for c in result.clusters) + result.n_outliers == len(ids)

    def test_every_point_is_returned_exactly_once(self):
        ids, matrix = corpus()
        result = cluster_vectors(ids, matrix, min_cluster_size=5)

        assert [p.embedding_id for p in result.points] == ids

    def test_outliers_are_kept_not_dropped(self):
        """Points HDBSCAN will not place come back labelled, not deleted."""
        ids, matrix = corpus(n_groups=2, per_group=25)
        # Sparse noise spread over the sphere: nowhere near dense enough to
        # form a cluster of its own, which is what makes it noise.
        noise = RNG.normal(0, 1, size=(15, matrix.shape[1]))
        noise /= np.linalg.norm(noise, axis=1, keepdims=True)
        matrix = np.vstack([matrix, noise])
        ids = ids + [f"noise-{i}" for i in range(len(noise))]

        result = cluster_vectors(ids, matrix, min_cluster_size=5)
        outliers = [p for p in result.points if p.cluster is None]

        assert outliers, "expected some points to be unplaceable"
        assert len(result.points) == len(ids)
        assert all(p.probability == 0.0 for p in outliers)
        assert result.n_outliers == len(outliers)

    def test_coordinates_come_from_the_clustering_projection(self):
        """The scatter must be the first two components of the same PCA, so the
        picture cannot come from a different fit than the clusters."""
        ids, matrix = corpus()
        result = cluster_vectors(ids, matrix, n_components=50, min_cluster_size=5)

        assert result.projection is Projection.PCA
        assert result.components == min(50, len(ids), matrix.shape[1])
        assert 0.0 <= result.explained_variance_2d <= 1.0

    def test_deterministic(self):
        ids, matrix = corpus()
        first = cluster_vectors(ids, matrix, min_cluster_size=5)
        second = cluster_vectors(ids, matrix, min_cluster_size=5)

        assert [p.cluster for p in first.points] == [p.cluster for p in second.points]
        assert [p.x for p in first.points] == [p.x for p in second.points]

    def test_representatives_are_members_of_their_cluster(self):
        ids, matrix = corpus()
        result = cluster_vectors(ids, matrix, min_cluster_size=5, n_representatives=3)
        placed = {p.embedding_id: p.cluster for p in result.points}

        for cluster in result.clusters:
            assert cluster.representatives
            assert len(cluster.representatives) <= 3
            assert all(placed[r] == cluster.id for r in cluster.representatives)

    def test_too_few_points_still_projects(self):
        ids, matrix = ["a", "b"], RNG.normal(0, 1, size=(2, 16))
        result = cluster_vectors(ids, matrix, min_cluster_size=5)

        assert len(result.points) == 2
        assert result.clusters == []
        assert result.n_outliers == 2

    def test_clusters_are_ordered_by_size(self):
        ids, matrix = corpus(n_groups=3, per_group=20)
        result = cluster_vectors(ids, matrix, min_cluster_size=5)
        sizes = [c.size for c in result.clusters]

        assert sizes == sorted(sizes, reverse=True)


class TestPurity:
    def test_empty_without_axes(self):
        ids, matrix = corpus()
        result = cluster_vectors(ids, matrix, min_cluster_size=5)

        assert all(c.purity == {} for c in result.clusters)

    def test_detects_clusters_that_are_really_one_question(self):
        """The failure mode this exists to expose: QA-pair chunks repeat their
        interview question verbatim, so blobs form per question."""
        ids, matrix = corpus(n_groups=3, per_group=20)
        axis = GroupAxis("question", [(0, i // 20) for i in range(len(ids))])

        result = cluster_vectors(ids, matrix, min_cluster_size=5, axes=[axis])

        assert result.clusters
        assert all(c.purity["question"] == pytest.approx(1.0) for c in result.clusters)

    def test_reports_every_axis_independently(self):
        """An axis is reported whether or not it is centred, and a cluster can
        be pure on one axis while crossing another -- which is the whole point
        of reporting them separately."""
        ids, matrix = corpus(n_groups=3, per_group=20)
        question = GroupAxis("question", [(0, i // 20) for i in range(len(ids))])
        # Alternating, so it cuts across the blobs rather than following them.
        language = GroupAxis("language", ["DA" if i % 2 else "EN" for i in range(60)])

        result = cluster_vectors(
            ids, matrix, min_cluster_size=5, axes=[question, language]
        )

        assert result.clusters
        for cluster in result.clusters:
            assert set(cluster.purity) == {"question", "language"}
            assert cluster.purity["question"] == pytest.approx(1.0)
            assert cluster.purity["language"] < 0.75


class TestCenterByGroups:
    def test_group_means_are_removed(self):
        matrix = np.array([[1.0, 0.0], [3.0, 0.0], [0.0, 10.0], [0.0, 20.0]])
        groups = ["a", "a", "b", "b"]

        centered = center_by_groups(matrix, groups)

        assert centered[:2].mean(axis=0) == pytest.approx([0.0, 0.0])
        assert centered[2:].mean(axis=0) == pytest.approx([0.0, 0.0])
        # Within-group variation survives; that is the point.
        assert centered[0][0] == pytest.approx(-1.0)
        assert centered[1][0] == pytest.approx(1.0)

    def test_singleton_group_becomes_zero(self):
        matrix = np.array([[5.0, 5.0]])
        assert center_by_groups(matrix, ["only"])[0].tolist() == pytest.approx(
            [0.0, 0.0]
        )

    def test_does_not_mutate_the_input(self):
        matrix = np.array([[1.0, 2.0], [3.0, 4.0]])
        original = matrix.copy()

        center_by_groups(matrix, ["a", "a"])

        assert np.array_equal(matrix, original)

    def test_centering_breaks_up_question_shaped_clusters(self):
        """Centering turns "these are all the same question" into "these
        answers resemble each other".

        Points are left un-normalised here on purpose. Centering is a linear
        operation, and L2-normalising each point first would rescale the shared
        answer direction differently per question -- an artefact of building
        synthetic data on a sphere, not something centering does wrong. The
        real-corpus behaviour is in the module docstring.
        """
        dim = 64
        # Three questions, each asked of 20 people; within each, two opposing
        # answer directions shared across all three questions.
        question_centres = RNG.normal(0, 1, size=(3, dim)) * 5
        answer_axis = RNG.normal(0, 1, size=(2, dim))

        rows, groups = [], []
        for q, centre in enumerate(question_centres):
            for i in range(20):
                rows.append(centre + answer_axis[i % 2] + RNG.normal(0, 0.05, dim))
                groups.append((0, q))

        matrix = np.vstack(rows)
        ids = [f"id-{i}" for i in range(len(matrix))]

        raw = cluster_vectors(
            ids, matrix, min_cluster_size=5, axes=[GroupAxis("question", groups)]
        )
        centered = cluster_vectors(
            ids,
            matrix,
            min_cluster_size=5,
            axes=[GroupAxis("question", groups, center=True)],
        )

        raw_purity = sum(c.purity["question"] for c in raw.clusters) / len(raw.clusters)
        centered_purity = sum(c.purity["question"] for c in centered.clusters) / len(
            centered.clusters
        )

        # Uncentred, clusters are questions. Centred, they cross questions.
        assert raw_purity == pytest.approx(1.0)
        assert centered_purity < 0.6


class TestUmapProjection:
    """UMAP is the non-linear alternative: the scatter *is* the clustering
    space, two dimensions wide, and there is no variance ratio to report.

    Kept to a handful of cases on purpose -- a UMAP fit is seconds, and the
    behaviour that matters here is the contract, not the layout it happens to
    produce.
    """

    def test_clusters_in_the_two_plotted_dimensions(self):
        ids, matrix = corpus(n_groups=3, per_group=20)
        result = cluster_vectors(
            ids, matrix, projection=Projection.UMAP, min_cluster_size=5
        )

        assert result.projection is Projection.UMAP
        # Two, and only two: unlike PCA there is nothing behind the picture.
        assert result.components == 2
        # No linear variance to account for, so nothing is claimed.
        assert result.explained_variance_2d is None
        assert [p.embedding_id for p in result.points] == ids
        assert sum(c.size for c in result.clusters) + result.n_outliers == len(ids)

    def test_separates_blobs_at_least_as_well_as_pca(self):
        ids, matrix = corpus(n_groups=4, per_group=20)
        result = cluster_vectors(
            ids, matrix, projection=Projection.UMAP, min_cluster_size=5
        )

        assert len(result.clusters) == 4

    def test_deterministic(self):
        """Seeded, so an analyst can compare two runs of the same corpus.

        Worth asserting rather than assuming: UMAP is stochastic by default and
        drops to a single thread precisely to honour the seed.
        """
        ids, matrix = corpus(n_groups=3, per_group=20)
        first = cluster_vectors(
            ids, matrix, projection=Projection.UMAP, min_cluster_size=5
        )
        second = cluster_vectors(
            ids, matrix, projection=Projection.UMAP, min_cluster_size=5
        )

        assert [p.x for p in first.points] == [p.x for p in second.points]
        assert [p.cluster for p in first.points] == [p.cluster for p in second.points]

    def test_too_few_points_still_projects(self):
        """Fewer points than UMAP's default neighbourhood: it must clamp rather
        than raise, since a filter can leave a project with three chunks."""
        ids, matrix = corpus(n_groups=1, per_group=4)
        result = cluster_vectors(
            ids, matrix, projection=Projection.UMAP, min_cluster_size=5
        )

        assert len(result.points) == 4
        assert result.n_outliers == 4

    def test_empty_input_reports_its_projection(self):
        result = cluster_vectors([], np.empty((0, 0)), projection=Projection.UMAP)

        assert result.projection is Projection.UMAP
        assert result.points == []


class TestCenteringOnSeveralAxes:
    """Centering on question *and* language must remove both confounds, which
    means using their composite key rather than one pass per axis."""

    @staticmethod
    def confounded_corpus(dim: int = 64):
        """Three questions x two languages, with two content directions shared
        across every cell.

        The content is what an analyst wants back; the question and language
        offsets are scaffolding, and both are made larger than the content so
        that uncentred clustering cannot help but recover them.
        """
        question_centres = RNG.normal(0, 1, size=(3, dim)) * 5
        language_offsets = RNG.normal(0, 1, size=(2, dim)) * 5
        content_axis = RNG.normal(0, 1, size=(2, dim))

        rows, questions, languages = [], [], []
        for q, centre in enumerate(question_centres):
            for lang in (0, 1):
                for i in range(15):
                    rows.append(
                        centre
                        + language_offsets[lang]
                        + content_axis[i % 2]
                        + RNG.normal(0, 0.05, dim)
                    )
                    questions.append((0, q))
                    languages.append("DA" if lang else "EN")

        matrix = np.vstack(rows)
        ids = [f"id-{i}" for i in range(len(matrix))]
        return ids, matrix, questions, languages

    def run(self, center_question: bool, center_language: bool):
        ids, matrix, questions, languages = self.confounded_corpus()
        result = cluster_vectors(
            ids,
            matrix,
            min_cluster_size=5,
            axes=[
                GroupAxis("question", questions, center=center_question),
                GroupAxis("language", languages, center=center_language),
            ],
        )
        assert result.clusters
        n = len(result.clusters)
        return (
            sum(c.purity["question"] for c in result.clusters) / n,
            sum(c.purity["language"] for c in result.clusters) / n,
        )

    def test_uncentred_recovers_the_scaffolding(self):
        question, language = self.run(False, False)

        assert question == pytest.approx(1.0)
        assert language == pytest.approx(1.0)

    def test_centring_one_axis_leaves_the_other(self):
        """Centring only by question does not fix language.

        This is the case that produced the Danish/English clusters on the real
        corpus: `center_by_question` was on, and the two biggest clusters were
        still just the two languages.
        """
        _, language = self.run(True, False)

        assert language == pytest.approx(1.0)

    def test_centring_both_removes_both(self):
        question, language = self.run(True, True)

        assert question < 0.75
        assert language < 0.75
