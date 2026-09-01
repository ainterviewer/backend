"""Tests for embedding clustering.

Synthetic vectors throughout: no database, no inference server, and no
dependence on what happens to be in anyone's corpus.
"""

import numpy as np
import pytest

from app.embed.clustering import (
    center_by_groups,
    cluster_vectors,
)

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


class TestQuestionPurity:
    def test_none_without_groups(self):
        ids, matrix = corpus()
        result = cluster_vectors(ids, matrix, min_cluster_size=5)

        assert all(c.question_purity is None for c in result.clusters)

    def test_detects_clusters_that_are_really_one_question(self):
        """The failure mode this exists to expose: QA-pair chunks repeat their
        interview question verbatim, so blobs form per question."""
        ids, matrix = corpus(n_groups=3, per_group=20)
        groups = [(0, i // 20) for i in range(len(ids))]

        result = cluster_vectors(ids, matrix, min_cluster_size=5, group_keys=groups)

        assert result.clusters
        assert all(c.question_purity == pytest.approx(1.0) for c in result.clusters)


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

        raw = cluster_vectors(ids, matrix, min_cluster_size=5, group_keys=groups)
        centered = cluster_vectors(
            ids, matrix, min_cluster_size=5, group_keys=groups, center_by_group=True
        )

        raw_purity = sum(c.question_purity for c in raw.clusters) / len(raw.clusters)
        centered_purity = sum(c.question_purity for c in centered.clusters) / len(
            centered.clusters
        )

        # Uncentred, clusters are questions. Centred, they cross questions.
        assert raw_purity == pytest.approx(1.0)
        assert centered_purity < 0.6
