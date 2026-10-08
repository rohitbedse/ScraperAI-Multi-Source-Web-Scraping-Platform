"""
Mindler Career Library Scraper - API-based approach
----------------------------------------------------
Uses Mindler's internal API endpoints to fetch all career data:

1. careerDomainNameList - gets all career domains with taglines
2. careerDomainDetails - gets all sub-careers with full details for each domain

No Selenium/Playwright needed - pure API calls, much faster and reliable.
"""

import json
import os
import argparse
import datetime as dt
import time
import requests
from bs4 import BeautifulSoup

DOMAIN_LIST_URL = os.environ.get("MINDLER_DOMAIN_LIST_URL", "https://careerlibrary.mindler.com/api/careerlibrary/v1/careerDomainNameList")
DOMAIN_DETAILS_URL = os.environ.get("MINDLER_DOMAIN_DETAILS_URL", "https://careerlibrary.mindler.com/api/careerlibrary/v1/careerDomainDetails")
REQUEST_TIMEOUT = os.environ.get("MINDLER_TIMEOUT")   # overrides the per-request timeouts below
OUTPUT_FILE = os.path.join(os.environ.get("SCRAPER_STATE_DIR", "."), "mindler_career_library.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

HEADERS = {
    "User-Agent": UA,
    "Content-Type": "application/json",
    "Referer": "https://www.mindler.com/",
}


def clean_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    if not text:
        return None
    soup = BeautifulSoup(text, "html.parser")
    return soup.get_text(" ", strip=True)


def fetch_domain_list(session: requests.Session) -> list:
    """Get all career domains with their taglines."""
    resp = session.get(DOMAIN_LIST_URL, headers={"User-Agent": UA}, timeout=float(REQUEST_TIMEOUT or 15))
    resp.raise_for_status()
    data = resp.json()
    domains = []
    for item in data.get("data", []):
        src = item.get("_source", {})
        domains.append({
            "domain_id": src.get("_id"),
            "career_domain_name": src.get("career_domain_name"),
            "tagline": src.get("tagline"),
            "image": src.get("image"),
            "description": clean_html(src.get("description")),
        })
    return domains


def fetch_domain_details(session: requests.Session, tagline: str) -> dict:
    """Get all sub-careers (career_details) for a domain tagline."""
    payload = {"tagline": tagline}
    resp = session.post(DOMAIN_DETAILS_URL, headers=HEADERS, json=payload, timeout=float(REQUEST_TIMEOUT or 20))
    resp.raise_for_status()
    return resp.json()


def parse_career_details(career_details: list) -> list:
    """Parse the career_details array into structured sub-career objects."""
    subcareers = []
    for cd in career_details:
        subcareer = {
            "id": cd.get("id"),
            "career_id": cd.get("career_id"),
            "career_domain_id": cd.get("career_domain_id"),
            "career_broad_id": cd.get("career_broad_id"),
            "name": cd.get("career_name") or (cd.get("keywords", "").split(",")[0].strip() if cd.get("keywords") else None),
            "keywords": cd.get("keywords"),
            "notes": cd.get("notes"),
            "eligibility_status": cd.get("eligliblity_status"),
            "entrance_exams": [],
            "colleges_india": [],
            "colleges_abroad": [],
            "career_paths": [],
            "pros_cons": {"Pros": [], "Cons": []},
            "career_opportunities": [],
            "work_description": [],
        }

        # Entrance exams
        for ex in cd.get("career_entrance", []):
            subcareer["entrance_exams"].append({
                "name": ex.get("entrance_exam"),
                "key_elements": clean_html(ex.get("key_elements", "")),
                "tentative_date": ex.get("tentative_date"),
                "level": ex.get("ug_pg"),
                "website": ex.get("links"),
            })

        # Colleges India
        for col in cd.get("career_colleges", []):
            subcareer["colleges_india"].append({
                "name": col.get("college_name"),
                "location": col.get("location"),
                "course": clean_html(col.get("course", "")),
                "website": col.get("website"),
            })

        # Colleges Abroad
        for col in cd.get("career_abroad_colleges", []):
            subcareer["colleges_abroad"].append({
                "name": col.get("college_name"),
                "location": col.get("location"),
                "course": clean_html(col.get("course", "")),
                "website": col.get("website"),
            })

        # Career Paths - handle both dict with path_1/path_2/path_3 and list formats
        careers_path = cd.get("careers_path") or cd.get("career_path")
        if isinstance(careers_path, dict):
            for path_key, path_list in careers_path.items():
                if isinstance(path_list, list):
                    for p in path_list:
                        subcareer["career_paths"].append({
                            "path_group": path_key,
                            "path_name": p.get("path_name"),
                            "path_title": p.get("path_title"),
                            "stream": clean_html(p.get("description", "")) if p.get("path_name") == "stream" else clean_html(p.get("stream", "")),
                            "graduation": clean_html(p.get("description", "")) if p.get("path_name") == "graduation" else clean_html(p.get("graduation", "")),
                            "after_graduation": clean_html(p.get("description", "")) if p.get("path_name") == "After_Graduation" else clean_html(p.get("after_graduation", "")),
                            "after_post_graduation": clean_html(p.get("description", "")) if p.get("path_name") == "After_Post_Graduation" else clean_html(p.get("after_post_graduation", "")),
                            "description": clean_html(p.get("description", "")),
                        })
        elif isinstance(careers_path, list):
            for p in careers_path:
                subcareer["career_paths"].append({
                    "path_name": p.get("Path") or p.get("path_name"),
                    "stream": clean_html(p.get("Stream") or p.get("stream", "")),
                    "graduation": clean_html(p.get("Graduation") or p.get("graduation", "")),
                    "after_graduation": clean_html(p.get("After_Graduation") or p.get("after_graduation", "")),
                    "after_post_graduation": clean_html(p.get("After_Post_Graduation") or p.get("after_post_graduation", "")),
                    "description": clean_html(p.get("description", "")),
                })

        # Pros & Cons - pros_cons is a list of objects with pros/cons as HTML strings
        pros_cons = cd.get("pros_cons", [])
        if isinstance(pros_cons, list):
            for pc in pros_cons:
                pros_html = pc.get("pros", "")
                cons_html = pc.get("cons", "")
                subcareer["pros_cons"]["Pros"].extend([p for p in [clean_html(pros_html)] if p])
                subcareer["pros_cons"]["Cons"].extend([c for c in [clean_html(cons_html)] if c])
        elif isinstance(pros_cons, dict):
            subcareer["pros_cons"]["Pros"] = [clean_html(p) for p in pros_cons.get("pros", []) if clean_html(p)]
            subcareer["pros_cons"]["Cons"] = [clean_html(c) for c in pros_cons.get("cons", []) if clean_html(c)]

        # Career Opportunities - list of objects with Name/Description
        for opp in cd.get("career_opportunities", []):
            subcareer["career_opportunities"].append({
                "title": clean_html(opp.get("Name") or opp.get("title", "")),
                "description": clean_html(opp.get("Description") or opp.get("description", "")),
            })

        # Work Description - career_job_description is a list of objects with job_description
        for wd in cd.get("career_job_description", []):
            desc = clean_html(wd.get("job_description", ""))
            if desc:
                subcareer["work_description"].append(desc)

        subcareers.append(subcareer)

    return subcareers


def subject_id_for(tagline: str) -> str:
    return tagline.strip().lower().replace(" ", "-")


def deduplicate_subcareers(subcareers: list) -> list:
    unique = {}
    for subcareer in subcareers:
        key = subcareer.get("career_id") or subcareer.get("id") or subcareer.get("name")
        if key is not None:
            unique[str(key)] = subcareer
    return list(unique.values())


def load_existing_library() -> dict:
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {
        item["subject_id"]: item
        for item in existing
        if isinstance(item, dict) and item.get("subject_id")
    }


def scrape_library():
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    print("Fetching career domain list...")
    domains = fetch_domain_list(session)
    print(f"Found {len(domains)} career domains.")

    library_by_id = load_existing_library()

    for i, domain in enumerate(domains, 1):
        tagline = domain["tagline"]
        subject_id = subject_id_for(tagline)
        previous = library_by_id.get(subject_id, {})
        print(f"[{i}/{len(domains)}] Fetching details for: {domain['career_domain_name']} (tagline: {tagline})")

        try:
            details_resp = fetch_domain_details(session, tagline)
            data_list = details_resp.get("data", [])
            if not data_list:
                print(f"    WARNING: No data returned")
                career_details = []
            else:
                source = data_list[0].get("_source", {})
                career_details = source.get("career_details", [])

            subcareers = deduplicate_subcareers(parse_career_details(career_details))
            if not subcareers and previous.get("subcareers"):
                subcareers = previous["subcareers"]
            print(f"    -> {len(subcareers)} sub-careers")

            library_by_id[subject_id] = {
                "subject_id": subject_id,
                "subject_title": domain["career_domain_name"],
                "description": domain["description"],
                "image": domain["image"],
                "subcareers": subcareers,
            }

        except Exception as e:
            print(f"    ERROR: {e}")
            import traceback
            traceback.print_exc()
            if not previous:
                library_by_id[subject_id] = {
                    "subject_id": subject_id,
                    "subject_title": domain["career_domain_name"],
                    "description": domain["description"],
                    "image": domain["image"],
                    "subcareers": [],
                    "error": str(e),
                }

        time.sleep(0.3)  # be polite

    library = list(library_by_id.values())
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(library, f, ensure_ascii=False, indent=2)

    total_sub = sum(len(s["subcareers"]) for s in library)
    print(f"\nDone. Wrote {len(library)} domains, {total_sub} sub-careers to {OUTPUT_FILE}")


def seconds_until(target_time: dt.time) -> float:
    now = dt.datetime.now()
    target = dt.datetime.combine(now.date(), target_time)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


def run_scheduler(schedule_time: dt.time):
    print(f"Scheduler enabled. Next run at {schedule_time.strftime('%H:%M')}.")
    while True:
        delay = seconds_until(schedule_time)
        time.sleep(delay)
        try:
            scrape_library()
        except Exception as exc:
            print(f"Scheduled run failed: {exc}")
        print(f"Next run at {schedule_time.strftime('%H:%M')} tomorrow.")


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch the Mindler career library")
    parser.add_argument(
        "--schedule",
        metavar="HH:MM",
        help="Run once daily at the local time, for example --schedule 02:00",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.schedule:
        scrape_library()
        return

    try:
        schedule_time = dt.datetime.strptime(args.schedule, "%H:%M").time()
    except ValueError as exc:
        raise SystemExit("--schedule must use 24-hour HH:MM format, for example 02:00") from exc
    run_scheduler(schedule_time)


if __name__ == "__main__":
    main()