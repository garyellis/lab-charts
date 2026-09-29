from pathlib import Path

import pytest

from chart_manager.plumbing.errors import YamlError
from chart_manager.plumbing.yaml_files import load_yaml_file


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"value: [\n", "failed to parse"),
        (b"value: \xff\n", "failed to decode"),
        (b"- not\n- a\n- mapping\n", "must contain a mapping"),
    ],
)
def test_mapping_file_boundary_reports_invalid_inputs(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    path = tmp_path / "input.yaml"
    path.write_bytes(payload)

    with pytest.raises(YamlError, match=message):
        load_yaml_file(path)
