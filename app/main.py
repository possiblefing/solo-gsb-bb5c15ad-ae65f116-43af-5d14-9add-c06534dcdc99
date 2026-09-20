"""食品过敏原标签审查台 —— FastAPI 入口。

功能：
- 过敏原字典管理（原料 → 过敏原集合）；
- 原料别名管理（「别名：标准名」批量维护，有向图原子校验与链式解析，
  仅接入正式审查与字典时效复核）；
- 解析带嵌套子配料的配方文本，传播并汇总过敏原；
- 对照标签声明，指出遗漏 / 多报的过敏原；
- 支持两版配方对比，输出过敏原变化；
- 生成并导出标签审查报告（HTML / Markdown）；
- 临时预览工具：供应商方案风险区间、配料占比区间顺序校验、共线生产换线
  排程、共用标签模板分组、目标过敏原最小改配方案、过敏原检测套餐规划
  （只读，不写历史、不改写审查记录）。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from . import db
from .aliases import resolve_preview, validate_alias_batch
from .allergen_panel import PanelError, build_panel_preview, list_panel_records
from .analysis import flatten, normalize_name
from .changeover import (ChangeoverError, build_changeover_preview,
                         list_changeover_products)
from .parser import parse_recipe
from .proportion import analyze_proportions
from .recheck import build_recheck
from .reformulation import (ReformulationError, build_reformulation_plan,
                            get_reformulation_sources,
                            list_reformulation_records)
from .report import render_html_report, render_markdown_report
from .review import analyze
from .supplier_risk import SupplierPlanError, analyze_supplier_risk
from .template_group import (TemplateGroupError,
                             build_template_group_preview,
                             list_template_products)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 参考 GB 7718 致敏物质推荐目录
PRESET_ALLERGENS = [
    "麸质谷物", "甲壳类", "鱼类", "蛋类", "花生", "大豆", "乳",
    "坚果", "芝麻", "亚硫酸盐", "芹菜", "芥末", "羽扇豆", "软体动物",
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="食品过敏原标签审查台", version="1.0.0", lifespan=lifespan)


# ---------- 请求模型 ----------

class DictEntry(BaseModel):
    ingredient: str
    allergens: List[str] = Field(default_factory=list)


class BulkImport(BaseModel):
    text: str


class AliasBulkImport(BaseModel):
    text: str


class AliasPreview(BaseModel):
    text: str


class ReviewCreate(BaseModel):
    name: str = "未命名审查"
    text_a: str
    text_b: Optional[str] = None
    label_a: str = "现版配方"
    label_b: str = "新版配方"
    declared: List[str] = Field(default_factory=list)


class RecheckRequest(BaseModel):
    # false：仅预览前后差异；true：确认后覆盖结果并更新指纹
    confirm: bool = False


class SupplierRiskPreview(BaseModel):
    text: str
    plans_text: str = ""
    declared: List[str] = Field(default_factory=list)


class ProportionPreview(BaseModel):
    text: str


class ChangeoverPreview(BaseModel):
    review_ids: List[int]
    cleaning_minutes: Dict[str, float] = Field(default_factory=dict)


class TemplateGroupPreview(BaseModel):
    review_ids: List[int]
    max_extra: Dict[str, int] = Field(default_factory=dict)


class ReformulationSourcesRequest(BaseModel):
    review_id: int
    allergen: str


class ReformulationCostItem(BaseModel):
    path: List[str]
    cost: float


class ReformulationPreview(BaseModel):
    review_id: int
    allergen: str
    costs: List[ReformulationCostItem] = Field(default_factory=list)


class PanelPreview(BaseModel):
    review_id: int
    packages_text: str = ""


# ---------- 页面 ----------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/preset-allergens")
def preset_allergens():
    return {"allergens": PRESET_ALLERGENS}


# ---------- 过敏原字典 ----------

@app.get("/api/dict")
def get_dict():
    return {"entries": db.list_dict()}


@app.post("/api/dict")
def upsert_entry(entry: DictEntry):
    name = entry.ingredient.strip()
    if not name:
        raise HTTPException(400, "原料名不能为空")
    allergens = sorted({a.strip() for a in entry.allergens if a.strip()})
    db.upsert_entry(name, allergens)
    return {"ok": True}


@app.delete("/api/dict/{ingredient}")
def delete_entry(ingredient: str):
    db.delete_entry(ingredient)
    return {"ok": True}


@app.post("/api/dict/bulk")
def bulk_import(payload: BulkImport):
    imported = db.bulk_import(payload.text)
    return {"ok": True, "imported": imported}


# ---------- 原料别名 ----------

@app.get("/api/aliases")
def get_aliases():
    return {"aliases": db.list_aliases()}


@app.post("/api/aliases/bulk")
def bulk_aliases(payload: AliasBulkImport):
    """按「别名：标准名」批量维护别名。

    整批视为有向图做原子校验：格式错误、同一别名多目标、自环与任意
    长度环都会拒绝整批（不写入任何一条）；错误返回冲突行，环错误额外
    返回从最早冲突行展开的最短环路。合法时整批 upsert 并返回写入条数。
    """
    outcome = validate_alias_batch(payload.text, db.alias_existing())
    if outcome["errors"]:
        raise HTTPException(
            status_code=400,
            detail={"message": "别名批量校验未通过，整批未写入",
                    "errors": outcome["errors"]})
    imported = db.bulk_upsert_aliases(outcome["entries"])
    return {"ok": True, "imported": imported}


@app.delete("/api/aliases/{alias}")
def delete_alias(alias: str):
    db.delete_alias(alias)
    return {"ok": True}


@app.post("/api/aliases/resolve-preview")
def alias_resolve_preview(payload: AliasPreview):
    """别名解析预览（只读）：把配方文本中每种原料沿别名链解析到最终
    标准名，展示解析链、最终标准名及该标准名在字典中的过敏原。

    不写入任何数据，也不产生审查记录。"""
    if not payload.text.strip():
        raise HTTPException(400, "配方文本不能为空")
    tree = parse_recipe(payload.text)
    names = [n.name for n in flatten(tree)]
    leaf_keys = {normalize_name(n.name) for n in flatten(tree)
                 if not n.children}
    # 展示名映射：别名原文 / 标准名原文 + 字典中登记的原料名原文
    displays = {normalize_name(e["ingredient"]): e["ingredient"]
                for e in db.list_dict()}
    for e in db.list_aliases():
        displays[normalize_name(e["alias"])] = e["alias"]
        displays[normalize_name(e["target"])] = e["target"]
    items = resolve_preview(names, db.alias_edges(), db.dict_lookup(),
                            displays, leaf_keys)
    return {"items": items}


# ---------- 供应商方案风险区间（临时预览） ----------

@app.post("/api/supplier-risk-preview")
def supplier_risk_preview(payload: SupplierRiskPreview):
    """互斥过敏原方案的风险区间预览。

    仅基于当前字典做只读计算：在配料树上分别传播必然（方案交集）与
    可能（方案并集）过敏原，输出确定遗漏 / 潜在遗漏 / 确定多报 /
    潜在多报 / 一致五类结论。不枚举方案组合，也不写入字典或历史记录。
    """
    if not payload.text.strip():
        raise HTTPException(400, "配方文本不能为空")
    declared = sorted({a.strip() for a in payload.declared if a.strip()})
    try:
        return analyze_supplier_risk(
            text=payload.text, plans_text=payload.plans_text,
            declared=declared)
    except SupplierPlanError as exc:
        raise HTTPException(400, str(exc))


# ---------- 配料占比区间顺序校验（临时预览） ----------

@app.post("/api/proportion-check-preview")
def proportion_check_preview(payload: ProportionPreview):
    """配料占比区间顺序校验（临时预览）。

    复用嵌套配方解析，在每个原料名后读取 ``[最小-最大%]`` / ``[值%]``
    占比标注，对每组同级配料按标签顺序建立占比非递增约束，用区间传播
    （前向收紧上界、后向收紧下界）判断整组可行性：可行时返回每项收紧后
    的精确可行区间并标出单值锁定项，不可行时返回最短冲突片段及完整嵌套
    路径；标注问题（缺少区间 / 格式错误 / 负值 / 下界大于上界）逐条定位
    到具体路径。只读计算，不写入审查历史。
    """
    if not payload.text.strip():
        raise HTTPException(400, "配方文本不能为空")
    return analyze_proportions(payload.text)


# ---------- 共线生产换线排程（临时预览） ----------

@app.get("/api/changeover/products")
def changeover_products():
    """换线排程候选产品清单。

    每条历史审查记录给出当前配方（两版记录取标签核对对象，即新版配方）
    的最终过敏原集合与参与资格；处于需复核状态的记录标记为禁止参与并
    附原因。只读，不改写任何数据。
    """
    return {"products": list_changeover_products()}


@app.post("/api/changeover-schedule-preview")
def changeover_schedule_preview(payload: ChangeoverPreview):
    """共线生产换线排程（临时预览）。

    复用历史审查记录当前配方的最终过敏原集合，按「前一产品含有、后一
    产品不含」的过敏原计算有向换线成本（各过敏原清洁分钟数之和，未配置
    的按 0 分钟计），用子集动态规划求总清洁时间最短的完整生产顺序；
    总耗时并列时按产品 ID 序列取最小者，并返回并列最优顺序总数。记录
    缺失或处于需复核状态时禁止参与并逐条指出原因。只读计算，不改写
    任何审查记录。
    """
    try:
        return build_changeover_preview(
            payload.review_ids, payload.cleaning_minutes)
    except ChangeoverError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": str(exc), "problems": exc.problems})


# ---------- 共用标签模板分组（临时预览） ----------

@app.get("/api/template-group/products")
def template_group_products():
    """模板分组候选产品清单。

    每条历史审查记录给出当前配方（两版记录取标签核对对象，即新版配方）
    的最终过敏原集合与参与资格；处于需复核状态的记录标记为禁止参与并
    附原因。只读，不改写任何数据。
    """
    return {"products": list_template_products()}


@app.post("/api/template-group-preview")
def template_group_preview(payload: TemplateGroupPreview):
    """共用标签模板分组（临时预览）。

    复用历史审查记录当前配方的最终过敏原集合：一组产品共用一张标签
    模板（内容 = 组内过敏原并集），每个产品的附加声明为「模板含有而
    自身没有」的过敏原，仅当组内每个产品的附加声明数都不超过其上限
    （未配置的按 0 个计）时该组才成立；用子集动态规划求模板数量最少
    的完整分组，模板数并列时按组内及组间产品 ID 序列取最小者，并返回
    达到最少模板数的分组方案总数。记录缺失或处于需复核状态时禁止参与
    并逐条指出原因。只读计算，不改写任何审查记录。
    """
    try:
        return build_template_group_preview(
            payload.review_ids, payload.max_extra)
    except TemplateGroupError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": str(exc), "problems": exc.problems})


# ---------- 目标过敏原最小改配方案（临时预览） ----------

@app.get("/api/reformulation/records")
def reformulation_records():
    """改配方案候选记录清单。

    每条历史审查记录给出当前配方（两版记录取标签核对对象，即新版配方）
    的最终过敏原集合与参与资格；处于需复核状态的记录标记为禁止选择并
    附原因。只读，不改写任何数据。
    """
    return {"records": list_reformulation_records()}


@app.post("/api/reformulation/sources")
def reformulation_sources(payload: ReformulationSourcesRequest):
    """目标过敏原的全部来源路径与候选替换点（只读）。

    复用所选时效正常记录的当前配方树（两版取新版），列出目标过敏原的
    全部来源路径（携带该过敏原的自身标注节点）以及由路径前缀构成的
    候选替换树；用户据此按完整节点路径填写可替换成本。记录缺失或处于
    需复核状态、过敏原不在当前配方最终集合中时返回 400。
    """
    try:
        return get_reformulation_sources(payload.review_id, payload.allergen)
    except ReformulationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": str(exc), "problems": exc.problems})


@app.post("/api/reformulation-plan-preview")
def reformulation_plan_preview(payload: ReformulationPreview):
    """目标过敏原最小改配方案（临时预览）。

    用户按完整节点路径填写可替换成本（未填写的节点视为不可替换），
    后端在候选树上用树形动态规划求覆盖全部来源路径的最低总成本节点
    集合：选择祖先即同时阻断其子树，方案不再选其后代；同成本按节点
    路径序列字典序取最小，并返回并列方案数。无解时列出仍不可覆盖的
    来源。记录缺失或需复核时返回 400，``detail.problems`` 给出原因。
    只读计算，不改写任何审查记录。
    """
    try:
        costs = [item.model_dump() if hasattr(item, "model_dump")
                 else item.dict() for item in payload.costs]
        return build_reformulation_plan(payload.review_id, payload.allergen, costs)
    except ReformulationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": str(exc), "problems": exc.problems})


# ---------- 过敏原检测套餐规划（临时预览） ----------

@app.get("/api/allergen-panel/records")
def allergen_panel_records():
    """套餐规划候选记录清单。

    仅允许选择**一条时效正常**记录：每条历史记录给出当前配方（两版记录
    取标签核对对象，即新版配方）的最终过敏原集合（套餐必须覆盖项）与
    选择资格；处于需复核状态的记录标记为禁止选择并附原因。只读。
    """
    return {"records": list_panel_records()}


@app.post("/api/allergen-panel-preview")
def allergen_panel_preview(payload: PanelPreview):
    """过敏原检测套餐规划（临时预览）。

    仅接入一条时效正常历史审查记录，读取其当前配方的最终过敏原作为
    **必须覆盖项**；用户逐行录入 ``套餐名：价格：过敏原列表``，后端以
    精确小数比价、用子集动态规划求**总价最低**的套餐组合，同价按套餐名
    序列字典序展示唯一方案并统计并列组合数，无法覆盖时逐条列出缺口；
    每个套餐给出覆盖贡献与累计覆盖。记录缺失（已删除）或转为需复核时
    接口拒绝并在 ``detail.problems`` 说明；套餐行格式 / 价格 / 重名问题
    同样整批拒绝、逐行指出。只读计算，套餐与记录都不写库。
    """
    try:
        return build_panel_preview(payload.review_id, payload.packages_text)
    except PanelError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": str(exc), "problems": exc.problems})


# ---------- 审查 ----------

@app.post("/api/reviews", status_code=201)
def create_review(payload: ReviewCreate):
    if not payload.text_a.strip():
        raise HTTPException(400, "配方文本不能为空")
    declared = sorted(set(payload.declared))
    result, fingerprint = analyze(
        text_a=payload.text_a, text_b=payload.text_b,
        label_a=payload.label_a, label_b=payload.label_b,
        declared=declared)
    rid = db.create_review(
        name=payload.name.strip() or "未命名审查",
        declared=declared,
        text_a=payload.text_a, text_b=payload.text_b,
        label_a=payload.label_a, label_b=payload.label_b,
        result=result, dict_fingerprint=fingerprint,
    )
    return {"id": rid, **result}


@app.get("/api/reviews")
def list_reviews():
    return {"reviews": db.list_reviews()}


@app.get("/api/reviews/{rid}")
def review_detail(rid: int):
    review = db.get_review(rid)
    if not review:
        raise HTTPException(404, "审查记录不存在")
    return review


@app.delete("/api/reviews/{rid}")
def remove_review(rid: int):
    if not db.get_review(rid):
        raise HTTPException(404, "审查记录不存在")
    db.delete_review(rid)
    return {"ok": True}


@app.post("/api/reviews/{rid}/recheck")
def recheck_review(rid: int, payload: RecheckRequest):
    """字典时效复核。

    始终先用当前字典重新解析记录的原始 A/B 配方与标签声明并计算前后差异；
    confirm=false 时仅返回差异供页面预览，不改动记录；
    confirm=true 时确认覆盖结果、刷新字典指纹（含旧记录补录指纹）。
    """
    review = db.get_review(rid)
    if not review:
        raise HTTPException(404, "审查记录不存在")
    outcome = build_recheck(review)
    if payload.confirm:
        db.update_review_result(
            rid, outcome["new_result"], outcome["fingerprint"])
        updated = db.get_review(rid)
        outcome["review"] = updated
    outcome["id"] = rid
    return outcome


@app.get("/api/reviews/{rid}/report")
def review_report(rid: int, format: str = Query("html", pattern="^(html|md)$")):
    review = db.get_review(rid)
    if not review:
        raise HTTPException(404, "审查记录不存在")
    if format == "md":
        return Response(
            render_markdown_report(review),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="allergen_review_{rid}.md"'},
        )
    return HTMLResponse(
        render_html_report(review),
        headers={"Content-Disposition":
                 f'attachment; filename="allergen_review_{rid}.html"'},
    )
