from pathlib import Path

import pytest

from chart_manager.plumbing.errors import YamlError
from chart_manager.plumbing.yaml_files import load_yaml_documents, load_yaml_file


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


def test_documents_accept_kubernetes_schema_value_tag_scalar(tmp_path: Path) -> None:
    path = tmp_path / "crd.yaml"
    path.write_text("openAPIV3Schema:\n  enum: [=, on, off]\n")

    documents = load_yaml_documents(path)

    assert documents[0]["openAPIV3Schema"]["enum"][0] == "="
