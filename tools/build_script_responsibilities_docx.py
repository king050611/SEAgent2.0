#!/usr/bin/env python3
"""Build a polished DOCX report with real Word tables using only stdlib."""

from __future__ import annotations

import html
import zipfile
from pathlib import Path


OUT = Path("docs/script_responsibilities.docx")


CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""

CORE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>TaskMonitor 脚本职责与运行逻辑</dc:title>
  <dc:creator>Codex</dc:creator>
  <cp:lastModifiedBy>Codex</cp:lastModifiedBy>
</cp:coreProperties>
"""

APP = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Codex</Application>
</Properties>
"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Calibri" w:eastAsia="Microsoft YaHei" w:hAnsi="Calibri"/><w:color w:val="1F2937"/><w:sz w:val="21"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:jc w:val="center"/><w:spacing w:after="280"/></w:pPr>
    <w:rPr><w:b/><w:rFonts w:ascii="Calibri" w:eastAsia="Microsoft YaHei" w:hAnsi="Calibri"/><w:color w:val="111827"/><w:sz w:val="36"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:jc w:val="center"/><w:spacing w:after="360"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Calibri" w:eastAsia="Microsoft YaHei" w:hAnsi="Calibri"/><w:color w:val="6B7280"/><w:sz w:val="20"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:outlineLvl w:val="0"/><w:spacing w:before="340" w:after="140"/></w:pPr>
    <w:rPr><w:b/><w:rFonts w:ascii="Calibri" w:eastAsia="Microsoft YaHei" w:hAnsi="Calibri"/><w:color w:val="0F172A"/><w:sz w:val="28"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:outlineLvl w:val="1"/><w:spacing w:before="220" w:after="100"/></w:pPr>
    <w:rPr><w:b/><w:rFonts w:ascii="Calibri" w:eastAsia="Microsoft YaHei" w:hAnsi="Calibri"/><w:color w:val="1E3A8A"/><w:sz w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Small">
    <w:name w:val="Small"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr><w:rFonts w:ascii="Calibri" w:eastAsia="Microsoft YaHei" w:hAnsi="Calibri"/><w:color w:val="4B5563"/><w:sz w:val="19"/></w:rPr>
  </w:style>
</w:styles>
"""


def e(text: object) -> str:
    return html.escape(str(text), quote=False)


def r(text: object, bold: bool = False, color: str | None = None, size: int | None = None) -> str:
    props = []
    if bold:
        props.append("<w:b/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    if size:
        props.append(f'<w:sz w:val="{size}"/>')
    props.append('<w:rFonts w:ascii="Calibri" w:eastAsia="Microsoft YaHei" w:hAnsi="Calibri"/>')
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>"
    return f'<w:r>{rpr}<w:t xml:space="preserve">{e(text)}</w:t></w:r>'


def p(text: object = "", style: str | None = None, bold: bool = False) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}{r(text, bold=bold)}</w:p>"


def bullet(text: object) -> str:
    return (
        '<w:p><w:pPr><w:ind w:left="360" w:hanging="240"/></w:pPr>'
        f'{r("• ")}{r(text)}</w:p>'
    )


def cell(text: object, width: int, header: bool = False) -> str:
    fill = "EAF2FF" if header else "FFFFFF"
    color = "0F172A" if header else "1F2937"
    return (
        "<w:tc>"
        "<w:tcPr>"
        f'<w:tcW w:w="{width}" w:type="dxa"/>'
        f'<w:shd w:fill="{fill}"/>'
        '<w:tcMar><w:top w:w="100" w:type="dxa"/><w:left w:w="100" w:type="dxa"/>'
        '<w:bottom w:w="100" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tcMar>'
        "</w:tcPr>"
        f"<w:p>{r(text, bold=header, color=color, size=20 if header else 18)}</w:p>"
        "</w:tc>"
    )


def table(headers: list[str], rows: list[list[str]], widths: list[int]) -> str:
    borders = (
        "<w:tblBorders>"
        '<w:top w:val="single" w:sz="6" w:space="0" w:color="CBD5E1"/>'
        '<w:left w:val="single" w:sz="6" w:space="0" w:color="CBD5E1"/>'
        '<w:bottom w:val="single" w:sz="6" w:space="0" w:color="CBD5E1"/>'
        '<w:right w:val="single" w:sz="6" w:space="0" w:color="CBD5E1"/>'
        '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="E5E7EB"/>'
        '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="E5E7EB"/>'
        "</w:tblBorders>"
    )
    grid = "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}"/>' for w in widths) + "</w:tblGrid>"
    out = [
        "<w:tbl>",
        f'<w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblLook w:firstRow="1" w:noHBand="0" w:noVBand="1"/>{borders}</w:tblPr>',
        grid,
        "<w:tr>" + "".join(cell(h, widths[i], header=True) for i, h in enumerate(headers)) + "</w:tr>",
    ]
    for row in rows:
        out.append("<w:tr>" + "".join(cell(row[i], widths[i]) for i in range(len(headers))) + "</w:tr>")
    out.append("</w:tbl>")
    out.append(p(""))
    return "\n".join(out)


