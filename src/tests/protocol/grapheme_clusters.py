import regex  # type: ignore[import-untyped]  # regex ships without type information

GRAPHEME_CLUSTER_REGULAR_EXPRESSION = regex.compile(r"\X")


def split_into_grapheme_clusters(text: str) -> list[str]:
    """Extended grapheme clusters of UAX #29, the unit Swift calls a Character."""
    grapheme_clusters: list[str] = GRAPHEME_CLUSTER_REGULAR_EXPRESSION.findall(text)
    return grapheme_clusters
