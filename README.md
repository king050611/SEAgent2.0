# TaskMonitor 测试集（基于task_intent_TI2026021192.json）

1. **启动系统**

```markdown
python run.py
```

2. **放置任务文件**

   ```bash
   cp task_intent_TI2026021192.json task/
   ```

3. **确认任务已创建**

   ```bash
   curl --noproxy '*' -s http://localhost:8889/api/tasks | jq .
   ```

---

## 三类任务对话意图

- QUERY：只读取任务事实、状态、判据和异常，不修改状态。
- CONTROL：改变流程位置或推进方式，包括 `rollback`、`retry`、`force_complete`。
- WRITE：写入参数或人工覆盖状态事实，包括 `change_parameter`、`override_field`。
- IRRELEVANT：与任务无关或无法可靠识别的安全出口。

查询水深、坐标、机器人属于 QUERY。修改水深、坐标、机器人元数据不在本轮 WRITE 范围内。

带有“能否、是否、为什么、怎么、会有什么影响、是否已经生效”等询问语气时，即使包含“重试、回退、修改、覆盖”等动作词，也优先作为 QUERY 处理，不会创建待确认动作。

CONTROL 和 WRITE 都需要二次确认。系统返回待确认回复后，用户需发送“确认”才会执行；发送“取消”会清除待确认动作。存在待确认动作时，仍可继续查询任务事实或动作影响，但新的 CONTROL/WRITE 请求不会覆盖原待确认动作。

---

## 一、正常流程（S1 → S8 全部成功）（测试时evidence_summary应当去掉！）

### S1：移动至采油树控制面板附近

**判据**：硬判据 `distance_error_max<=0.1`, `angle_error_max<=10.0`, `speed_stable_frames>=5`；软判据 `min_grid_count>=10`, `panel_visible_flag=1`；

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S1","status":"reported","criteria_details":{"distance_error_m":0.05,"angle_error_deg":5.0,"speed_stable_frames":5,"grid_count":15,"panel_visible_flag":1,"plug_stable_flag":1},"evidence_summary":"距离误差0.05m，角度误差5°，速度稳定，面板可见。"}}'
```

### S2：视觉识别插孔和插头的位置

**判据**：硬判据 `slot_pose_delta_max<=0.01`, `plug_pose_delta_max<=0.01`；软判据 `slot_stable_flag=1`, `plug_stable_flag=1`；

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S2","status":"reported","criteria_details":{"slot_pose_delta_m":0.005,"plug_pose_delta_m":0.006,"slot_stable_flag":1,"plug_stable_flag":1},"evidence_summary":"插孔插头位置稳定，delta均在0.01以内，稳定flag均为1。"}}'
```

### S3：机械臂原点到夹取起点的路径规划

**判据**：硬判据 `ik_valid_flag=1`；

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S3","status":"reported","criteria_details":{"ik_valid_flag":1},"evidence_summary":"逆运动学求解有效。"}}'
```

### S4：夹取插头

**判据**：硬判据 `grasp_done_flag=1`；

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S4","status":"reported","criteria_details":{"grasp_done_flag":1},"evidence_summary":"夹取动作完成。"}}'
```

### S5：机械臂起点到插入终点的路径规划

**判据**：硬判据 `ik_valid_flag=1`

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S5","status":"reported","criteria_details":{"ik_valid_flag":1},"evidence_summary":"逆运动学求解有效。"}}'
```

### S6：执行插入

**判据**：硬判据 `insert_done_flag=1`

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S6","status":"reported","criteria_details":{"insert_done_flag":1},"evidence_summary":"插入动作完成。"}}'
```

### S7：视觉 check 确认插入结果

**判据**：硬判据 `visual_check_flag=1`；需要人工审核

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S7","status":"reported","criteria_details":{"visual_check_flag":1},"evidence_summary":"视觉确认插入成功。"}}'
```

### S8：返程与复位

**判据**：硬判据 `arm_reset_flag=1`, `return_position_error_max<=0.1`；

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S8","status":"reported","criteria_details":{"arm_reset_flag":1,"return_position_error_m":0.03},"evidence_summary":"机械臂复位，返程误差0.03m。"}}'
```
---

