"""
Regenerate machine-readable configuration schemas from the code's own data models.

    python config/generate_schemas.py

Writes config/sharp_provider.schema.json (JSON Schema of ProviderConfig). A test
fails if the committed file drifts from the model, so it cannot silently go stale.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from sharp_feed import ProviderConfig  # noqa: E402


def provider_schema() -> str:
    return json.dumps(ProviderConfig.model_json_schema(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    (HERE / "sharp_provider.schema.json").write_text(provider_schema())
    print("wrote", HERE / "sharp_provider.schema.json")
