"""根据配方版本、原料批次与参与者忌口计算可提供性结论。

结论为三档：allow（可提供）、review（需人工确认）、deny（不得提供）。
解释中保留每个命中标签的来源，便于闭市前人工复核。
"""

from __future__ import annotations

from typing import Any


def _tag_sources(tag: str, recipe_version: dict[str, Any], batches: list[dict[str, Any]],
                 level: str) -> list[dict[str, Any]]:
    """列出一个风险标签来自哪些配方版本或原料批次。"""

    key = f"{level}_tags"
    sources: list[dict[str, Any]] = []
    if tag in recipe_version.get(key, []):
        sources.append({
            "source_type": "recipe_version",
            "recipe_version_id": recipe_version["recipe_version_id"],
            "recipe_name": recipe_version["recipe_name"],
            "version": recipe_version["version"],
        })
    for batch in batches:
        if tag in batch.get(key, []):
            sources.append({
                "source_type": "ingredient_batch",
                "batch_id": batch["batch_id"],
                "ingredient_name": batch["ingredient_name"],
                "lot_number": batch["lot_number"],
            })
    return sources


def build_assessment(*, holder_type: str, holder_id: str, holder_status: str,
                     preparation_status: str, recipe_version: dict[str, Any],
                     batches: list[dict[str, Any]], participant_id: str,
                     restrictions: list[str], declaration_found: bool) -> dict[str, Any]:
    """汇总阻断原因与忌口命中，返回稳定的判定解释。"""

    blocking: list[dict[str, str]] = []
    if holder_status == "frozen":
        blocking.append({"reason": "holder_frozen", "message": "容器或制备批次已冻结，暂停提供"})
    elif holder_status == "in_transfer":
        blocking.append({"reason": "holder_in_transfer", "message": "容器正在跨摊位转移，确认前不能发放"})
    elif holder_status in ("emptied", "depleted"):
        blocking.append({"reason": "holder_empty", "message": "容器或制备批次已清空"})
    if preparation_status == "frozen" and holder_status != "frozen":
        blocking.append({"reason": "preparation_frozen", "message": "所属制备批次已冻结，暂停提供"})
    frozen_batches = [batch for batch in batches if batch.get("status") == "frozen"]
    for batch in frozen_batches:
        blocking.append({
            "reason": "ingredient_batch_frozen",
            "message": f"原料 {batch['ingredient_name']}（批号 {batch['lot_number']}）已冻结",
        })

    matched_strict = [
        {"tag": tag, "sources": _tag_sources(tag, recipe_version, batches, "strict")}
        for tag in restrictions
        if tag in recipe_version.get("strict_tags", [])
        or any(tag in batch.get("strict_tags", []) for batch in batches)
    ]
    matched_caution = [
        {"tag": tag, "sources": _tag_sources(tag, recipe_version, batches, "caution")}
        for tag in restrictions
        if tag in recipe_version.get("caution_tags", [])
        or any(tag in batch.get("caution_tags", []) for batch in batches)
    ]

    if blocking or matched_strict:
        decision = "deny"
    elif matched_caution:
        decision = "review"
    else:
        decision = "allow"

    if decision == "deny":
        if blocking:
            message = "不得提供：" + "；".join(item["message"] for item in blocking)
        else:
            tags = "、".join(item["tag"] for item in matched_strict)
            message = f"不得提供：参与者忌口命中禁忌标签 {tags}"
    elif decision == "review":
        tags = "、".join(item["tag"] for item in matched_caution)
        message = f"需人工确认：参与者忌口命中慎用标签 {tags}"
    elif declaration_found:
        message = "可提供：参与者忌口与本品风险标签无冲突"
    else:
        message = "可提供：参与者未登记忌口信息"

    return {
        "decision": decision,
        "holder_type": holder_type,
        "holder_id": holder_id,
        "participant_id": participant_id,
        "recipe_version_id": recipe_version["recipe_version_id"],
        "participant_restrictions": list(restrictions),
        "declaration_found": declaration_found,
        "matched_strict": matched_strict,
        "matched_caution": matched_caution,
        "blocking": blocking,
        "message": message,
    }
