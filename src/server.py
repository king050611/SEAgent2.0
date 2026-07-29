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
                decision_info = query_responder.classify_confirmation(user_message, pending, task_state)
                decision = decision_info.get("decision")

                if decision == "confirm":
                    task_manager.clear_pending_intervention(task_id)
                    action = pending.get("action") or {}
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
                    )
                    return jsonify({
                        "type": "intervention",
                        "answer": answer,
                        "result": intervention_result,
                        "refresh_required": True,
                    })

                if decision == "cancel":
                    clear_result = task_manager.clear_pending_intervention(task_id)
                    latest_state = task_manager.get_task_status(task_id) or task_state
                    answer = query_responder.generate_reply(
                        reply_intent="用户取消了待确认的流程干预。请说明没有修改任务状态，并提示可继续查询或重新发起干预",
                        user_message=user_message,
                        task_state=latest_state,
                        operation_result={"cancelled_pending_intervention": clear_result.get("pending_intervention")},
                    )
                    return jsonify({
                        "type": "intervention_cancelled",
                        "answer": answer,
                        "refresh_required": False,
                    })

                answer = query_responder.generate_reply(
                    reply_intent="当前存在一个待确认的流程干预，用户本轮没有明确确认或取消。请提醒用户必须先回复“确认”或“取消”，不要执行任何修改",
                    user_message=user_message,
                    task_state=task_state,
                    operation_result={"pending_intervention": pending, "confirmation_decision": decision_info},
                )
                return jsonify({
                    "type": "intervention_pending",
                    "answer": answer,
                    "pending_action": pending.get("action"),
                    "refresh_required": False,
                })

            result = query_responder.process(user_message, task_state)

            if result["type"] == "irrelevant":
                return jsonify({"type": "irrelevant", "answer": result["answer"], "refresh_required": False})
            if result["type"] == "query":
                return jsonify({"type": "query", "answer": result["answer"], "refresh_required": False})
            if result["type"] == "intervention":
                action = result.get("action")
                if not action:
                    answer = query_responder.generate_reply(
                        reply_intent="说明干预动作解析失败，请用户补充更明确的动作、参数或子任务",
                        user_message=user_message,
                        task_state=task_state,
                        operation_result={"error": "missing_action"},
                    )
                    return jsonify({"type": "irrelevant", "answer": answer, "refresh_required": False})

                pending_result = task_manager.set_pending_intervention(
                    task_id=task_id,
                    action=action,
                    user_message=user_message,
                    raw_intent=result.get("raw_intent") or {},
                )
                latest_state = task_manager.get_task_status(task_id) or task_state
                answer = query_responder.generate_confirmation_request(user_message, action, latest_state)
                return jsonify({
                    "type": "intervention_pending",
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