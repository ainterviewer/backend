"""The report's cards for a ranking and a list item.

A ranking counts each option's ranks, placed by position in the options the
respondent saw so one option counts as one across languages; a list counts
the entries respondents commonly gave."""

from ainterviewer.interview_guides import ListItem, RankingItem
from app.api.dashboard.analysis.report import (
    MAX_LIST_ENTRIES,
    _Answer,
    _list_entries,
    _ranking_options,
)

OPTIONS = ["Price", "Quality", "Delivery", "Brand"]


def _ranked(item: RankingItem, ranked: list[str], language: str = "EN") -> _Answer:
    return _Answer(item.encode_answer(ranked), language, item.options, item)


def test_ranks_are_counted_per_option_best_ranked_first():
    item = RankingItem(options=OPTIONS)
    danish = RankingItem(options=[f"{option} (DA)" for option in OPTIONS])
    options = _ranking_options(
        item,
        [
            _ranked(item, ["Quality", "Price", "Delivery", "Brand"]),
            _ranked(item, ["Quality", "Brand", "Price", "Delivery"]),
            # Placed by position in the options this respondent saw.
            _ranked(
                danish,
                ["Price (DA)", "Quality (DA)", "Brand (DA)", "Delivery (DA)"],
                language="DA",
            ),
        ],
    )
    assert [option.label for option in options] == [
        "Quality",
        "Price",
        "Brand",
        "Delivery",
    ]
    quality, price = options[0], options[1]
    assert [bar.count for bar in quality.counts] == [2, 1, 0, 0]
    assert quality.mean_rank == 4 / 3
    assert price.counts[0].by_language == {"DA": 1}
    assert [bar.label for bar in price.counts] == ["1", "2", "3", "4"]


def test_a_top_n_ranking_counts_only_its_ranks_and_options_nobody_ranked_go_last():
    item = RankingItem(options=OPTIONS, top_n=2)
    options = _ranking_options(
        item,
        [_ranked(item, ["Brand", "Price"]), _ranked(item, ["Price", "Brand"])],
    )
    assert [option.label for option in options] == [
        "Price",
        "Brand",
        "Quality",
        "Delivery",
    ]
    assert all(len(option.counts) == 2 for option in options)
    assert options[2].n_ranked == 0 and options[2].mean_rank is None


def test_an_edited_ranking_falls_back_to_matching_the_text():
    item = RankingItem(options=OPTIONS)
    older = RankingItem(options=["Brand", "Price"])
    options = _ranking_options(item, [_ranked(older, ["Brand", "Price"])])
    by_label = {option.label: option for option in options}
    assert by_label["Brand"].counts[0].count == 1
    assert by_label["Price"].counts[1].count == 1


def _listed(item: ListItem, entries: list[str], language: str = "EN") -> _Answer:
    return _Answer(item.encode_answer(entries), language, None, item)


def test_list_entries_are_counted_once_per_answer_on_their_normalized_form():
    item = ListItem()
    counts, hidden, hidden_count, sizes = _list_entries(
        item,
        [
            _listed(item, ["Slack", "Figma", "slack"]),
            _listed(item, ["SLACK ", "Notion"]),
            _listed(item, ["Figma"], language="DA"),
        ],
    )
    assert [(bar.label, bar.count) for bar in counts] == [
        ("Figma", 2),
        ("Slack", 2),
        ("Notion", 1),
    ]
    assert counts[0].by_language == {"EN": 1, "DA": 1}
    assert (hidden, hidden_count) == (0, 0)
    assert [size.value for size in sizes] == [3, 2, 1]


def test_list_entries_past_the_limit_are_reported_as_a_tail():
    item = ListItem()
    answers = [_listed(item, [f"app {n}"]) for n in range(MAX_LIST_ENTRIES + 3)]
    counts, hidden, hidden_count, _ = _list_entries(item, answers)
    assert len(counts) == MAX_LIST_ENTRIES
    assert (hidden, hidden_count) == (3, 3)
