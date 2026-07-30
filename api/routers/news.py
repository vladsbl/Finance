"""GET /api/news/* -- NOT YET IMPLEMENTED.

Planned to wrap dashboard/app.py's "News & Analyse IA" page: the news
feed query plus each item's Groq analysis (importance/tonalite/impact from
news_analysis, joined to news_raw).
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/news", tags=["news"])
