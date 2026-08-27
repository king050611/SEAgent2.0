"""
server.py – Flask routes: unified interface, status query, manual approval, LLM query, task list.
"""

import time
import logging
from flask import Flask, request, jsonify, render_template
from pathlib import Path

logger = logging.getLogger(__name__)


def create_app(task_manager, query_responder, state_monitor, state_store, task_scanner=None):
    # 设置模板文件夹为项目根目录（与 run.py 同级）
    app = Flask(__name__, template_folder=str(Path(__file__).parent.parent))

    def _is_standalone_confirmation_message(message: str) -> bool:
        text = (message or "").strip().lower()
        return text in {"确认", "确定", "同意", "可以", "执行", "yes", "y", "ok", "好"}

    def _is_standalone_cancel_message(message: str) -> bool:
        text = (message or "").strip().lower()
        return text in {"取消", "不用", "不要", "否", "no", "n"}

    def _is_explicit_new_intervention_message(message: str) -> bool:
        text = (message or "").strip()
        if not text or _is_standalone_confirmation_message(text) or _is_standalone_cancel_message(text):
            return False
        if any(marker in text for marker in ("吗", "能否", "是否", "可以", "可不可以", "？", "?")):
            return False
        # ########## Bug C1 修复：大幅扩展新干预动作的关键词匹配覆盖面 ##########
        intervention_keywords = (
            # retry 类
            "重试", "重新执行", "再跑一次", "再来一次", "再执行一遍", "重新跑", "再试一次",
            "重跑", "重新试", "再试",
            # rollback / 回退类
            "回退", "退回", "回到", "拉回", "退到", "回到步骤", "回到子任务",
            # change_parameter / 参数修改类
            "修改", "改成", "改为", "调整", "调为", "设置为", "设定为", "设为",
            "修改参数", "调整参数", "改参数", "放宽", "收紧", "阈值",
            # override_field / 事实覆盖类
            "覆盖", "修正", "改实际", "实测为", "实际为", "实际是", "确认为",
            "人工确认", "确认实际", "实际测得", "实际值", "现场确认",
            # force_complete 类
            "强制完成", "人工完成", "跳过", "强行完成", "直接完成", "人为完成",
            "手工完成", "标记完成",
            # 通用动作开头的模式（例如 "把S1..." / "对S2..." 前面有动作后面有子任务编号）
        )
        # 如果包含子任务编号模式且前面有关键动作描述，也判定为新干预
        import re
        has_subtask_ref = bool(re.search(r"(^|\s|，|,|。|、)(S\d+|步骤\d+|子任务\d+)(\s|，|,|。|、|$)", text))
        has_action_word = any(
            k in text
            for k in (
                "重", "回", "改", "调", "设", "覆", "修", "强", "跳过", "完成",
                "确认", "实际", "修正", "标记",
            )
        )
        return any(keyword in text for keyword in intervention_keywords) or (has_subtask_ref and has_action_word)

    def _json_body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return None
        return data

    @app.route("/")
    def index():
        # 直接渲染根目录下的 index.html
        return render_template("index.html")

    @app.route("/api/task/update", methods=["POST"])
    def update_task():
        """Unified interface: new task issuance / subtask status report."""
        data = _json_body()
        if not data:
            return jsonify({"error": "missing or invalid json body"}), 400

        task_id = data.get("task_id")
        msg_type = data.get("type")
        payload = data.get("data") or {}
        if not task_id:
            return jsonify({"error": "task_id required"}), 400
        if not isinstance(payload, dict):
            return jsonify({"error": "data must be an object"}), 400

        if msg_type == "new_task":
            description = payload.get("description")
            if not description:
                return jsonify({"error": "new_task requires data.description"}), 400
            result = task_manager.create_new_task(task_id, description, payload)
            status_code = 200 if result.get("ok") else 409 if result.get("error") == "task already exists" else 400
            return jsonify(result), status_code

        if msg_type == "status_update":
            subtask_id = payload.get("subtask_id")
            criteria_details = payload.get("criteria_details")
            if not subtask_id:
                return jsonify({"error": "status_update requires data.subtask_id"}), 400
            if not isinstance(criteria_details, dict):
                return jsonify({"error": "status_update requires data.criteria_details object"}), 400

            result = task_manager.update_subtask_status(
                task_id=task_id,
                subtask_id=subtask_id,
                status=payload.get("status", "reported"),
                criteria_met=payload.get("criteria_met", []),
                criteria_details=criteria_details,
                evidence_summary=payload.get("evidence_summary", ""),
                anomaly_key=payload.get("anomaly_key"),
                # ############# anomaly_state 接入开始 #############
                anomaly_state=payload.get("anomaly_state"),
                # ############# anomaly_state 接入结束 #############
            )
            code = 200 if not result.get("error") else 409 if result.get("error") in {"subtask_out_of_order", "subtask_not_writable", "task_not_active"} else 400
            return jsonify(result), code

        return jsonify({"error": "invalid type", "allowed": ["new_task", "status_update"]}), 400

    @app.route("/api/task/from_intent", methods=["POST"])
    def create_from_intent():
        """Create a task from the task admission module's JSON output."""
        intent_data = _json_body()
        if not intent_data:
            return jsonify({"error": "missing intent data"}), 400

        task_type = intent_data.get("task_type")
        if task_type == "valve_operation":
            description = "执行采油树控制面板插头插入任务"
        else:
            description = intent_data.get("task", {}).get("type") or f"执行 {task_type or '未知'} 任务"

        task_id = intent_data.get("intent_id", f"task_{int(time.time())}")
        initial_params = {
            "intent": intent_data,
            "description": description,
            "task_type": task_type,
            "priority": intent_data.get("priority"),
            "location": intent_data.get("location", {}),
            "time": intent_data.get("time", {}),
        }
        result = task_manager.create_new_task(task_id, description, initial_params)
        status_code = 200 if result.get("ok") else 409 if result.get("error") == "task already exists" else 400
        return jsonify(result), status_code

    @app.route("/api/task/scan", methods=["POST"])
    def scan_tasks():
        """Manually scan the task folder to create new tasks."""
        if task_scanner is None:
            return jsonify({"error": "scanner not initialized"}), 500
        results = task_scanner.scan_and_create()
        return jsonify({"results": results})

    @app.route("/api/task/<task_id>/subtask/<sub_id>/approve", methods=["POST"])
    def approve_subtask(task_id, sub_id):
        result = task_manager.approve_subtask(task_id, sub_id)
        code = 200 if not result.get("error") else 409
        return jsonify(result), code

    @app.route("/api/task/<task_id>/status", methods=["GET"])
    def get_task_status(task_id):
        status = task_manager.get_task_status(task_id)
        if not status:
            return jsonify({"error": "task not found"}), 404
        return jsonify(status)

    @app.route("/api/query", methods=["POST"])
    def query_task():
        """Handle queries, pending intervention confirmation, and new interventions."""
        try:
            data = _json_body()
            if not data:
                return jsonify({"error": "missing or invalid json body"}), 400
            user_message = (data.get("message") or "").strip()
            global_mode = data.get("global_mode", False)
            task_id = data.get("task_id") if not global_mode else None

            if not user_message:
                return jsonify({"error": "message required"}), 400

            if global_mode:
                all_tasks_state = []
                all_tasks = state_store.list_tasks()
                for tid, tstate in all_tasks.items():
                    all_tasks_state.append({
                        "task_id": tid,
                        "description": tstate.get("description"),
                        "overall_status": tstate.get("overall_status"),
                        "current_subtask": tstate.get("current_subtask"),
                        "subtasks": [
                            {"id": st["subtask_id"], "name": st["name"], "status": st["status"]}
                            for st in tstate.get("subtasks", [])
                        ]
                    })
                result = query_responder.process_global(user_message, all_tasks_state, task_manager)
                return jsonify(result)

            if not task_id:
                return jsonify({"error": "task_id required when global_mode is false"}), 400

            task_state = task_manager.get_task_status(task_id)
            if not task_state:
                return jsonify({"error": "task not found"}), 404

            pending = task_manager.get_pending_intervention(task_state)
            if pending:
                # ########## Bug C2 修复：检测到用户明确的新干预动作时，记录为 superseding_proposal + 历史 ##########
                if _is_explicit_new_intervention_message(user_message):
                    result = query_responder.process(user_message, task_state)
                    pending_intent = pending.get("intent") or (pending.get("raw_intent") or {}).get("intent") or "intervention"
                    if result["type"] == "query":
                        return jsonify({
                            "type": "query",
                            "intent": "query",
                            "answer": result["answer"],
                            "pending_action": pending.get("action"),
                            "refresh_required": False,
                        })
                    # 保存为新的候选提案（superseding_proposal），并追加到历史上下文
                    if result["type"] in {"control", "write"} and result.get("action"):
                        new_proposal = {
                            "action": result["action"],
                            "user_message": user_message,
                            "intent": result["type"],
                            "timestamp": time.time(),
                        }
                        history = list(pending.get("new_intervention_history") or [])
                        history.append(new_proposal)
                        # ########## Bug C2 兼容：旧版本/假 task_manager 无 update_pending_intervention_fields 时降级 ##########
                        if hasattr(task_manager, "update_pending_intervention_fields"):
                            task_manager.update_pending_intervention_fields(task_id, {
                                "superseding_proposal": new_proposal,
                                "new_intervention_history": history,
                            })
                        else:
                            # 降级：直接在原 pending dict 上就地修改（兼容旧接口/测试 stub）
                            try:
                                pending["superseding_proposal"] = new_proposal
                                pending["new_intervention_history"] = history
                            except Exception:
                                pass
                        # 生成明确的三选话术（确认原动作 / 确认新动作 / 取消全部）
                        answer = query_responder.generate_reply(
                            reply_intent=(
                                "当前已有待确认请求（原请求），本轮您又提出了新的流程控制或写入请求（新请求）。"
                                "请先简要复述两个请求的差异和影响范围，再明确给出三种选择："
                                "1）回复“确认原动作”或“确认原来的”——将执行原待确认动作；"
                                "2）回复“确认新动作”或“确认覆盖”——取消原请求并执行您本轮刚刚提出的新请求；"
                                "3）回复“取消”或“取消全部”——两边都不执行。"
                                "不要修改任务状态，等待用户三选一。"
                            ),
                            user_message=user_message,
                            task_state=task_state,
                            operation_result={
                                "pending_intervention": pending,
                                "new_request": result,
                                "new_proposal_saved": True,
                                "confirmation_decision": {"decision": "other", "reason": "explicit_new_intervention_with_superseding"},
                            },
                        )
                        return jsonify({
                            "type": "intervention_pending",
                            "intent": pending_intent,
                            "answer": answer,
                            "pending_action": pending.get("action"),
                            "superseding_action": new_proposal["action"],
                            "refresh_required": False,
                        })
                    # 如果新请求不是 control/write（如 clarification 降级为 irrelevant），保留原提示
                    answer = query_responder.generate_reply(
                        reply_intent=(
                            "当前已有一个待确认的流程控制或写入请求。用户本轮提出了新的请求，"
                            "请说明不会覆盖原待确认动作，并要求用户先回复“确认”或“取消”；"
                            "如果用户希望改执行新请求，可以回复“确认覆盖”或“取消原动作后再重新发起”。"
                        ),
                        user_message=user_message,
                        task_state=task_state,
                        operation_result={"pending_intervention": pending, "new_request": result, "confirmation_decision": {"decision": "other", "reason": "explicit_new_intervention"}},
                    )
                    return jsonify({
                        "type": "intervention_pending",
                        "intent": pending_intent,
                        "answer": answer,
                        "pending_action": pending.get("action"),
                        "refresh_required": False,
                    })

                # ########## Bug C2 修复：硬编码识别“确认新动作/确认覆盖/确认原动作” ##########
                _user_text = (user_message or "").strip()
                superseding = pending.get("superseding_proposal") if isinstance(pending.get("superseding_proposal"), dict) else None
                _confirm_new_keywords = ("确认新动作", "确认覆盖", "执行新的", "用新的", "确认新", "覆盖原")
                _confirm_old_keywords = ("确认原动作", "确认原来的", "执行原", "用原", "确认旧")
                if superseding and any(k in _user_text for k in _confirm_new_keywords):
                    # 用户明确选择新动作：先取消旧 pending，把新的 proposal 覆盖为新 pending，然后立即执行 confirm
                    task_manager.clear_pending_intervention(task_id)
                    new_action = superseding.get("action") or {}
                    new_intent = superseding.get("intent") or "intervention"
                    task_manager.set_pending_intervention(
                        task_id=task_id,
                        action=new_action,
                        user_message=superseding.get("user_message") or user_message,
                        raw_intent={"intent": new_intent},
                    )
                    latest_state = task_manager.get_task_status(task_id) or task_state
                    # 清掉新 pending 直接执行（用户已经明确选择"确认新"）
                    task_manager.clear_pending_intervention(task_id)
                    intervention_result = task_manager.execute_intervention(task_id, new_action)
                    latest_state = task_manager.get_task_status(task_id) or latest_state
                    answer = query_responder.generate_intervention_response(
                        user_message=superseding.get("user_message") or user_message,
                        intervention_result={
                            **intervention_result,
                            "confirmed_by_user": True,
                            "confirmation_message": user_message,
                            "superseded_original": True,
                        },
                        task_state=latest_state,
                        intent=new_intent,
                    )
                    return jsonify({
                        "type": "intervention",
                        "intent": new_intent,
                        "answer": answer,
                        "result": intervention_result,
                        "superseded_original": True,
                        "refresh_required": True,
                    })
                if superseding and any(k in _user_text for k in _confirm_old_keywords):
                    # 用户明确选择原动作：清掉 superseding 避免后续污染，走原 confirm 流程
                    if hasattr(task_manager, "update_pending_intervention_fields"):
                        task_manager.update_pending_intervention_fields(task_id, {
                            "superseding_proposal": None,
                        })
                    else:
                        try:
                            pending["superseding_proposal"] = None
                        except Exception:
                            pass
                    pending = task_manager.get_pending_intervention(task_manager.get_task_status(task_id) or task_state) or pending
                    decision_info = {"decision": "confirm", "confidence": 1.0, "reason": "user_explicit_confirm_original"}
                else:
                    decision_info = query_responder.classify_confirmation(user_message, pending, task_state)
                decision = decision_info.get("decision")

                if decision == "confirm":
                    task_manager.clear_pending_intervention(task_id)
                    action = pending.get("action") or {}
                    pending_intent = pending.get("intent") or (pending.get("raw_intent") or {}).get("intent") or "intervention"
                    intervention_result = task_manager.execute_intervention(task_id, action)
                    latest_state = task_manager.get_task_status(task_id) or task_state
                    answer = query_responder.generate_intervention_response(
                        user_message=pending.get("user_message") or user_message,
                        intervention_result={
                            **intervention_result,
                            "confirmed_by_user": True,
                            "confirmation_message": user_message,
                        },
                        task_state=latest_state,
                        intent=pending_intent,
                    )
                    return jsonify({
                        "type": "intervention",
                        "intent": pending_intent,
                        "answer": answer,
                        "result": intervention_result,
                        "refresh_required": True,
                    })

                if decision == "cancel":
                    pending_intent = pending.get("intent") or (pending.get("raw_intent") or {}).get("intent") or "intervention"
                    clear_result = task_manager.clear_pending_intervention(task_id)
                    latest_state = task_manager.get_task_status(task_id) or task_state
                    answer = query_responder.generate_reply(
                        reply_intent="用户取消了待确认的流程控制或写入请求。请说明没有修改任务状态，并提示可继续查询或重新发起请求",
                        user_message=user_message,
                        task_state=latest_state,
                        operation_result={"cancelled_pending_intervention": clear_result.get("pending_intervention")},
                    )
                    return jsonify({
                        "type": "intervention_cancelled",
                        "intent": pending_intent,
                        "answer": answer,
                        "refresh_required": False,
                    })

                result = query_responder.process(user_message, task_state)
                if result["type"] == "query":
                    return jsonify({
                        "type": "query",
                        "intent": "query",
                        "answer": result["answer"],
                        "pending_action": pending.get("action"),
                        "refresh_required": False,
                    })

                pending_intent = pending.get("intent") or (pending.get("raw_intent") or {}).get("intent") or "intervention"
                # 非 query / 非 confirm / 非 cancel：用户可能又提了一个新请求（没命中显式关键词），也一并记录到历史
                if result["type"] in {"control", "write"} and result.get("action"):
                    soft_proposal = {
                        "action": result["action"],
                        "user_message": user_message,
                        "intent": result["type"],
                        "timestamp": time.time(),
                        "implicit": True,
                    }
                    history = list(pending.get("new_intervention_history") or [])
                    history.append(soft_proposal)
                    if hasattr(task_manager, "update_pending_intervention_fields"):
                        task_manager.update_pending_intervention_fields(task_id, {
                            "new_intervention_history": history,
                        })
                    else:
                        try:
                            pending["new_intervention_history"] = history
                        except Exception:
                            pass
                answer = query_responder.generate_reply(
                    reply_intent=(
                        "当前已有一个待确认的流程控制或写入请求。用户本轮又提出了新的流程控制或写入请求，"
                        "请先复述原待确认动作和用户本轮请求的区别，再给出三种选择："
                        "1）回复“确认原动作”或“确认原来的”——执行原待确认动作；"
                        "2）回复“确认新动作”或“确认覆盖”——执行本轮最新请求；"
                        "3）回复“取消”或“取消全部”——两边都不执行。"
                        "要求用户三选一后再继续。"
                    ),
                    user_message=user_message,
                    task_state=task_state,
                    operation_result={"pending_intervention": pending, "new_request": result, "confirmation_decision": decision_info},
                )
                return jsonify({
                    "type": "intervention_pending",
                    "intent": pending_intent,
                    "answer": answer,
                    "pending_action": pending.get("action"),
                    "refresh_required": False,
                })

            result = query_responder.process(user_message, task_state)

            if _is_standalone_confirmation_message(user_message):
                current = task_state.get("current_subtask") or "当前子任务"
                current_name = ""
                for st in task_state.get("subtasks", []):
                    if st.get("subtask_id") == current and st.get("name"):
                        current_name = f"“{st.get('name')}”"
                        break
                answer = (
                    f"当前没有待确认操作，任务状态未被修改。"
                    f"目前推进到 {current}{current_name}，如需继续处理，请先发起明确动作，"
                    f"例如重试、回退、修改参数或人工完成。"
                )
                return jsonify({"type": "irrelevant", "intent": "irrelevant", "answer": answer, "refresh_required": False})

            if result["type"] == "irrelevant":
                return jsonify({"type": "irrelevant", "intent": result.get("intent", "irrelevant"), "answer": result["answer"], "refresh_required": False})
            if result["type"] == "query":
                return jsonify({"type": "query", "intent": "query", "answer": result["answer"], "refresh_required": False})
            if result["type"] in {"control", "write"}:
                action = result.get("action")
                if not action:
                    answer = query_responder.generate_reply(
                        reply_intent="说明流程控制或写入动作解析失败，请用户补充更明确的动作、参数、字段值或子任务",
                        user_message=user_message,
                        task_state=task_state,
                        operation_result={"error": "missing_action"},
                    )
                    return jsonify({"type": "irrelevant", "intent": result.get("intent", "irrelevant"), "answer": answer, "refresh_required": False})

                pending_result = task_manager.set_pending_intervention(
                    task_id=task_id,
                    action=action,
                    user_message=user_message,
                    raw_intent=result.get("raw_intent") or {"intent": result["type"]},
                )
                latest_state = task_manager.get_task_status(task_id) or task_state
                answer = query_responder.generate_confirmation_request(user_message, action, latest_state, intent=result["type"])
                return jsonify({
                    "type": "intervention_pending",
                    "intent": result["type"],
                    "answer": answer,
                    "pending_action": action,
                    "result": pending_result,
                    "refresh_required": False,
                })

            answer = query_responder.generate_reply(
                reply_intent="说明系统暂时无法理解请求，请用户换一种方式描述任务查询或干预指令",
                user_message=user_message,
                task_state=task_state,
                operation_result={"error": "unknown_processing_type", "result": result},
            )
            return jsonify({"type": "error", "answer": answer, "refresh_required": False})
        except Exception as e:
            logger.exception("Unhandled exception in /api/query: %s", e)
            return jsonify({
                "type": "error",
                "answer": "系统暂时无法处理本轮请求，请稍后重试。",
                "error": "internal_query_error",
                "refresh_required": False,
            }), 500

    @app.route("/api/task/<task_id>/reset", methods=["POST"])
    def reset_task(task_id):
        task_manager.reset_task(task_id)
        return jsonify({"ok": True})

    # ---------- 新增：清除通知 ----------
    @app.route("/api/task/<task_id>/notifications/clear", methods=["POST"])
    def clear_notifications(task_id):
        """清除该任务的所有未读通知（显示后调用）"""
        def _clear(state):
            state['notifications'] = []
        updated = state_store.update_task_atomic(task_id, _clear)
        if updated is None:
            return jsonify({"error": "task not found"}), 404
        return jsonify({"ok": True})

    @app.route("/api/tasks", methods=["GET"])
    def list_tasks():
        """Get a list of all tasks with basic info for the left sidebar."""
        all_tasks = state_store.list_tasks()
        result = []
        for task_id, state in all_tasks.items():
            meta = state.get("metadata", {})
            result.append({
                "task_id": task_id,
                "description": state.get("description", ""),
                "overall_status": state.get("overall_status", "unknown"),
                "current_subtask": state.get("current_subtask"),
                "created_at": state.get("created_at"),
                "task_type": meta.get("task_type"),
                "priority": meta.get("priority"),
                "location": meta.get("location", {}),
                "time": meta.get("time", {}),
            })
        result.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
        return jsonify(result)

    return app
