"""
Database adapter registry — add new databases here.

sp_url 中的 {entity_id_raw} 由 get_sp_url() 用 school.py 里的 entityId 填充，
换学校只需改 carsi_search/school.py。
"""

from . import school

DB_REGISTRY = {

    "ieee": {
        "name": "ieee",
        "label": "IEEE Xplore",
        "sp_url": (
            "https://ieeexplore.ieee.org/servlet/wayf.jsp"
            "?entityId={entity_id_raw}"
            "&url=https%3A%2F%2Fieeexplore.ieee.org%2FXplore%2Fhome.jsp"
        ),
        "entity_id": school.IEEE_ENTITY_ID,
        "home_url": "https://ieeexplore.ieee.org/Xplore/home.jsp",
        "cookie_accept": [
            'button:has-text("全部接受")',
            'button:has-text("Accept All")',
        ],
        "target_url_pattern": "**/ieeexplore.ieee.org/Xplore/**",
        "adapter": "carsi_search.databases.ieee:IeeeAdapter",
    },

    "sciencedirect": {
        "name": "sciencedirect",
        "label": "ScienceDirect (Elsevier)",
        "sp_url": (
            "https://auth.elsevier.com/ShibAuth/institutionLogin"
            "?entityID={entity_id_raw}"
            "&appReturnURL=https%3A%2F%2Fwww.sciencedirect.com"
        ),
        "entity_id": school.SD_ENTITY_ID,
        "home_url": "https://www.sciencedirect.com/",
        "cookie_accept": [
            'button:has-text("Accept All")',
            'button:has-text("Accept all cookies")',
            '#onetrust-accept-btn-handler',
        ],
        "target_url_pattern": "**/sciencedirect.com/**",
        "adapter": "carsi_search.databases.sciencedirect:ScienceDirectAdapter",
    },

    "cnki": {
        "name": "cnki",
        "label": "CNKI 知网",
        "home_url": "https://kns.cnki.net/kns8s/search",
        "cookie_accept": [],
        "target_url_pattern": "**/cnki.net/**",
        "adapter": "carsi_search.databases.cnki:CnkiAdapter",
    },
}


def get_db(name: str) -> dict | None:
    return DB_REGISTRY.get(name)


def get_sp_url(name: str) -> str | None:
    """返回可直接打开的机构登录 URL（已填入本校 entityId）；无 sp_url 的库返回 None。"""
    db = get_db(name)
    if not db or not db.get("sp_url") or not db.get("entity_id"):
        return None
    return db["sp_url"].format(entity_id_raw=db["entity_id"])



def list_dbs() -> list[str]:
    return list(DB_REGISTRY.keys())


def _import_adapter(adapter_path: str):
    import importlib
    module_path, class_name = adapter_path.rsplit(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


async def get_adapter(name: str, page):
    db = get_db(name)
    if not db:
        raise ValueError(f"Unknown database: {name}. Available: {list_dbs()}")
    cls = _import_adapter(db["adapter"])
    return cls(page)
