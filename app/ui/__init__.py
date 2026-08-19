from pathlib import Path

from fastapi import APIRouter
from fastapi.templating import Jinja2Templates

UI_DIR = Path(__file__).resolve().parent

router = APIRouter(prefix="/ui")
# Jinja2Templates autoescapes by default — load-bearing here, because
# contact fields, profile JSON and draft text are third-party/LLM output.
templates = Jinja2Templates(directory=str(UI_DIR / "templates"))

# Read-only views of an email body use `| anchor_to_text` so a drafter-embedded
# <a> link shows as "text (url)" instead of a raw tag; the editable textarea
# keeps the raw source so it round-trips on save.
from ..email_format import anchor_to_text  # noqa: E402
templates.env.filters["anchor_to_text"] = anchor_to_text

from . import routes  # noqa: E402,F401  (registers the routes on the router)
