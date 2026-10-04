# Vercel's entry point: every request is routed here (see vercel.json) and
# handed to the FastAPI app.
from app.main import app  # noqa: F401
