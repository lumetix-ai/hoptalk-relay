import pytest

from tests.protocol.shared_vectors import (
    REGENERATION_COMMAND,
    VECTOR_FILE_BUILDERS,
    VECTORS_DIRECTORY,
    render_vector_file,
)


@pytest.mark.parametrize("file_name", VECTOR_FILE_BUILDERS)
def test_the_committed_vector_file_matches_the_test_cases(file_name: str) -> None:
    vector_file_path = VECTORS_DIRECTORY / file_name
    expected_content = render_vector_file(VECTOR_FILE_BUILDERS[file_name]())

    assert vector_file_path.exists(), f"{vector_file_path} is missing; generate it with: {REGENERATION_COMMAND}"
    committed_content = vector_file_path.read_text(encoding="utf-8")
    assert committed_content == expected_content, (
        f"{file_name} is out of date; regenerate it with: {REGENERATION_COMMAND}"
    )
