"""Add a scraper = add one line here. Classes are imported lazily."""
import importlib
from typing import Optional

from app import config

REGISTRY: dict[str, str] = {
    "swayam": "app.scrapers.swayam.scraper:SwayamScraper",
    "mindler": "app.scrapers.mindler.scraper:MindlerScraper",
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
        except Exception as exc:  # missing dependency etc. -> still listed, flagged
            out.append({"id": sid, "name": sid.title(), "description": f"Unavailable: {exc}",
                        "status": "unavailable", "params_schema": {}})
    return out


def validate_params(source: str, params: Optional[dict]) -> dict:
    return get_scraper_class(source).ParamsModel(**(params or {})).model_dump()
