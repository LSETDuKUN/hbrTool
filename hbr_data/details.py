"""Typed display/calculation-facing interfaces. Missing hit ratios stay missing."""
from dataclasses import dataclass
import json
from .assets import AssetStore
from .store import Catalog, walk


@dataclass(frozen=True)
class HitDetail:
    index: int
    kind: str | None
    ratio: float | None


@dataclass(frozen=True)
class EffectDetail:
    index: int
    kind: str | None
    power: tuple
    stat_difference: float | None
    weights: tuple
    elements: tuple
    physical_type: str | None
    dp_multiplier: float | None
    hp_multiplier: float | None
    destruction_multiplier: float | None
    hits: tuple[HitDetail, ...]
    condition: str
    growth: tuple
    raw: dict


@dataclass(frozen=True)
class SkillDetail:
    id: object
    name: str
    description: str
    hit_count: int | None
    sp_cost: float | None
    hits: tuple[HitDetail, ...]
    effects: tuple[EffectDetail, ...]
    pointer: str
    raw: dict


def hit_details(hits):
    return tuple(HitDetail(hit.get("id",i+1),hit.get("type"),hit.get("power_ratio"))
                 for i,hit in enumerate(hits or []) if isinstance(hit,dict))


def skill_detail(skill, pointer="", names=None, descriptions=None):
    effects = []
    for i,part in enumerate(skill.get("parts") or []):
        mult = part.get("multipliers") or {}
        effects.append(EffectDetail(i,part.get("skill_type"),tuple(part.get("power") or []),
            part.get("diff_for_max"),tuple((part.get("parameters") or {}).items()),
            tuple(part.get("elements") or []),part.get("type"),mult.get("dp"),mult.get("hp"),mult.get("dr"),
            hit_details(part.get("hits")),part.get("cond") or "",tuple(part.get("growth") or []),part))
    label = skill.get("label")
    return SkillDetail(skill.get("id"),(names or {}).get(label) or skill.get("name") or label or "技能",
        (descriptions or {}).get(label) or skill.get("desc") or "",skill.get("hit_count"),skill.get("sp_cost"),
        hit_details(skill.get("hits")),tuple(effects),pointer,skill)


class DetailService:
    def __init__(self, catalog=None):
        self.catalog = catalog or Catalog()
        self.assets = AssetStore(self.catalog.root)
        self._translations = None
        self._snapshot = None

    def translations(self):
        snapshot = self.catalog.manifest()["database"]
        if self._translations is None or snapshot != self._snapshot:
            _,rows,_ = self.catalog.query("SELECT field,label,text FROM translations WHERE language='zhTW' AND category='skill' AND field IN ('name','info')",limit=100000)
            self._translations = {"name":{},"info":{}}
            for field,label,text in rows:
                self._translations[field][label] = text
            self._snapshot = snapshot
        return self._translations

    def record(self, record_key):
        _,rows,_ = self.catalog.query("SELECT region,dataset,id,name_zh,source_url,raw_json FROM records WHERE record_key=?",(record_key,))
        if not rows:
            raise KeyError(record_key)
        region,dataset,entity_id,name,source,raw = rows[0]
        data = json.loads(raw)
        translations = self.translations() if region == "jp" else {}
        skills = [skill_detail(value,pointer,translations.get("name"),translations.get("info"))
                  for pointer,value in walk(data) if "hit_count" in value and isinstance(value.get("parts"),list)]
        return {"region":region,"dataset":dataset,"id":entity_id,"name":name or (data.get("name") if isinstance(data,dict) else "资料"),
                "source":source,"raw":data,"skills":skills,"portrait":self.assets.get(region,dataset,entity_id)}

    def skills(self, region, skill_id):
        translations = self.translations() if region == "jp" else {}
        return [skill_detail(v["data"],v["json_pointer"],translations.get("name"),translations.get("info"))
                for v in self.catalog.skill_variants(region,skill_id)]
