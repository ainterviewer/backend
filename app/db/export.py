from pathlib import Path

import polars as pl
from typer import Typer
from xlsxwriter import Workbook

from ..settings import app_settings

app = Typer()


@app.command()
def save_interviews_to_excel(
    project_id: str,
    output_dir: Path = Path("data/dumps"),
) -> Workbook:
    query = "SELECT * FROM message"
    print(app_settings.database.connection_string)
    exit()
    messages = pl.read_database_uri(
        uri=app_settings.database.connection_string,
        query=query,
        engine="adbc",
    )

    messages = messages.filter(messages["project_id"] == project_id.replace("-", ""))

    interview_ids = messages["interview_id"].unique()

    print(f"Number of interviews {len(interview_ids)}")

    filepath = output_dir / f"interviews_{project_id}.xlsx"

    with Workbook(filepath) as workbook:
        text_format = workbook.add_format({"text_wrap": True})

        for index, interview_id in enumerate(interview_ids):
            messages.filter(interview_id=interview_id).with_columns(
                methodological_notes=pl.lit(""), analytical_notes=pl.lit("")
            ).select(pl.exclude("id")).write_excel(
                workbook=workbook,
                worksheet=f"{index}",
                column_widths={
                    "content": 300,
                    "role": 100,
                    "created_at": 150,
                    "methodological_notes": 300,
                    "analytical_notes": 300,
                },
                column_formats={
                    "content": text_format,
                    "methodological_notes": text_format,
                    "analyitical_notes": text_format,
                },
            )

    print(f"Database exported to {filepath}")

    return workbook


if __name__ == "__main__":
    app()
