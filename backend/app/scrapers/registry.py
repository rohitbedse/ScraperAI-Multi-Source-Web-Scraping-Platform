"""Add a scraper = add one line here. Classes are imported lazily."""
import importlib
import logging
from typing import Optional

from app import config

logger = logging.getLogger("scraper.registry")

REGISTRY: dict[str, str] = {
    "swayam": "app.scrapers.swayam.scraper:SwayamScraper",
    "mindler": "app.scrapers.mindler.scraper:MindlerScraper",
    "careers360_nirf": "app.scrapers.careers360_nirf.platform_scraper:Careers360NirfPlatformScraper",
}
if config.ENABLE_DEMO:
    REGISTRY["demo"] = "app.scrapers.demo.scraper:DemoScraper"


def get_scraper_class(source: str):
    if source not in REGISTRY:
        raise KeyError(source)
    module, cls = REGISTRY[source].split(":")
    return getattr(importlib.import_module(module), cls)


def describe_sources() -> list[dict]:
    out = []
    for sid in REGISTRY:
        try:
            cls = get_scraper_class(sid)
            out.append({"id": sid, "name": cls.name, "description": cls.description,
                        "status": "available", "stages": [x.value for x in cls.stages],
                        "max_concurrent": cls.max_concurrent,
                        "params_schema": cls.ParamsModel.model_json_schema()})
        except Exception as exc:  # missing dependency, import error... -> still listed, flagged with the reason
            reason = f"{type(exc).__name__}: {exc}"[:400]
            logger.error("scraper %r failed to import: %s", sid, reason, exc_info=True)
            out.append({"id": sid, "name": sid.title(), "description": f"Unavailable: {reason}",
                        "status": "unavailable", "reason": reason, "params_schema": {}})
    return out


def validate_params(source: str, params: Optional[dict]) -> dict:
    return get_scraper_class(source).ParamsModel(**(params or {})).model_dump()
