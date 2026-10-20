"""Build the real wheel and verify it cannot overwrite another package's files."""

from email.parser import Parser
from pathlib import Path
import subprocess
from zipfile import ZipFile


def test_wheel_keeps_project_documents_in_distribution_metadata(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    with ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        metadata_files = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_files) == 1
        metadata_root = metadata_files[0].split("/")[0]
        assert {name.split("/")[0] for name in names} == {"abx_plugins", metadata_root}
        metadata = Parser().parsestr(wheel.read(metadata_files[0]).decode("utf-8"))
        readme = metadata.get_payload()
        assert isinstance(readme, str)
        assert readme.rstrip() == (repo / "README.md").read_text().rstrip()
        assert (
            wheel.read(f"{metadata_root}/licenses/LICENSE")
            == (repo / "LICENSE").read_bytes()
        )
        assert (
            wheel.read("abx_plugins/plugins/tlsnotary/server/README.md")
            == (repo / "abx_plugins/plugins/tlsnotary/server/README.md").read_bytes()
        )