def document_xml() -> str:
    body: list[str] = []
    body.append(p("TaskMonitor 脚本职责与运行逻辑", "Title"))
    body.append(p("面向后续维护与二次开发的模块说明文档", "Subtitle"))

    body.append(p("一、系统概览", "Heading1"))
    body.append(p("当前系统是一个离线 TaskMonitor 服务，用于监管采油树控制面板插头插入任务。系统将任务拆解为 S1 到 S8 的串行子任务，接收外部状态上报，根据配置化判据判断完成、失败或等待人工审核，并支持自然语言查询与人工干预。"))
    body.append(table(
        ["阶段", "核心动作", "主要参与脚本"],
        [
            ["启动装配", "加载配置、模型、状态存储和业务组件，启动 Flask 服务。", "run.py"],
            ["任务创建", "扫描 task/ 目录或接收 intent JSON，生成任务和 S1-S8 子任务。", "task_scanner.py, task_manager.py, task_decomposer.py"],
            ["状态评估", "校验当前子任务、映射字段、评估硬/软判据并更新状态。", "server.py, state_monitor.py, criteria_evaluator.py"],
            ["流程推进", "完成后推进下一步；失败时触发异常处理；需要审核时等待人工确认。", "task_manager.py, anomaly_handler.py"],
            ["查询干预", "识别用户查询或干预意图，执行二次确认后的重试、回退、覆盖字段等动作。", "query_responder.py, intervention_handler.py"],
            ["异常建议", "根据 anomaly_state 和当前失败判据生成诊断建议，但不直接改变流程。", "anomaly_advisor/*"],
        ],
        [1700, 5600, 3300],
    ))

    body.append(p("二、核心脚本职责", "Heading1"))
    body.append(table(
        ["脚本", "职责定位", "关键运作逻辑", "依赖/输出"],
        [
            ["run.py", "系统启动入口与组件装配中心。", "启动前清理端口和 vLLM 进程；加载配置、tokenizer、vLLM；创建各核心对象；启动任务扫描线程和 Flask 服务。", "依赖 config/*.yaml、本地模型；输出 Flask App。"],
            ["src/server.py", "HTTP 接口层。", "负责请求校验和路由分发；将任务创建、状态上报、查询、审核、重置等请求转交业务层。", "依赖 TaskManager、QueryResponder、StateStore、TaskScanner。"],
            ["src/task_manager.py", "任务生命周期与状态机核心。", "创建任务、启动子任务、处理状态上报结果、推进下一步、执行审核、记录待确认干预、生成异常建议。", "依赖 StateStore、TaskDecomposer、StateMonitor、AnomalyHandler、InterventionHandler、AnomalyAdvisor。"],
            ["src/task_decomposer.py", "子任务实例生成器。", "复制 task_templates.yaml 中 S1-S8 模板，将 id 转为 subtask_id，并初始化 pending、retry_count、completion_criteria 等运行时字段。", "输出运行时 subtasks 列表。"],
            ["src/task_scanner.py", "任务准入文件扫描器。", "扫描 task/*.json；要求 intent_id 和 task_type；跳过已处理文件；调用 TaskManager 创建任务。", "维护 data/processed_records.json。"],
            ["src/state_store.py", "任务状态持久化层。", "按 task_id 分文件保存 JSON；使用线程锁和临时文件替换实现原子更新。", "读写 data/tasks/<task_id>.json。"],
            ["src/state_monitor.py", "状态写入与判据触发器。", "只允许 current_subtask 接收上报；映射原始字段；合并 user_overrides；调用判据评估；更新 completed、failed、waiting_approval 或 in_progress。", "依赖 StateStore、CriteriaEvaluator、state_mapping.yaml。"],
            ["src/criteria_evaluator.py", "硬/软判据评估器。", "按 criteria.yaml 逐项比较实际值与阈值；返回 hard_met、soft_met、all_met、未满足字段等结构。", "输出 completion_criteria。"],
            ["src/anomaly_handler.py", "异常分支处理器。", "根据子任务 anomalies 和 anomaly_actions 找到处理动作；当前自动重试/回退关闭，返回人工确认提示；可设置 failed 或 waiting_approval。", "依赖 task_templates.yaml、StateStore。"],
            ["src/intervention_handler.py", "人工干预执行器。", "执行 rollback、retry、change_parameter、override_field；失败状态只允许对当前失败子任务进行受控恢复。", "修改任务状态并清理旧通知/异常快照。"],
            ["src/query_responder.py", "自然语言交互中枢。", "识别查询/干预/无关；判断确认/取消；整理任务状态、判据解释、异常建议后生成回复。", "依赖 prompts.py、llm_client.py、criteria.yaml。"],
            ["src/llm_client.py", "本地 vLLM 封装。", "使用 tokenizer chat template 构造 prompt；调用 vLLM generate；提供 JSON 抽取。", "供 QueryResponder 和 AnomalyAdvisor 使用。"],
            ["src/prompts.py", "提示词集中管理。", "分离意图分类、干预确认、最终回复三类 prompt，并约束安全关键事实必须来自 task_state 与 operation_result。", "被 QueryResponder 引用。"],
            ["src/utils.py", "通用工具函数。", "加载 YAML、创建 logger、获取时间戳。", "被 run.py 等模块使用。"],
        ],
        [2200, 2600, 4300, 2800],
    ))

    body.append(p("三、异常建议模块", "Heading1"))
    body.append(table(
        ["脚本", "职责", "关键逻辑"],
        [
            ["src/anomaly_advisor/advisor.py", "异常建议编排入口。", "记录异常上下文；调用规则匹配器和建议生成器；不执行重试、回退、审核或终止任务。"],
            ["src/anomaly_advisor/context.py", "异常上下文构建。", "规范化 anomaly_state 和旧版 diagnostic_signals；组装当前子任务、失败判据、最新状态、系统动作。"],
            ["src/anomaly_advisor/rules.py", "规则匹配。", "将后端 abnormal anomaly_state 与当前子任务 possible_anomalies 取交集；过滤 normal、unknown、不支持或越界异常。"],
            ["src/anomaly_advisor/llm_generator.py", "建议生成。", "优先用 LLM 生成结构化建议，失败时用模板回退；确保候选故障模块不超出规则范围。"],
        ],
        [3100, 3100, 5200],
    ))

    body.append(p("四、配置文件职责", "Heading1"))
    body.append(table(
        ["配置文件", "用途", "对运行时的影响"],
        [
            ["config/monitor.yaml", "服务、模型、持久化和日志配置。", "决定 Flask host/port、本地模型路径、任务状态目录 data/tasks。"],
            ["config/task_templates.yaml", "S1-S8 子任务模板和异常动作映射。", "决定任务拆解顺序、子任务名称、判据引用、异常 key 到动作的映射。"],
            ["config/criteria.yaml", "每个子任务的硬判据、软判据、审核要求和自然语言解释。", "硬判据失败会导致 failed；软判据不满足会保持 in_progress；require_approval=true 会进入 waiting_approval。"],
            ["config/state_mapping.yaml", "外部状态字段到内部判据字段的映射。", "例如 distance_error_m -> distance_error_max，grid_count -> min_grid_count。"],
            ["config/anomaly_advice.yaml", "ROV 异常语义库、组件组、异常类型和子任务异常画像。", "只影响异常建议输出，不直接改变任务流程状态。"],
        ],
        [3000, 3600, 5000],
    ))

    body.append(p("五、关键流程说明", "Heading1"))
    body.append(table(
        ["流程", "步骤摘要", "状态变化"],
        [
            ["任务创建", "TaskScanner 发现 intent JSON，TaskManager 调用 TaskDecomposer 生成子任务并保存。", "overall_status=in_progress，S1 从 pending 变为 in_progress。"],
            ["状态上报", "server.py 接收 status_update，TaskManager 调用 StateMonitor 映射并评估判据。", "根据判据进入 completed、failed、waiting_approval 或继续 in_progress。"],
            ["人工审核", "判据全部满足且 require_approval=true 时生成通知，审核通过后推进。", "waiting_approval -> completed，然后启动下一子任务。"],
            ["失败恢复", "当前失败子任务可重新上报；也可经确认后 retry、rollback、override_field 或 force_complete。", "failed 可恢复为 in_progress，但不能跳过 current_subtask。"],
            ["异常建议", "失败或判据未满足时记录上下文，匹配 anomaly_state 并生成建议。", "写入 latest_anomaly_context 和 latest_anomaly_advice，不直接改流程。"],
        ],
        [2300, 5600, 3300],
    ))

    body.append(p("六、维护注意事项", "Heading1"))
    for item in [
        "流程严格串行，任何非 current_subtask 的状态写入都会被拒绝。",
        "AnomalyHandler 中自动重试和自动回退已关闭，流程变更应通过人工确认后的干预完成。",
        "override_field 修改的是评估用状态值，不是判据阈值；若要调整阈值，应修改配置或新增专门动作。",
        "CLASSIFY_PROMPT 提到 adjust_criterion_tolerance，但 QueryResponder.VALID_ACTIONS 当前不包含该动作，模型输出后会被拒绝。",
        "QueryResponder.process_global() 的无关分支存在未定义变量 task_state 的潜在错误。",
        "run.py 启动前清理 vLLM 和端口的逻辑较强，共机部署时需确认不会误杀其他服务。",
        "data/copy/ 看起来是 src/ 和 config/ 的副本，当前启动链路使用仓库根目录下的 src/ 与 config/。",
    ]:
        body.append(bullet(item))

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {''.join(body)}
    <w:sectPr>
      <w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/>
      <w:pgMar w:top="900" w:right="900" w:bottom="900" w:left="900" w:header="708" w:footer="708" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>
"""


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", CONTENT_TYPES)
        docx.writestr("_rels/.rels", ROOT_RELS)
        docx.writestr("word/_rels/document.xml.rels", DOC_RELS)
        docx.writestr("word/styles.xml", STYLES)
        docx.writestr("word/document.xml", document_xml())
        docx.writestr("docProps/core.xml", CORE)
        docx.writestr("docProps/app.xml", APP)


if __name__ == "__main__":
    main()
