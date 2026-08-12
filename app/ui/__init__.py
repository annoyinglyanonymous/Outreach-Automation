from pathlib import Path

from fastapi import APIRouter
from fastapi.templating import Jinja2Templates

UI_DIR = Path(__file__).resolve().parent

router = APIRouter(prefix="/ui")
# Jinja2Templates autoescapes by default — load-bearing here, because
# contact fields, profile JSON and draft text are third-party/LLM output.
templates = Jinja2Templates(directory=str(UI_DIR / "templates"))

from . import routes  # noqa: E402,F401  (registers the routes on the router)
