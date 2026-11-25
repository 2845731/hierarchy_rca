from dataclasses import dataclass
from pathlib import Path
import json
from storage.results import sha256

PARQUET_FILES = tuple(f"{period}_{kind}.parquet" for period in ("normal", "abnormal")
                      for kind in ("traces", "metrics", "logs", "metrics_histogram", "metrics_sum"))


@dataclass(frozen=True)
class Case:
    name: str
    system: str
    directory: Path

    def inventory(self, hashes=True):
        records = []
        for name in PARQUET_FILES:
            path = self.directory / name
            if not path.is_file() or path.resolve().parent != self.directory.resolve():
                raise ValueError(f"Missing or non-local telemetry file: {name}")
            record = {"name": name, "bytes": path.stat().st_size}
            if hashes:
                record["sha256"] = sha256(path)
            records.append(record)
        return records


def load_cases(dataset_root: Path) -> list[Case]:
    cases = []
    seen = set()
    for line in (dataset_root / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        name = row["name"]
        if not isinstance(name, str) or Path(name).name != name or any(c in name for c in "/\\:"):
            raise ValueError("Invalid case identifier")
        if name in seen:
            raise ValueError(f"Duplicate case: {name}")
        seen.add(name)
        directory = (dataset_root / "cases" / name).resolve()
        if directory.parent != (dataset_root / "cases").resolve():
            raise ValueError("Case directory escapes dataset")
        # Never retain the label-bearing fields of manifest rows in agent state.
        cases.append(Case(name, row["system"], directory))
    return cases
