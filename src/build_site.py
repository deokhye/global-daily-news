"""
build_site.py
--------------
data/latest.json (collector.py 결과) 을 template/template.html (Jinja2) 에 주입하여
docs/index.html 을 생성한다. docs/ 폴더는 GitHub Pages 배포 소스로 사용한다.

실행:
    python src/collector.py   # 데이터 수집 -> data/latest.json
    python src/build_site.py  # 템플릿 렌더 -> docs/index.html
"""

import os
import json
import logging

from jinja2 import Environment, FileSystemLoader, select_autoescape

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("build_site")

BASE_DIR = os.path.join(os.path.dirname(__file__), "..")
DATA_PATH = os.path.join(BASE_DIR, "data", "latest.json")
TEMPLATE_DIR = os.path.join(BASE_DIR, "template")
TEMPLATE_NAME = "template.html"
OUTPUT_PATH = os.path.join(BASE_DIR, "docs", "index.html")

CATEGORY_LABEL = {
    "POLITICS": "POLITICS",
    "ECONOMY": "ECONOMY",
    "ENVIRON": "ENVIRON",
    "SOCIETY": "SOCIETY",
}

# Tailwind 텍스트 색상 클래스 매핑 (뉴스 카테고리 / HR 태그용)
COLOR_CLASS = {
    "blue": "text-blue-700",
    "green": "text-green-600",
    "orange": "text-orange-500",
    "purple": "text-purple-600",
}
TAG_BG_CLASS = {
    "red": "bg-red-100 text-red-700",
    "indigo": "bg-indigo-100 text-indigo-700",
    "teal": "bg-teal-100 text-teal-700",
    "yellow": "bg-yellow-100 text-yellow-700",
}


def load_data() -> dict:
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build(data: dict) -> None:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template(TEMPLATE_NAME)

    er = data["exchange_rate"]
    is_up = er.get("change_pct", 0) >= 0

    ctx = {
        "generated_at_display": data["generated_at_display"],
        "current_rate": f"{er['current_rate']:.2f}",
        "change_pct": abs(er.get("change_pct", 0)),
        "is_up": is_up,
        "chart_labels": json.dumps(er.get("history_labels", []), ensure_ascii=False),
        "chart_values": json.dumps(er.get("history_values", [])),
        "profile": data["country_profile"],
        "headlines": [
            {**h, "color_class": COLOR_CLASS.get(h.get("color"), "text-slate-600")}
            for h in data["headlines"]
        ],
        "hr_trends": [
            {**t, "tag_class": TAG_BG_CLASS.get(t.get("color"), "bg-slate-100 text-slate-700")}
            for t in data["hr_trends"]
        ],
    }

    html = template.render(**ctx)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    log.info(f"생성 완료 -> {OUTPUT_PATH}")


def main():
    data = load_data()
    build(data)


if __name__ == "__main__":
    main()
