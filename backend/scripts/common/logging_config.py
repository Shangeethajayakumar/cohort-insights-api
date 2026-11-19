"""Small JSON log formatter without request bodies or authentication secrets."""

import json
import logging


class JsonFormatter(logging.Formatter):
    def format(self, record):
        event = {"level": record.levelname, "logger": record.name, "message": record.getMessage()}
        for field in (
            "document_id",
            "content_version",
            "stage",
            "user_id",
            "method",
            "status_code",
        ):
            if hasattr(record, field):
                event[field] = getattr(record, field)
        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)
        return json.dumps(event)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