## 二、异常流程及修复

### 异常场景1：端口修正

### S1 距离误差过大导致失败，通过重新上报正确数据恢复

**模拟失败上报**

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S1","status":"reported","criteria_details":{"distance_error_m":0.15,"angle_error_deg":5.0,"speed_stable_frames":5,"grid_count":15,"panel_visible_flag":1},"evidence_summary":"距离误差0.15m超标。"}}'
```

系统判距误差 0.15 > 0.1 → 硬判据不满足 → S1 状态变为 `failed`，任务整体状态变为 `failed`

此时任何上报都会被拒绝：



**修复方法**：重新上报正确的满足判据的数据（系统已修改为允许从 failed 恢复）

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S1","status":"reported","criteria_details":{"distance_error_m":0.05,"angle_error_deg":5.0,"speed_stable_frames":5,"grid_count":15,"panel_visible_flag":1},"evidence_summary":"修正后距离误差达标。"}}'
```

此时 S1 重新变为 `waiting_approval`，任务整体恢复 `in_progress`，然后可正常审核推进。

### 异常场景2：重试功能

### S2 视觉估计不稳定导致失败，通过对话框指令“重试S2”恢复

**模拟失败上报**（插孔位置变化过大）

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S2","status":"reported","criteria_details":{"slot_pose_delta_m":0.02,"plug_pose_delta_m":0.006,"slot_stable_flag":0,"plug_stable_flag":1}}}'
```

硬判据 `slot_pose_delta_max<=0.01` 不满足 → S2 失败 → 任务状态变为 `failed`

**通过自然语言对话框重试**

发送 POST 请求到 `/api/query`（模拟用户在聊天框输入）

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/query -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","global_mode":false,"message":"重试S2"}'
```

系统会返回要求确认的回复（需要二次确认）。再次发送确认消息：

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/query -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","global_mode":false,"message":"确认"}'
```

确认后系统执行重试，S2 状态变为 `in_progress`，任务恢复。然后重新上报正确数据：

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S2","status":"reported","criteria_details":{"slot_pose_delta_m":0.005,"plug_pose_delta_m":0.006,"slot_stable_flag":1,"plug_stable_flag":1}}}'
```

正常推进。

### 异常场景3：回退功能

### S7 视觉 check 失败，通过对话框“回退到S5”重新执行

**模拟失败上报**

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S7","status":"reported","criteria_details":{"visual_check_flag":0}}}'
```

S7 硬判据不满足 → S7 失败 → 任务失败。

**通过对话框回退**

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/query -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","global_mode":false,"message":"回退到S5"}'
```

系统要求确认，再发送：

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/query -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","global_mode":false,"message":"确认"}'
```

回退成功后，S5、S6、S7 被重置为 pending，当前子任务变为 S5。然后重新上报 S5、S6 的正确数据，最后 S7 上报 `visual_check_flag=1` 并审核，任务继续完成。

### 异常场景4：手动修正

### S1 距离误差过大导致失败，通过对话框人工覆盖实际状态值恢复

**模拟失败上报**（同场景1）

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/task/update -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","type":"status_update","data":{"subtask_id":"S1","status":"reported","criteria_details":{"distance_error_m":0.15,"angle_error_deg":5.0,"speed_stable_frames":5,"grid_count":15,"panel_visible_flag":1},"evidence_summary":"距离误差0.15m超标。"}}'
```

S1 失败，任务整体 `failed`。

**通过对话框人工覆盖字段值（WRITE / override_field）**

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/query -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","global_mode":false,"message":"将距离误差改为0.05米"}'
```

系统识别为 WRITE / `override_field` 动作，返回待确认回复。再次发送确认消息：

```bash
curl --noproxy '*' -X POST http://localhost:8889/api/query -H "Content-Type: application/json" -d '{"task_id":"TI2026021192","global_mode":false,"message":"确认"}'
```

确认后系统修改 `user_overrides`，重新评估判据。由于距离误差变为0.05（满足阈值），S1 硬判据全部满足，若需要人工审核则进入 `waiting_approval`，否则自动完成。

此时任务整体状态恢复为 `in_progress`。



