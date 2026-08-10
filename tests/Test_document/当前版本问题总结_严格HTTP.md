# 当前版本问题总结（严格 HTTP）

这次不是 FakeLLM 仿真，而是严格走真实 HTTP 服务：后端输入走 `/api/task/update` / `/api/task/from_intent`，前端聊天输入走 `/api/query`。每条用例都在 JSON 和报告里保留了前端输入、后端输入、后端响应、前端响应。

测试结果：共 28 个用例，25 个通过，3 个失败。fixture 恢复结果：`True`。

## 失败项

### CR-TS1-CHANGE 硬失败解决方案：修改判据阈值

- 确认后执行 change_parameter: {"body": {"answer": "已收到您的修改指令，并将 S1“移动至采油树控制面板附近”的完成判据距离误差阈值更新为 0.15。\n\n系统已按此新标准重新评估当前子任务。由于此前判定失败是因为实际距离误差超过了旧阈值，在应用新阈值后，系统会立即检查机器人当前位置是否满足 0.15 米以内的距离要求。若满足条件，S1 将自动标记为完成，流程随即推进至 S2“视觉识别插孔和插头的位置”；若不满足，任务仍会保持失败状态并等待进一步操作。", "intent": "write", "refresh_required": true, "result": {"action": "override_field", "field": "distance_error_max", "hard_unmet": ["distance_error_max"], "value": 0.15}, "type": "intervention"}, "code": 200}
- distance_error_max 写入参数: {"description": "执行采油树控制面板插头插入任务", "intent": {"conditions": {}, "equipment": {"payload": [], "robot_type": "work_class_rov", "support_vessel": {"latitude": null, "longitude": null, "name": null}}, "intent_id": "TI2026021192", "location": {"oilfield": null, "water_depth_m": 200.0}, "monitoring_runtime_example": {"current_stage": "S2", "explanation": "当前阶段 S2 模型评估未通过，插孔与插头位置估计稳定性不足。", "failed_criteria": [{"actual": 0, "expected": 1, "name": "slot_pose_stable_flag", "operator": "=="}, {"actual": 0, "expected": 1, "name": "plug_pose_stable_flag", "operator": "=="}], "last_evaluation": {"explanation": "当前阶段 S2 模型评估未通过，插孔与插头位置估计稳定性不足，建议等待多帧稳定、调整视角并重新触发感知识别流程。", "failed_criteria_names": ["slot_pose_stable_flag", "plug_pose_stable_flag"], "passed": false, "primary_failure_type": "perception_failure", "primary_failure_type_name": "感知失败", "subtask_id": "S2", "suggestions": ["增强观测信息质量，等待多帧稳定", "调整传...<truncated>
- 最后一条后端输入：`{"method": "GET", "path": "/api/task/TI2026021192/status"}`
- 最后一条后端响应：`{"anomaly_state": {"data_commun": "normal", "execution": "normal", "perception": "normal", "planning": "normal", "plant": "normal"}, "created_at": 1785996866.0808673, "current_subtask": "S1", "description": "执行采油树控制面板插头插入任务", "global_parameters": {"description": "执行采油树控制面板插头插入任务", "intent": {"conditions": {}, "equipment": {"payload": [], "robot_type": "work_class_rov", "support_vessel": {"latitude": null, "longitude": null, "name": null}}, "intent_id": "TI2026021192", "location": {"oilfield": null, "water_depth_m": 200.0}, "monitoring_runtime_example": {"current_stage": "S2", "explanation": "当前阶段 S2 模型评估未通过，插孔与插头位置估计稳定性不足。", "failed_criteria": [{"actual": 0, "expected": 1, "name": "slot_pose_stable_flag", "operator": "=="}, {"actual": 0, "expected": 1, "name": "plug_pose_stable_flag", "operator": "=="}], "last_evaluation": {"explanation": "当前阶段 S2 模型评估未通过，插孔与插头位置估计稳定性不足，建议等待多帧稳定、调整视角并重新触发感知识别流程。", "failed_criteria_names": ["slot_pose_stable_flag", "plug_pose_stable_flag"], "passed": false, "primary_failure_type": "perception_failure", "primary_failure_type_name": "感知失败", "subtask_id": "S2", "suggestions": ["增强观测信息质量，等待多帧稳定", "调整传感器视角或观测位置", "重新触发感知识别流程"]}, "pending_human_review": t...<truncated>`
- 最后一条前端输入：`确认`
- 最后一条前端响应：`{"answer": "已收到您的修改指令，并将 S1“移动至采油树控制面板附近”的完成判据距离误差阈值更新为 0.15。\n\n系统已按此新标准重新评估当前子任务。由于此前判定失败是因为实际距离误差超过了旧阈值，在应用新阈值后，系统会立即检查机器人当前位置是否满足 0.15 米以内的距离要求。若满足条件，S1 将自动标记为完成，流程随即推进至 S2“视觉识别插孔和插头的位置”；若不满足，任务仍会保持失败状态并等待进一步操作。", "intent": "write", "refresh_required": true, "result": {"action": "override_field", "field": "distance_error_max", "hard_unmet": ["distance_error_max"], "value": 0.15}, "type": "intervention"}`

### QA-TS7 与任务无关的问题

- 前端路由为 irrelevant: {"answer": "抱歉，我目前无法提供天气信息。作为水下任务流程监管系统的现场助手，我只能协助您查询任务进度、分析失败原因或提供流程干预建议。\n\n如果您需要关注任务状态，当前任务（TI2026021192）整体状态为失败，卡点在子任务 S1“移动至采油树控制面板附近”。该步骤因未满足距离误差等完成条件而未能通过，导致后续视觉识别及机械臂操作等步骤尚未执行。如需继续推进或调整任务，请指示下一步操作。", "intent": "query", "refresh_required": false, "type": "query"}
- 最后一条后端输入：`{"json": {"data": {"anomaly_state": {"data_commun": "normal", "execution": "normal", "perception": "normal", "planning": "normal", "plant": "normal"}, "criteria_details": {"angle_error_deg": 5.0, "distance_error_m": 0.15, "grid_count": 15, "panel_visible_flag": 1, "plug_stable_flag": 1, "speed_stable_frames": 5}, "status": "reported", "subtask_id": "S1"}, "task_id": "TI2026021192", "type": "status_update"}, "method": "POST", "path": "/api/task/update"}`
- 最后一条后端响应：`{"action": "fail_task", "message": "目标点不可达，请检查环境或重新规划路径", "subtask_id": "S1"}`
- 最后一条前端输入：`今天天气怎么样？`
- 最后一条前端响应：`{"answer": "抱歉，我目前无法提供天气信息。作为水下任务流程监管系统的现场助手，我只能协助您查询任务进度、分析失败原因或提供流程干预建议。\n\n如果您需要关注任务状态，当前任务（TI2026021192）整体状态为失败，卡点在子任务 S1“移动至采油树控制面板附近”。该步骤因未满足距离误差等完成条件而未能通过，导致后续视觉识别及机械臂操作等步骤尚未执行。如需继续推进或调整任务，请指示下一步操作。", "intent": "query", "refresh_required": false, "type": "query"}`

### INT-TS3 change_parameter 二次确认与执行

- 确认后执行 change_parameter: {"body": {"answer": "已收到您的修改指令，系统已将子任务 S1“移动至采油树控制面板附近”的完成判据 distance_error_max 更新为 0.15 并重新评估。\n\n当前子任务 S1 状态仍显示为失败，原因是 ROV 尚未稳定到达目标距离，实际距离误差超过了更新后的允许范围。由于该前置步骤未完成，后续的子任务 S2 至 S8 暂时无法进入执行阶段。\n\n建议检查 ROV 当前的定位数据及周围障碍物情况，确认是否已接近控制面板。待距离误差满足新阈值后，子任务状态将自动更新，流程可继续推进。", "intent": "write", "refresh_required": true, "result": {"action": "override_field", "field": "distance_error_max", "hard_unmet": ["distance_error_max"], "value": 0.15}, "type": "intervention"}, "code": 200}
- distance_error_max 写入参数: {"description": "执行采油树控制面板插头插入任务", "intent": {"conditions": {}, "equipment": {"payload": [], "robot_type": "work_class_rov", "support_vessel": {"latitude": null, "longitude": null, "name": null}}, "intent_id": "TI2026021192", "location": {"oilfield": null, "water_depth_m": 200.0}, "monitoring_runtime_example": {"current_stage": "S2", "explanation": "当前阶段 S2 模型评估未通过，插孔与插头位置估计稳定性不足。", "failed_criteria": [{"actual": 0, "expected": 1, "name": "slot_pose_stable_flag", "operator": "=="}, {"actual": 0, "expected": 1, "name": "plug_pose_stable_flag", "operator": "=="}], "last_evaluation": {"explanation": "当前阶段 S2 模型评估未通过，插孔与插头位置估计稳定性不足，建议等待多帧稳定、调整视角并重新触发感知识别流程。", "failed_criteria_names": ["slot_pose_stable_flag", "plug_pose_stable_flag"], "passed": false, "primary_failure_type": "perception_failure", "primary_failure_type_name": "感知失败", "subtask_id": "S2", "suggestions": ["增强观测信息质量，等待多帧稳定", "调整传...<truncated>
- 最后一条后端输入：`{"method": "GET", "path": "/api/task/TI2026021192/status"}`
- 最后一条后端响应：`{"anomaly_state": {"data_commun": "normal", "execution": "normal", "perception": "normal", "planning": "normal", "plant": "normal"}, "created_at": 1785996926.4126804, "current_subtask": "S1", "description": "执行采油树控制面板插头插入任务", "global_parameters": {"description": "执行采油树控制面板插头插入任务", "intent": {"conditions": {}, "equipment": {"payload": [], "robot_type": "work_class_rov", "support_vessel": {"latitude": null, "longitude": null, "name": null}}, "intent_id": "TI2026021192", "location": {"oilfield": null, "water_depth_m": 200.0}, "monitoring_runtime_example": {"current_stage": "S2", "explanation": "当前阶段 S2 模型评估未通过，插孔与插头位置估计稳定性不足。", "failed_criteria": [{"actual": 0, "expected": 1, "name": "slot_pose_stable_flag", "operator": "=="}, {"actual": 0, "expected": 1, "name": "plug_pose_stable_flag", "operator": "=="}], "last_evaluation": {"explanation": "当前阶段 S2 模型评估未通过，插孔与插头位置估计稳定性不足，建议等待多帧稳定、调整视角并重新触发感知识别流程。", "failed_criteria_names": ["slot_pose_stable_flag", "plug_pose_stable_flag"], "passed": false, "primary_failure_type": "perception_failure", "primary_failure_type_name": "感知失败", "subtask_id": "S2", "suggestions": ["增强观测信息质量，等待多帧稳定", "调整传感器视角或观测位置", "重新触发感知识别流程"]}, "pending_human_review": t...<truncated>`
- 最后一条前端输入：`确认`
- 最后一条前端响应：`{"answer": "已收到您的修改指令，系统已将子任务 S1“移动至采油树控制面板附近”的完成判据 distance_error_max 更新为 0.15 并重新评估。\n\n当前子任务 S1 状态仍显示为失败，原因是 ROV 尚未稳定到达目标距离，实际距离误差超过了更新后的允许范围。由于该前置步骤未完成，后续的子任务 S2 至 S8 暂时无法进入执行阶段。\n\n建议检查 ROV 当前的定位数据及周围障碍物情况，确认是否已接近控制面板。待距离误差满足新阈值后，子任务状态将自动更新，流程可继续推进。", "intent": "write", "refresh_required": true, "result": {"action": "override_field", "field": "distance_error_max", "hard_unmet": ["distance_error_max"], "value": 0.15}, "type": "intervention"}`

### 补充记录：已有 pending 干预时，“重试S1”被旧 force_complete pending 拦截

- 现象：S1 失败后，用户在前端输入 `重试S1`，但后端日志显示当前已有待确认干预为 `force_complete`：
  `{'action': {'action': 'force_complete', 'subtask_id': 'S1'}, 'user_message': '重试S1', 'raw_intent': {'intent': 'control', 'action': {'action': 'force_complete', 'subtask_id': 'S1'}, ...}, 'status': 'awaiting_confirmation'}`
- 同轮前端/LLM 可产生正确的重试解析：`{"intent":"control","target_task_id":"TI2026021192","query_topics":[],"action":{"action":"retry","subtask_id":"S1"},"needs_clarification":false,"confidence":0.95,"reason":"用户明确指令'重试 S1'，符合 CONTROL 类中重试当前子任务的定义。"}`
- 随后确认判断为：`{"decision":"confirm","confidence":0.99}`。
- 根因判断：`/api/query` 在存在 `pending_intervention` 时会优先进入确认/取消判断，并使用旧 pending 的 `action` 执行；因此新的明确控制指令 `重试S1` 不会覆盖旧 pending，也不会先按新指令重新解析执行。
- 风险：如果旧 pending 因 LLM 误判写成 `force_complete`，用户后续输入 `重试S1` 可能被当成对旧 `force_complete` 的确认语境，存在执行强制完成而不是重试的风险。
- 建议：已有 pending 时，若用户输入包含明确新控制/写入动作（如 `重试S1`、`回退到S2`、`修改...`），不要交给确认分类器直接判定为确认；应提示先 `确认`/`取消` 旧动作，或显式支持“取消旧 pending 并覆盖为新动作”。

### 补充记录：输入“回退到S1”后确认，却执行旧 force_complete S2

- 现象：用户期望执行 `回退到S1`，随后输入 `确认`，但后端实际持有的待确认干预为 `force_complete S2`，最终表现为 S2 被人工完成/流程继续推进，而不是回退到 S1。
- 附件日志证据：
  - pending 内容：`{'action': {'action': 'force_complete', 'subtask_id': 'S2'}, 'user_message': '确认', 'raw_intent': {'intent': 'control', 'action': {'action': 'force_complete', 'subtask_id': 'S2'}, 'reason': "用户发送'确认'，在当前子任务 S2 进行中且 pending_intervention 为 null 的状态下，表示人工确认当前步骤完成，符合 force_complete 意图。"}, 'status': 'awaiting_confirmation'}`
  - 第一次确认判断：`LLM classify_confirmation result: {'decision': 'other', 'confidence': 0.95}`
  - 第二次确认判断：`LLM classify_confirmation result: {'decision': 'confirm', 'confidence': 0.99}`
- 根因判断：`/api/query` 检测到已有 `pending_intervention` 后，先调用 `classify_confirmation` 判断当前输入是否确认/取消旧 pending；若确认，则直接执行 `pending.action`。因此在旧 pending 为 `force_complete S2` 时，后续 `确认` 执行的是 `force_complete S2`，不是用户期望的 `rollback S1`。
- 进一步影响：`force_complete` 会把当前子任务标记为 completed，并调用 `_advance_to_next` 推进流程；因此前端会看到类似“人工审核通过/已完成并推进”的效果。
- 风险：当用户输入 `回退到S1` 时，如果系统已有旧 pending，且旧 pending 没有被取消或覆盖，用户的下一次 `确认` 可能确认旧动作，造成方向完全相反的流程变更。
- 建议：已有 pending 时，如果新输入是明确的新动作（如 `回退到S1`），应拒绝进入旧 pending 的确认执行链路，并明确提示“当前待确认动作为 X；请先取消后再发起回退”，或提供覆盖旧 pending 的显式机制。另需避免把单独的 `确认` 在无 pending 语境下解析并存储为新的 `force_complete`。

## 证据文件

- 测试报告：`/root/seagent/seagent2.0-main_nomodule/tests/Test_document/测试集2026_严格HTTP测试报告.md`
- JSON 明细：`/root/seagent/seagent2.0-main_nomodule/tests/Test_document/测试集2026_严格HTTP测试结果.json`
- run.py 日志：`/root/seagent/seagent2.0-main_nomodule/tests/Test_document/测试集2026_严格HTTP_runpy.log`
- revision：`3e16276eb2ea3fea7a14cef688ac3a22e2892d4c`
