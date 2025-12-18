import polars as pl


def fix_nested_columns(df: pl.DataFrame) -> pl.DataFrame:
    """
    Converts nested columns of type Struct or List (ie. dicts or lists) to JSON strings.
    """

    stringified_cols = []

    for col_name, col_type in df.schema.items():
        # Check if the type is Struct or List
        if isinstance(col_type, (pl.Struct, pl.List)):
            stringified_cols.append(
                pl.col(col_name).struct.json_encode().alias(col_name)
            )  # Use alias to keep original name

    # Apply the casting if any nested columns were found
    if stringified_cols:
        df = df.with_columns(stringified_cols)

    return df
