import json
import uuid
from pathlib import Path

from social_crawler.domain.redaction import redact


class Samples:
    def __init__(self, directory: Path):
        self.directory = directory / "samples"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def write(self, request: dict, status: int, response) -> str:
        name = uuid.uuid4().hex + ".json"
        path = self.directory / name
        path.write_text(
            json.dumps(
                redact({"request": request, "status": status, "response": response}),
                ensure_ascii=False,
                indent=2,
            )
        )
        path.chmod(0o600)
        return "samples/" + name
