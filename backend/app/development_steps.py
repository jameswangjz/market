"""Packaged task definitions only; substep progress is not tracked in the DB."""
from functools import cache
import json
from pathlib import Path
from typing import Literal, TypedDict


class DevelopmentSubtask(TypedDict):
    id: str
    title: str
    status: Literal['untracked']


@cache
def _definitions() -> dict[str, tuple[str, ...]]:
    document = json.loads(Path(__file__).with_suffix('.json').read_text(encoding='utf-8'))
    tasks = document['tasks']
    if not isinstance(tasks, dict):
        raise ValueError('Development step definitions must be keyed by task code')
    definitions = {}
    for code, titles in tasks.items():
        if (not isinstance(code, str) or not code
                or not isinstance(titles, list)
                or any(not isinstance(title, str) or not title.strip() for title in titles)):
            raise ValueError('Invalid development step definition')
        definitions[code] = tuple(titles)
    return definitions


def subtasks_for(code: str) -> list[DevelopmentSubtask]:
    return [{'id': f'{code}-S{index:02d}', 'title': title, 'status': 'untracked'}
            for index, title in enumerate(_definitions().get(code, ()), start=1)]
